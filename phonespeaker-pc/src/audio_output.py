"""
audio_output.py — PC audio playback, dual-speaker sync delay line, and Phone -> PC receiver.
Supports Windows (WASAPI/PyAudio) and Linux (PipeWire/PulseAudio/PyAudio).
"""

import collections
import logging
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

try:
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        import pyaudio
    _PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None
    _PYAUDIO_AVAILABLE = False

try:
    import soxr
    _SOXR_AVAILABLE = True
except ImportError:
    soxr = None
    _SOXR_AVAILABLE = False

from opus_codec import OpusDecoder
from protocol import (
    CODEC_OPUS,
    CODEC_PCM16,
    PHONE_AUDIO_PORT,
    SAMPLE_RATE,
    CHANNELS,
    parse_header,
)

logger = logging.getLogger(__name__)
_IS_LINUX = sys.platform.startswith("linux")


def list_output_devices() -> List[Dict]:
    """Return available audio output devices (plus Linux PulseAudio/PipeWire sinks)."""
    devices = []
    if _IS_LINUX and shutil.which("pactl"):
        try:
            res = subprocess.run(
                ["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=2
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        sink_name = parts[1].strip()
                        is_virt = "phonespeaker" in sink_name.lower() or "null" in sink_name.lower()
                        devices.append({
                            "index": f"pulse:{sink_name}",
                            "name": f"[PipeWire/Pulse] {sink_name}",
                            "sample_rate": SAMPLE_RATE,
                            "channels": CHANNELS,
                            "is_default": False,
                            "is_virtual": is_virt,
                        })
        except Exception:
            pass

    if not _PYAUDIO_AVAILABLE:
        return devices

    try:
        pa = pyaudio.PyAudio()
        try:
            default_idx = None
            try:
                def_info = pa.get_default_output_device_info()
                default_idx = def_info.get("index")
            except Exception:
                pass

            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                    max_out = info.get("maxOutputChannels", 0)
                    is_loopback = info.get("isLoopbackDevice", False)
                    if max_out > 0 and not is_loopback:
                        name = info.get("name", f"Output {i}")
                        name_lower = name.lower()
                        is_virt = any(
                            kw in name_lower
                            for kw in ["virtual", "cable", "phonespeaker", "vb-audio", "audiorelay"]
                        )
                        devices.append({
                            "index": i,
                            "name": name,
                            "sample_rate": int(info.get("defaultSampleRate", SAMPLE_RATE)),
                            "channels": max_out,
                            "is_default": (i == default_idx),
                            "is_virtual": is_virt,
                        })
                except Exception:
                    continue
            devices.sort(key=lambda d: (not d.get("is_default", False), not d.get("is_virtual", False)))
        finally:
            pa.terminate()
    except Exception as exc:
        logger.error("Error enumerating output devices: %s", exc)

    return devices


class PCDelayedAudioPlayer:
    """
    Plays captured audio to a physical PC output device with a configurable delay
    (0 to 150 ms) to achieve acoustic synchronization with the Phone speaker.
    """

    def __init__(
        self,
        device_index: Optional[object] = None,
        delay_ms: float = 25.0,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
    ):
        self._device_index = device_index
        self._delay_ms = max(0.0, min(300.0, delay_ms))
        self._sample_rate = sample_rate
        self._channels = channels

        self._pa: Optional["pyaudio.PyAudio"] = None
        self._stream = None
        self._pacat_proc: Optional[subprocess.Popen] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._buffer: collections.deque = collections.deque(maxlen=100)
        self._volume_gain: float = 1.0
        self._muted: bool = False
        self._lock = threading.Lock()

    def set_delay_ms(self, delay_ms: float) -> None:
        with self._lock:
            self._delay_ms = max(0.0, min(300.0, delay_ms))

    def set_gain_and_mute(self, gain: float, muted: bool) -> None:
        with self._lock:
            self._volume_gain = max(0.0, min(2.0, float(gain)))
            self._muted = bool(muted)

    def set_output_device(self, device_index: Optional[object]) -> None:
        """Hot-swap the PC playback device without crashing."""
        if self._device_index == device_index:
            return
        self._device_index = device_index
        if self._running:
            self.stop()
            self.start()

    def write_frame(self, pcm_bytes: bytes) -> None:
        if not self._running:
            return
        now = time.monotonic()
        with self._lock:
            self._buffer.append((now, pcm_bytes))

    def start(self) -> bool:
        if self._running:
            return False

        if _IS_LINUX and isinstance(self._device_index, str) and self._device_index.startswith("pulse:"):
            sink = self._device_index.split("pulse:", 1)[1]
            if shutil.which("pacat"):
                try:
                    self._pacat_proc = subprocess.Popen(
                        [
                            "pacat", "--playback", f"--device={sink}",
                            f"--rate={self._sample_rate}", "--format=s16le",
                            f"--channels={self._channels}", "--latency-msec=10",
                        ],
                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                    )
                    self._running = True
                    self._thread = threading.Thread(target=self._run, name="PCDelayedAudioPlayer", daemon=True)
                    self._thread.start()
                    return True
                except Exception as exc:
                    logger.error("Failed to start pacat player: %s", exc)
                    return False

        if not _PYAUDIO_AVAILABLE:
            return False

        try:
            self._pa = pyaudio.PyAudio()
            dev_idx = self._device_index if isinstance(self._device_index, int) else None
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self._channels,
                rate=self._sample_rate,
                output=True,
                output_device_index=dev_idx,
                frames_per_buffer=240,
            )
            self._running = True
            self._thread = threading.Thread(target=self._run, name="PCDelayedAudioPlayer", daemon=True)
            self._thread.start()
            return True
        except Exception as exc:
            logger.error("Failed to start PCDelayedAudioPlayer: %s", exc)
            self.stop()
            return False

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._pacat_proc:
            try:
                self._pacat_proc.terminate()
                self._pacat_proc.wait(timeout=1.0)
            except Exception:
                pass
            self._pacat_proc = None
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        with self._lock:
            self._buffer.clear()

    def _run(self) -> None:
        while self._running:
            now = time.monotonic()
            with self._lock:
                target_delay_sec = self._delay_ms / 1000.0
                frame_to_play = None
                if self._buffer:
                    ts, data = self._buffer[0]
                    if (now - ts) >= target_delay_sec:
                        frame_to_play = self._buffer.popleft()[1]
                muted = self._muted
                gain = self._volume_gain

            if frame_to_play:
                if muted:
                    frame_to_play = b"\x00" * len(frame_to_play)
                elif abs(gain - 1.0) > 0.01:
                    arr = np.frombuffer(frame_to_play, dtype="<i2").astype(np.float32)
                    arr = np.clip(arr * gain, -32768, 32767).astype("<i2")
                    frame_to_play = arr.tobytes()

                try:
                    if self._pacat_proc and self._pacat_proc.stdin:
                        self._pacat_proc.stdin.write(frame_to_play)
                    elif self._stream:
                        self._stream.write(frame_to_play)
                except Exception:
                    pass
            else:
                time.sleep(0.002)


class PhoneAudioReceiver:
    """
    Listens on UDP port 5006 for incoming audio from the Phone (Mic),
    decodes PCM16 or Opus, and plays it to the selected PC audio output / virtual mic device.
    """

    def __init__(
        self,
        port: int = PHONE_AUDIO_PORT,
        output_device_index: Optional[object] = None,
        on_stats: Optional[Callable[[int], None]] = None,
    ):
        self._port = port
        self._device_index = output_device_index
        self._on_stats = on_stats

        self._sock: Optional[socket.socket] = None
        self._pa: Optional["pyaudio.PyAudio"] = None
        self._stream = None
        self._pacat_proc: Optional[subprocess.Popen] = None
        self._dev_rate = SAMPLE_RATE
        self._dev_channels = CHANNELS
        self._opus_decoder: Optional[OpusDecoder] = None

        self._running = False
        self._muted = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pps_count = 0
        self._last_pps_time = 0.0
        self.peak_level: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def _open_audio_sink(self) -> bool:
        """Open either PipeWire/PulseAudio sink via pacat or PyAudio stream."""
        if _IS_LINUX and (
            (isinstance(self._device_index, str) and self._device_index.startswith("pulse:"))
            or not _PYAUDIO_AVAILABLE
        ):
            if shutil.which("pacat"):
                cmd = [
                    "pacat", "--playback",
                    f"--rate={SAMPLE_RATE}", "--format=s16le",
                    f"--channels={CHANNELS}", "--latency-msec=10",
                ]
                if isinstance(self._device_index, str) and self._device_index.startswith("pulse:"):
                    sink = self._device_index.split("pulse:", 1)[1]
                    cmd.append(f"--device={sink}")
                self._pacat_proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
                )
                self._dev_rate = SAMPLE_RATE
                self._dev_channels = CHANNELS
                return True

        if not _PYAUDIO_AVAILABLE:
            return False

        if self._pa is None:
            self._pa = pyaudio.PyAudio()

        dev_idx = self._device_index if isinstance(self._device_index, int) else None
        dev_rate = SAMPLE_RATE
        dev_channels = CHANNELS
        try:
            if dev_idx is not None:
                dev_info = self._pa.get_device_info_by_index(dev_idx)
            else:
                dev_info = self._pa.get_default_output_device_info()
            dev_rate = int(dev_info.get("defaultSampleRate", SAMPLE_RATE))
            max_ch = int(dev_info.get("maxOutputChannels", 2))
            dev_channels = min(2, max(1, max_ch))
        except Exception:
            pass

        # Prefer opening directly at 48000 Hz if the driver supports it
        for candidate_rate in (SAMPLE_RATE, dev_rate):
            try:
                self._stream = self._pa.open(
                    format=pyaudio.paInt16,
                    channels=dev_channels,
                    rate=candidate_rate,
                    output=True,
                    output_device_index=dev_idx,
                    frames_per_buffer=max(64, int(candidate_rate * 0.005)),
                )
                self._dev_rate = candidate_rate
                self._dev_channels = dev_channels
                return True
            except Exception:
                continue
        return False

    def _close_audio_sink(self) -> None:
        if self._pacat_proc:
            try:
                self._pacat_proc.terminate()
                self._pacat_proc.wait(timeout=1.0)
            except Exception:
                pass
            self._pacat_proc = None
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def set_output_device(self, device_index: Optional[object]) -> None:
        if self._device_index == device_index:
            return
        self._device_index = device_index
        if self._running:
            with self._lock:
                self._close_audio_sink()
                self._open_audio_sink()

    def start(self) -> bool:
        if self._running:
            return True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
            self._sock.bind(("", self._port))
            self._sock.settimeout(0.25)

            with self._lock:
                if not self._open_audio_sink():
                    logger.warning("PhoneAudioReceiver could not open audio output device")

            self._opus_decoder = OpusDecoder(SAMPLE_RATE, CHANNELS)
            self._running = True
            self._pps_count = 0
            self._last_pps_time = time.monotonic()
            self._thread = threading.Thread(target=self._run, name="PhoneAudioReceiver", daemon=True)
            self._thread.start()
            logger.info("PhoneAudioReceiver listening on UDP port %d", self._port)
            return True
        except Exception as exc:
            logger.error("Failed to start PhoneAudioReceiver: %s", exc)
            self.stop()
            return False

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        with self._lock:
            self._close_audio_sink()
            if self._pa:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None
        if self._opus_decoder:
            self._opus_decoder.destroy()
            self._opus_decoder = None
        self.peak_level = 0.0
        if self._on_stats:
            try:
                self._on_stats(0)
            except Exception:
                pass
        logger.info("PhoneAudioReceiver stopped")

    def _run(self) -> None:
        while self._running:
            try:
                data, _ = self._sock.recvfrom(2048)

                # Drain excess queued packets (>6 frames = >30ms) to prevent latency accumulation
                drained = 0
                while drained < 10:
                    r, _, _ = select.select([self._sock], [], [], 0.0)
                    if not r:
                        break
                    # Peek/consume if backlog is building up
                    data, _ = self._sock.recvfrom(2048)
                    drained += 1

                header = parse_header(data)
                if header:
                    payload = data[12:]
                    codec = header["codec"]
                    channels = header["channels"]

                    decoded_pcm = None
                    if codec == CODEC_OPUS and self._opus_decoder and self._opus_decoder.available:
                        decoded_pcm = self._opus_decoder.decode(payload, frame_size=960)
                    elif codec == CODEC_PCM16:
                        decoded_pcm = payload

                    if decoded_pcm and not self._muted:
                        pcm_arr = np.frombuffer(decoded_pcm, dtype="<i2")
                        if len(pcm_arr) > 0:
                            self.peak_level = float(np.max(np.abs(pcm_arr))) / 32768.0

                        out_pcm = decoded_pcm
                        if self._dev_rate != SAMPLE_RATE or self._dev_channels != channels:
                            in_ch = channels if channels in (1, 2) else 2
                            pcm_2d = pcm_arr.reshape(-1, in_ch)
                            if self._dev_channels == 1 and in_ch == 2:
                                pcm_2d = pcm_2d.mean(axis=1, keepdims=True).astype(np.int16)
                            elif self._dev_channels == 2 and in_ch == 1:
                                pcm_2d = np.repeat(pcm_2d, 2, axis=1)

                            if self._dev_rate != SAMPLE_RATE:
                                if _SOXR_AVAILABLE:
                                    resampled = soxr.resample(
                                        pcm_2d.astype(np.float32), SAMPLE_RATE, self._dev_rate, quality="LQ"
                                    )
                                    out_pcm = np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
                                else:
                                    in_len = len(pcm_2d)
                                    out_len = int(round(in_len * self._dev_rate / SAMPLE_RATE))
                                    x_old = np.arange(in_len, dtype=np.float32) / in_len
                                    x_new = np.arange(out_len, dtype=np.float32) / out_len
                                    resampled = np.zeros((out_len, self._dev_channels), dtype=np.float32)
                                    for c in range(self._dev_channels):
                                        resampled[:, c] = np.interp(x_new, x_old, pcm_2d[:, c])
                                    out_pcm = np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
                            else:
                                out_pcm = pcm_2d.astype("<i2").tobytes()

                        with self._lock:
                            if self._pacat_proc and self._pacat_proc.stdin:
                                self._pacat_proc.stdin.write(out_pcm)
                            elif self._stream:
                                self._stream.write(out_pcm)

                    self._pps_count += 1

            except socket.timeout:
                self.peak_level = 0.0
            except Exception as exc:
                if self._running:
                    logger.debug("PhoneAudioReceiver loop error: %s", exc)

            # Always update PPS every 1.0s (even on timeout so RX PPS resets to 0 when phone stops!)
            now = time.monotonic()
            if (now - self._last_pps_time) >= 1.0:
                elapsed = now - self._last_pps_time
                pps = int(round(self._pps_count / elapsed))
                if self._on_stats:
                    try:
                        self._on_stats(pps)
                    except Exception:
                        pass
                self._pps_count = 0
                self._last_pps_time = now
