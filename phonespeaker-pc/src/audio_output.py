"""
audio_output.py — PC audio playback, drift-free dual-speaker sync delay line,
and zero-drop Phone -> PC microphone receiver.
Supports Windows (WASAPI/PyAudio) and Linux (PipeWire/PulseAudio/PyAudio).
"""

import collections
import logging
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

_VIRTUAL_KEYWORDS = [
    "virtual",
    "cable",
    "phonespeaker",
    "vb-audio",
    "audiorelay",
    "voicemeeter",
    "vac",
    "null",
]

_MAPPER_KEYWORDS = [
    "microsoft sound mapper",
    "primary sound driver",
    "primary sound capture driver",
]


def normalize_device_name(name: str) -> str:
    """Normalize a device name so Loopback and Output endpoints of the same device can be matched."""
    s = (name or "").lower()
    for tag in ["[loopback]", "(loopback)", "monitor: ", "[pipewire/pulse] ", "(default)"]:
        s = s.replace(tag, "")
    return " ".join(s.split()).strip()


def is_virtual_device_name(name: str) -> bool:
    name_lower = (name or "").lower()
    return any(kw in name_lower for kw in _VIRTUAL_KEYWORDS)


def list_output_devices() -> List[Dict]:
    """
    Return real audio output devices (plus Linux PulseAudio/PipeWire sinks),
    excluding OS Sound Mapper redirectors that cause loopback feedback loops.
    Physical speakers/headphones are sorted first, followed by virtual cables.
    """
    devices = []
    if _IS_LINUX and shutil.which("pactl"):
        try:
            default_sink = ""
            res_def = subprocess.run(
                ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=2
            )
            if res_def.returncode == 0:
                default_sink = res_def.stdout.strip()

            res = subprocess.run(
                ["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=2
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        sink_name = parts[1].strip()
                        is_virt = is_virtual_device_name(sink_name)
                        devices.append({
                            "index": f"pulse:{sink_name}",
                            "name": f"[PipeWire/Pulse] {sink_name}",
                            "sample_rate": SAMPLE_RATE,
                            "channels": CHANNELS,
                            "is_default": (sink_name == default_sink),
                            "is_virtual": is_virt,
                        })
        except Exception:
            pass

    if not _PYAUDIO_AVAILABLE:
        return devices

    try:
        pa = pyaudio.PyAudio()
        try:
            default_name_norm = ""
            try:
                def_info = pa.get_default_output_device_info()
                default_name_norm = normalize_device_name(def_info.get("name", ""))
            except Exception:
                pass

            # Prefer MME host API on Windows for clean 100% paInt16 / 48kHz compatibility, fallback to 0
            preferred_api = 0
            for api_idx in range(pa.get_host_api_count()):
                try:
                    api_info = pa.get_host_api_info_by_index(api_idx)
                    if "mme" in api_info.get("name", "").lower():
                        preferred_api = api_idx
                        break
                except Exception:
                    continue

            seen_norm_names = set()
            norm_indices = collections.defaultdict(list)
            candidates = []
            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                    max_out = int(info.get("maxOutputChannels", 0))
                    is_loopback = bool(info.get("isLoopbackDevice", False))
                    if max_out <= 0 or is_loopback:
                        continue

                    name = info.get("name", f"Output {i}").strip()
                    name_lower = name.lower()
                    if any(mk in name_lower for mk in _MAPPER_KEYWORDS):
                        continue

                    host_api = int(info.get("hostApi", 0))
                    norm_name = normalize_device_name(name)
                    is_virt = is_virtual_device_name(name)
                    is_def = bool(default_name_norm and (norm_name == default_name_norm))

                    norm_indices[norm_name].append(i)
                    candidates.append({
                        "index": i,
                        "name": name,
                        "norm_name": norm_name,
                        "host_api": host_api,
                        "sample_rate": int(info.get("defaultSampleRate", SAMPLE_RATE)),
                        "channels": max_out,
                        "is_default": is_def,
                        "is_virtual": is_virt,
                    })
                except Exception:
                    continue

            # Sort so preferred_api (MME) comes first before deduplicating by normalized name
            candidates.sort(key=lambda c: (c["host_api"] != preferred_api, c["host_api"]))
            for c in candidates:
                key = c["norm_name"]
                if key in seen_norm_names:
                    continue
                seen_norm_names.add(key)
                devices.append({
                    "index": c["index"],
                    "name": c["name"],
                    "sample_rate": c["sample_rate"],
                    "channels": c["channels"],
                    "is_default": c["is_default"],
                    "is_virtual": c["is_virtual"],
                    "alt_indices": norm_indices[key],
                })

            # Sort physical non-virtual speakers first so PC Playback Dev defaults to real speakers/headphones
            devices.sort(key=lambda d: (d.get("is_virtual", False), not d.get("is_default", False), d.get("name", "")))
        finally:
            pa.terminate()
    except Exception as exc:
        logger.error("Error enumerating output devices: %s", exc)

    return devices


def _open_pyaudio_output_with_fallback(
    pa: "pyaudio.PyAudio",
    device_index: Optional[object],
    sample_rate: int,
    channels: int,
    frames_per_buffer: int,
) -> tuple:
    """
    Attempts to open a PyAudio output stream.
    Collects candidate device indices matching the target device name across host APIs.
    Tries format=paInt16 across candidate sample rates, then falls back to format=paFloat32.
    Returns: (stream, open_rate, dev_channels, use_float32)
    """
    candidate_indices = []
    if isinstance(device_index, int):
        candidate_indices.append(device_index)
        try:
            target_norm = normalize_device_name(pa.get_device_info_by_index(device_index).get("name", ""))
            for i in range(pa.get_device_count()):
                if i != device_index:
                    info = pa.get_device_info_by_index(i)
                    if int(info.get("maxOutputChannels", 0)) > 0 and not info.get("isLoopbackDevice", False):
                        if normalize_device_name(info.get("name", "")) == target_norm:
                            candidate_indices.append(i)
        except Exception:
            pass
    else:
        candidate_indices.append(None)

    for idx in candidate_indices:
        dev_def_rate = sample_rate
        dev_channels = channels
        try:
            info = pa.get_device_info_by_index(idx) if idx is not None else pa.get_default_output_device_info()
            dev_def_rate = int(info.get("defaultSampleRate", sample_rate))
            dev_channels = min(2, max(1, int(info.get("maxOutputChannels", channels))))
        except Exception:
            pass

        # 1. Try paInt16 at (sample_rate, dev_def_rate, 44100)
        for r in (sample_rate, dev_def_rate, 44100):
            try:
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=dev_channels,
                    rate=r,
                    output=True,
                    output_device_index=idx,
                    frames_per_buffer=frames_per_buffer,
                )
                logger.info("Opened PyAudio output device idx=%s rate=%d ch=%d (paInt16)", idx, r, dev_channels)
                return stream, r, dev_channels, False
            except Exception:
                continue

        # 2. Try paFloat32 fallback at (dev_def_rate, sample_rate, 44100)
        for r in (dev_def_rate, sample_rate, 44100):
            try:
                stream = pa.open(
                    format=pyaudio.paFloat32,
                    channels=dev_channels,
                    rate=r,
                    output=True,
                    output_device_index=idx,
                    frames_per_buffer=frames_per_buffer,
                )
                logger.info("Opened PyAudio output device idx=%s rate=%d ch=%d (paFloat32 fallback)", idx, r, dev_channels)
                return stream, r, dev_channels, True
            except Exception:
                continue

    return None, sample_rate, channels, False


class PCDelayedAudioPlayer:
    """
    Plays captured audio to a physical PC output device with a drift-free configurable delay
    (0 to 250 ms) to achieve exact acoustic synchronization with the Phone speaker.
    """

    def __init__(
        self,
        device_index: Optional[object] = None,
        delay_ms: float = 35.0,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
    ):
        self._device_index = device_index
        self._delay_ms = max(0.0, min(300.0, float(delay_ms)))
        self._sample_rate = sample_rate
        self._channels = channels
        self._dev_rate = sample_rate
        self._dev_channels = channels
        self._use_float32: bool = False
        self._resampler = None

        self._pa: Optional["pyaudio.PyAudio"] = None
        self._stream = None
        self._pacat_proc: Optional[subprocess.Popen] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._buffer: collections.deque = collections.deque(maxlen=150)
        self._data_event = threading.Event()
        self._volume_gain: float = 1.0
        self._muted: bool = False
        self._lock = threading.Lock()

    def set_delay_ms(self, delay_ms: float) -> None:
        with self._lock:
            self._delay_ms = max(0.0, min(300.0, float(delay_ms)))

    def set_smooth_delay_ms(self, target_delay_ms: float) -> float:
        with self._lock:
            clamped = max(0.0, min(300.0, float(target_delay_ms)))
            # Smooth EMA filter so auto-sync never causes abrupt jumps
            if abs(clamped - self._delay_ms) > 25.0:
                self._delay_ms = clamped
            else:
                self._delay_ms = 0.75 * self._delay_ms + 0.25 * clamped
            return self._delay_ms

    def get_delay_ms(self) -> float:
        with self._lock:
            return self._delay_ms

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
        if not self._running or not pcm_bytes:
            return
        now = time.monotonic()
        with self._lock:
            self._buffer.append((now, pcm_bytes))
        self._data_event.set()

    def start(self) -> bool:
        if self._running:
            return True

        if _IS_LINUX and isinstance(self._device_index, str) and self._device_index.startswith("pulse:"):
            sink = self._device_index.split("pulse:", 1)[1]
            if shutil.which("pacat"):
                try:
                    self._pacat_proc = subprocess.Popen(
                        [
                            "pacat", "--playback", f"--device={sink}",
                            f"--rate={self._sample_rate}", "--format=s16le",
                            f"--channels={self._channels}", "--latency-msec=12",
                        ],
                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                    )
                    self._dev_rate = self._sample_rate
                    self._dev_channels = self._channels
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
            frames_buf = max(480, int(self._sample_rate * 0.020))
            stream, rate, ch, use_f32 = _open_pyaudio_output_with_fallback(
                self._pa, dev_idx, self._sample_rate, self._channels, frames_buf
            )
            if stream is None:
                logger.error("PCDelayedAudioPlayer could not open device_index=%s", dev_idx)
                self.stop()
                return False

            self._stream = stream
            self._dev_rate = rate
            self._dev_channels = ch
            self._use_float32 = use_f32

            if _SOXR_AVAILABLE and self._dev_rate != self._sample_rate:
                try:
                    self._resampler = soxr.ResampleStream(
                        self._sample_rate, self._dev_rate, self._dev_channels, dtype="float32", quality="HQ"
                    )
                except Exception:
                    self._resampler = None
            else:
                self._resampler = None

            self._running = True
            self._thread = threading.Thread(target=self._run, name="PCDelayedAudioPlayer", daemon=True)
            self._thread.start()
            logger.info(
                "PCDelayedAudioPlayer started (device=%s, rate=%d, ch=%d, f32=%s, delay=%.1fms)",
                dev_idx, self._dev_rate, self._dev_channels, self._use_float32, self._delay_ms
            )
            return True
        except Exception as exc:
            logger.error("Failed to start PCDelayedAudioPlayer: %s", exc)
            self.stop()
            return False

    def stop(self) -> None:
        self._running = False
        self._data_event.set()
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
        self._resampler = None
        with self._lock:
            self._buffer.clear()

    def _format_for_device(self, pcm_bytes: bytes, muted: bool, gain: float) -> bytes:
        if muted:
            n_frames = len(pcm_bytes) // 4
            out_frames = int(round(n_frames * self._dev_rate / self._sample_rate))
            return b"\x00" * (out_frames * self._dev_channels * 2)

        arr = np.frombuffer(pcm_bytes, dtype="<i2")
        if len(arr) == 0:
            return pcm_bytes

        if abs(gain - 1.0) > 0.01:
            arr = np.clip(arr.astype(np.float32) * gain, -32768, 32767).astype("<i2")

        if self._dev_rate == self._sample_rate and self._dev_channels == 2:
            return arr.tobytes()

        pcm_2d = arr.reshape(-1, 2)
        if self._dev_channels == 1:
            pcm_2d = pcm_2d.mean(axis=1, keepdims=True).astype("<i2")

        if self._dev_rate != self._sample_rate:
            if _SOXR_AVAILABLE:
                if self._resampler is not None:
                    try:
                        resampled = self._resampler.resample_chunk(pcm_2d.astype(np.float32), last=False)
                        return np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
                    except Exception:
                        pass
                resampled = soxr.resample(
                    pcm_2d.astype(np.float32), self._sample_rate, self._dev_rate, quality="HQ"
                )
                return np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
            in_len = len(pcm_2d)
            out_len = int(round(in_len * self._dev_rate / self._sample_rate))
            x_old = np.arange(in_len, dtype=np.float32) / max(1, in_len)
            x_new = np.arange(out_len, dtype=np.float32) / max(1, out_len)
            resampled = np.zeros((out_len, self._dev_channels), dtype=np.float32)
            for c in range(self._dev_channels):
                resampled[:, c] = np.interp(x_new, x_old, pcm_2d[:, c])
            return np.clip(resampled, -32768, 32767).astype("<i2").tobytes()

        return pcm_2d.tobytes()

    def _run(self) -> None:
        while self._running:
            now = time.monotonic()
            ready_frames = []
            with self._lock:
                delay_ms = self._delay_ms
                target_delay_sec = delay_ms / 1000.0
                muted = self._muted
                gain = self._volume_gain

                if delay_ms <= 2.0:
                    # PC_ONLY mode: play frames immediately without delay
                    while self._buffer and len(ready_frames) < 4:
                        ready_frames.append(self._buffer.popleft()[1])
                else:
                    # BOTH_SYNCED mode: drift-locked delay line
                    # If oldest frame exceeds target_delay + 35ms, drop with crossfade to prevent clicks
                    if len(self._buffer) > 2 and (now - self._buffer[0][0]) > (target_delay_sec + 0.035):
                        dropped = self._buffer.popleft()[1]
                        next_t, next_pcm = self._buffer[0]
                        try:
                            fade_len = min(32, len(dropped) // 4, len(next_pcm) // 4)
                            if fade_len > 0:
                                arr_prev = np.frombuffer(dropped, dtype="<i2")
                                arr_next = np.frombuffer(next_pcm, dtype="<i2").copy()
                                alpha = np.linspace(0.0, 1.0, fade_len * 2, dtype=np.float32)
                                tail = arr_prev[-fade_len * 2:].astype(np.float32)
                                head = arr_next[:fade_len * 2].astype(np.float32)
                                arr_next[:fade_len * 2] = np.rint(tail * (1.0 - alpha) + head * alpha).astype("<i2")
                                self._buffer[0] = (next_t, arr_next.tobytes())
                        except Exception:
                            pass

                    # Pop up to 4 ready frames (up to 20ms) into a single contiguous write
                    while self._buffer and len(ready_frames) < 4 and (now - self._buffer[0][0]) >= target_delay_sec:
                        ready_frames.append(self._buffer.popleft()[1])

            if ready_frames:
                combined_pcm = b"".join(ready_frames)
                out_bytes = self._format_for_device(combined_pcm, muted, gain)
                try:
                    if self._use_float32:
                        out_write = (np.frombuffer(out_bytes, dtype="<i2").astype(np.float32) / 32768.0).tobytes()
                    else:
                        out_write = out_bytes

                    if self._pacat_proc and self._pacat_proc.stdin:
                        self._pacat_proc.stdin.write(out_bytes)
                    elif self._stream:
                        self._stream.write(out_write)
                except Exception as exc:
                    logger.debug("PCDelayedAudioPlayer write error: %s", exc)
            else:
                self._data_event.wait(timeout=0.003)
                self._data_event.clear()


class PhoneAudioReceiver:
    """
    Listens on UDP port 5006 for incoming audio from the Phone (Mic),
    decodes PCM16 or Opus without dropping burst packets, and plays it with low latency
    to the selected PC audio output / virtual mic device.
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
        self._use_float32: bool = False
        self._resampler = None
        self._opus_decoder: Optional[OpusDecoder] = None

        self._running = False
        self._muted = True
        self._manual_mute = False
        self._rx_thread: Optional[threading.Thread] = None
        self._play_thread: Optional[threading.Thread] = None
        self._pcm_queue: collections.deque = collections.deque(maxlen=40)
        self._is_prebuffering = True
        self._last_rx_time = 0.0
        self._last_valid_frame = b"\x00" * 960
        self._last_channels = CHANNELS
        self._queue_event = threading.Event()
        self._lock = threading.Lock()
        self._pps_count = 0
        self._last_pps_time = 0.0
        self.peak_level: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    def set_muted(self, muted: bool, manual: bool = True) -> None:
        self._muted = bool(muted)
        if manual:
            self._manual_mute = bool(muted)
        if self._muted:
            self.peak_level = 0.0
            self._pcm_queue.clear()
            self._is_prebuffering = True

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
        frames_buf = max(480, int(SAMPLE_RATE * 0.020))
        stream, rate, ch, use_f32 = _open_pyaudio_output_with_fallback(
            self._pa, dev_idx, SAMPLE_RATE, CHANNELS, frames_buf
        )
        if stream is not None:
            self._stream = stream
            self._dev_rate = rate
            self._dev_channels = ch
            self._use_float32 = use_f32
            if _SOXR_AVAILABLE and self._dev_rate != SAMPLE_RATE:
                try:
                    self._resampler = soxr.ResampleStream(
                        SAMPLE_RATE, self._dev_rate, self._dev_channels, dtype="float32", quality="HQ"
                    )
                except Exception:
                    self._resampler = None
            else:
                self._resampler = None
            return True
        return False

    def _close_audio_sink(self) -> None:
        self._resampler = None
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
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 524288)
            self._sock.bind(("", self._port))
            self._sock.settimeout(0.20)

            with self._lock:
                if not self._open_audio_sink():
                    logger.warning("PhoneAudioReceiver could not open audio output device")

            self._opus_decoder = OpusDecoder(SAMPLE_RATE, CHANNELS)
            self._running = True
            self._is_prebuffering = True
            self._last_rx_time = 0.0
            self._last_valid_frame = b"\x00" * 960
            self._pcm_queue.clear()
            self._pps_count = 0
            self._last_pps_time = time.monotonic()
            self._rx_thread = threading.Thread(target=self._rx_loop, name="PhoneAudioRxSocket", daemon=True)
            self._play_thread = threading.Thread(target=self._play_loop, name="PhoneAudioRxPlay", daemon=True)
            self._rx_thread.start()
            self._play_thread.start()
            logger.info("PhoneAudioReceiver listening on UDP port %d", self._port)
            return True
        except Exception as exc:
            logger.error("Failed to start PhoneAudioReceiver: %s", exc)
            self.stop()
            return False

    def stop(self) -> None:
        self._running = False
        self._queue_event.set()
        if self._rx_thread and self._rx_thread is not threading.current_thread():
            self._rx_thread.join(timeout=1.0)
            self._rx_thread = None
        if self._play_thread and self._play_thread is not threading.current_thread():
            self._play_thread.join(timeout=1.0)
            self._play_thread = None
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
        self._pcm_queue.clear()
        self.peak_level = 0.0
        if self._on_stats:
            try:
                self._on_stats(0)
            except Exception:
                pass
        logger.info("PhoneAudioReceiver stopped")

    def _rx_loop(self) -> None:
        """Dedicated non-blocking UDP socket receiver so zero mic packets are dropped."""
        while self._running:
            try:
                data, _ = self._sock.recvfrom(2048)
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

                    if decoded_pcm:
                        self._pps_count += 1
                        self._last_rx_time = time.monotonic()
                        if not self._manual_mute:
                            self._muted = False
                        if not self._muted:
                            pcm_arr = np.frombuffer(decoded_pcm, dtype="<i2")
                            if len(pcm_arr) > 0:
                                self.peak_level = float(np.max(np.abs(pcm_arr))) / 32768.0
                            self._pcm_queue.append((decoded_pcm, channels))
                            self._queue_event.set()
            except socket.timeout:
                self.peak_level = 0.0
            except Exception as exc:
                if self._running:
                    logger.debug("PhoneAudioReceiver rx error: %s", exc)

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

    def _render_and_write(self, pcm_data: bytes, channels: int) -> None:
        try:
            out_pcm = pcm_data
            if self._dev_rate != SAMPLE_RATE or self._dev_channels != channels:
                pcm_arr = np.frombuffer(pcm_data, dtype="<i2")
                in_ch = channels if channels in (1, 2) else 2
                pcm_2d = pcm_arr.reshape(-1, in_ch)
                if self._dev_channels == 1 and in_ch == 2:
                    pcm_2d = pcm_2d.mean(axis=1, keepdims=True).astype(np.int16)
                elif self._dev_channels == 2 and in_ch == 1:
                    pcm_2d = np.repeat(pcm_2d, 2, axis=1)

                if self._dev_rate != SAMPLE_RATE:
                    if _SOXR_AVAILABLE:
                        if self._resampler is not None:
                            try:
                                resampled = self._resampler.resample_chunk(pcm_2d.astype(np.float32), last=False)
                                out_pcm = np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
                            except Exception:
                                resampled = soxr.resample(
                                    pcm_2d.astype(np.float32), SAMPLE_RATE, self._dev_rate, quality="HQ"
                                )
                                out_pcm = np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
                        else:
                            resampled = soxr.resample(
                                pcm_2d.astype(np.float32), SAMPLE_RATE, self._dev_rate, quality="HQ"
                            )
                            out_pcm = np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
                    else:
                        in_len = len(pcm_2d)
                        out_len = int(round(in_len * self._dev_rate / SAMPLE_RATE))
                        x_old = np.arange(in_len, dtype=np.float32) / max(1, in_len)
                        x_new = np.arange(out_len, dtype=np.float32) / max(1, out_len)
                        resampled = np.zeros((out_len, self._dev_channels), dtype=np.float32)
                        for c in range(self._dev_channels):
                            resampled[:, c] = np.interp(x_new, x_old, pcm_2d[:, c])
                        out_pcm = np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
                else:
                    out_pcm = pcm_2d.astype("<i2").tobytes()

            if self._use_float32:
                out_write = (np.frombuffer(out_pcm, dtype="<i2").astype(np.float32) / 32768.0).tobytes()
            else:
                out_write = out_pcm

            with self._lock:
                if self._pacat_proc and self._pacat_proc.stdin:
                    self._pacat_proc.stdin.write(out_pcm)
                elif self._stream:
                    self._stream.write(out_write)
        except Exception as exc:
            if self._running:
                logger.debug("PhoneAudioReceiver write error: %s", exc)

    def _play_loop(self) -> None:
        """Dedicated audio playback thread consuming decoded mic frames smoothly."""
        while self._running:
            try:
                if self._is_prebuffering:
                    if len(self._pcm_queue) < 6:
                        self._queue_event.wait(timeout=0.02)
                        self._queue_event.clear()
                        continue
                    self._is_prebuffering = False

                if self._pcm_queue:
                    while len(self._pcm_queue) > 18:
                        try:
                            self._pcm_queue.popleft()
                        except IndexError:
                            break

                    try:
                        decoded_pcm, channels = self._pcm_queue.popleft()
                    except IndexError:
                        continue

                    if self._muted or not decoded_pcm:
                        continue

                    self._last_valid_frame = decoded_pcm
                    self._last_channels = channels
                    self._render_and_write(decoded_pcm, channels)
                else:
                    now = time.monotonic()
                    if (now - self._last_rx_time) < 0.150 and not self._muted:
                        # Wi-Fi micro-jitter gap during active speech: conceal 5ms gap by decaying last frame (* 0.80)
                        arr = np.frombuffer(self._last_valid_frame, dtype="<i2").astype(np.float32) * 0.80
                        self._last_valid_frame = arr.astype("<i2").tobytes()
                        self._render_and_write(self._last_valid_frame, getattr(self, "_last_channels", CHANNELS))
                    else:
                        self._is_prebuffering = True
                        self._queue_event.wait(timeout=0.02)
                        self._queue_event.clear()
            except Exception as exc:
                logger.debug("PhoneAudioReceiver play error: %s", exc)
