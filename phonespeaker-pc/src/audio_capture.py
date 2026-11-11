"""
audio_capture.py — WASAPI loopback audio capture via PyAudioWPatch.

Captures system audio (loopback) and delivers resampled 48kHz / stereo /
int16 frames via a callback or queue interface.
"""

import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

_IS_LINUX = sys.platform.startswith("linux")
_IS_MAC = (sys.platform == "darwin")

# Target output format
TARGET_RATE = 48000
TARGET_CHANNELS = 2
TARGET_DTYPE = np.int16

# Attempt to import optional deps
try:
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        import pyaudio
    _PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None
    _PYAUDIO_AVAILABLE = False
    logger.warning("PyAudioWPatch not available — audio capture disabled")

try:
    import soxr
    _SOXR_AVAILABLE = True
except ImportError:
    soxr = None
    _SOXR_AVAILABLE = False
    logger.warning("soxr not available — using numpy resampler (lower quality)")


# ---------------------------------------------------------------------------
# Resampling helpers
# ---------------------------------------------------------------------------

def _resample_numpy(data: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    """Linear interpolation resampler (fallback when soxr is not available)."""
    if in_rate == out_rate:
        return data
    n_in = data.shape[0]
    n_out = int(round(n_in * out_rate / in_rate))
    x_in = np.linspace(0, 1, n_in)
    x_out = np.linspace(0, 1, n_out)
    if data.ndim == 1:
        return np.interp(x_out, x_in, data.astype(np.float32)).astype(data.dtype)
    # multi-channel
    out = np.zeros((n_out, data.shape[1]), dtype=data.dtype)
    for ch in range(data.shape[1]):
        out[:, ch] = np.interp(x_out, x_in, data[:, ch].astype(np.float32)).astype(data.dtype)
    return out


def sample_format_name(fmt: Optional[int]) -> str:
    if not _PYAUDIO_AVAILABLE or fmt is None:
        return str(fmt)

    mapping = {
        pyaudio.paFloat32: "paFloat32",
        pyaudio.paInt32: "paInt32",
        pyaudio.paInt24: "paInt24",
        pyaudio.paInt16: "paInt16",
        pyaudio.paInt8: "paInt8",
        pyaudio.paUInt8: "paUInt8",
    }
    return mapping.get(fmt, f"unknown({fmt})")


def convert_to_target(
    raw: bytes,
    in_rate: int,
    in_channels: int,
    sample_format: int,
    resampler: Optional[object] = None,
) -> bytes:
    """
    Convert raw audio bytes from the capture device format into
    48kHz / stereo / int16 interleaved bytes.
    """
    if not raw:
        return b""

    if in_channels <= 0:
        raise ValueError(f"Invalid input channel count: {in_channels}")

    if _PYAUDIO_AVAILABLE and sample_format == pyaudio.paFloat32:
        usable_bytes = (len(raw) // (4 * in_channels)) * (4 * in_channels)
        if usable_bytes == 0:
            return b""
        arr = np.frombuffer(raw[:usable_bytes], dtype="<f4").astype(np.float32, copy=False)

    elif _PYAUDIO_AVAILABLE and sample_format == pyaudio.paInt32:
        usable_bytes = (len(raw) // (4 * in_channels)) * (4 * in_channels)
        if usable_bytes == 0:
            return b""
        arr = (
            np.frombuffer(raw[:usable_bytes], dtype="<i4").astype(np.float32)
            / 2147483648.0
        )

    elif _PYAUDIO_AVAILABLE and sample_format == pyaudio.paInt24:
        raw_arr = np.frombuffer(raw, dtype=np.uint8)
        bytes_per_frame = 3 * in_channels
        usable_bytes = (raw_arr.size // bytes_per_frame) * bytes_per_frame
        if usable_bytes == 0:
            return b""

        raw_arr = raw_arr[:usable_bytes].reshape(-1, 3)

        int32_arr = (
            raw_arr[:, 0].astype(np.int32)
            | (raw_arr[:, 1].astype(np.int32) << 8)
            | (raw_arr[:, 2].astype(np.int32) << 16)
        )

        int32_arr = np.where(
            int32_arr & 0x800000,
            int32_arr | ~0xFFFFFF,
            int32_arr,
        ).astype(np.int32)

        arr = int32_arr.astype(np.float32) / 8388608.0

    elif _PYAUDIO_AVAILABLE and sample_format == pyaudio.paInt16:
        usable_bytes = (len(raw) // (2 * in_channels)) * (2 * in_channels)
        if usable_bytes == 0:
            return b""
        arr = (
            np.frombuffer(raw[:usable_bytes], dtype="<i2").astype(np.float32)
            / 32768.0
        )

    elif _PYAUDIO_AVAILABLE and sample_format == pyaudio.paInt8:
        usable_bytes = (len(raw) // in_channels) * in_channels
        if usable_bytes == 0:
            return b""
        arr = (
            np.frombuffer(raw[:usable_bytes], dtype=np.int8).astype(np.float32)
            / 128.0
        )

    elif _PYAUDIO_AVAILABLE and sample_format == pyaudio.paUInt8:
        usable_bytes = (len(raw) // in_channels) * in_channels
        if usable_bytes == 0:
            return b""
        arr = (
            np.frombuffer(raw[:usable_bytes], dtype=np.uint8).astype(np.float32)
            - 128.0
        ) / 128.0

    else:
        raise ValueError(
            f"Unsupported PyAudio sample format: {sample_format}"
        )

    # Reshape to (frames, in_channels)
    arr = arr.reshape(-1, in_channels)

    # Channel conversion
    if in_channels == 1:
        # Mono -> Stereo
        arr = np.column_stack((arr[:, 0], arr[:, 0]))
    elif in_channels == 2:
        pass
    elif in_channels >= 6:
        # 5.1/7.1 downmix: L=FL+0.707*FC+0.707*RL, R=FR+0.707*FC+0.707*RR
        left = arr[:, 0] + 0.7071 * arr[:, 2] + 0.7071 * arr[:, 4]
        right = arr[:, 1] + 0.7071 * arr[:, 2] + 0.7071 * arr[:, 5]
        arr = np.column_stack((left * 0.5, right * 0.5))
    else:
        left = arr[:, 0] + (np.sum(arr[:, 2:], axis=1) * (0.5 / (in_channels - 2)))
        right = arr[:, 1] + (np.sum(arr[:, 2:], axis=1) * (0.5 / (in_channels - 2)))
        arr = np.column_stack((left * 0.7, right * 0.7))

    # Resample to 48000 Hz if needed
    if in_rate != TARGET_RATE:
        if resampler is not None:
            try:
                arr = resampler.resample_chunk(arr.astype(np.float32, copy=False), last=False)
            except Exception:
                arr = soxr.resample(arr, in_rate, TARGET_RATE, quality="HQ") if _SOXR_AVAILABLE else _resample_numpy(arr, in_rate, TARGET_RATE)
        elif _SOXR_AVAILABLE:
            arr = soxr.resample(arr, in_rate, TARGET_RATE, quality="HQ")
        else:
            arr = _resample_numpy(arr, in_rate, TARGET_RATE)

    # Use np.nan_to_num() to remove NaN and Infinity from malformed Float32 data
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
    arr = np.clip(arr, -1.0, 1.0)
    int16_arr = np.rint(arr * 32767.0).astype("<i2")
    return int16_arr.tobytes()


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

def _list_linux_pulse_monitors() -> List[dict]:
    """List PipeWire / PulseAudio monitor sources on Linux using pactl."""
    if not _IS_LINUX or not shutil.which("pactl"):
        return []
    devices = []
    try:
        default_sink = ""
        res_def = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=2
        )
        if res_def.returncode == 0:
            default_sink = res_def.stdout.strip()

        res = subprocess.run(
            ["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=2
        )
        if res.returncode == 0:
            for idx, line in enumerate(res.stdout.splitlines()):
                parts = line.split("\t")
                if len(parts) >= 2:
                    src_name = parts[1].strip()
                    if src_name.endswith(".monitor"):
                        is_def = bool(default_sink and src_name == f"{default_sink}.monitor")
                        entry = {
                            "index": f"pulse:{src_name}",
                            "name": f"Monitor: {src_name}",
                            "sample_rate": TARGET_RATE,
                            "channels": TARGET_CHANNELS,
                            "is_default": is_def,
                        }
                        if is_def:
                            devices.insert(0, entry)
                        else:
                            devices.append(entry)
    except Exception as exc:
        logger.debug("Error listing Linux PulseAudio/PipeWire monitors: %s", exc)
    return devices


def list_loopback_devices() -> List[dict]:
    """Return WASAPI loopback devices with the active default playback loopback first (or Linux monitors)."""
    if _IS_LINUX:
        monitors = _list_linux_pulse_monitors()
        if monitors:
            return monitors
    if not _PYAUDIO_AVAILABLE:
        return []
    devices = []
    default_loopback_idx = None
    try:
        pa = pyaudio.PyAudio()
        try:
            if hasattr(pa, "get_default_wasapi_loopback"):
                try:
                    def_info = pa.get_default_wasapi_loopback()
                    default_loopback_idx = def_info.get("index")
                except Exception:
                    pass

            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                    is_loopback = info.get("isLoopbackDevice", False)
                    if is_loopback:
                        entry = {
                            "index": i,
                            "name": info.get("name", f"Device {i}"),
                            "sample_rate": int(info.get("defaultSampleRate", TARGET_RATE)),
                            "channels": max(1, int(info.get("maxInputChannels", 2))),
                            "is_default": (i == default_loopback_idx),
                        }
                        if i == default_loopback_idx:
                            devices.insert(0, entry)
                        else:
                            devices.append(entry)
                except Exception:
                    continue

            if _IS_MAC and not devices:
                virtual_keywords = ("blackhole", "soundflower", "loopback", "virtual", "phonespeaker", "aggregate", "multi-output")
                all_inputs = []
                for i in range(pa.get_device_count()):
                    try:
                        info = pa.get_device_info_by_index(i)
                        in_channels = int(info.get("maxInputChannels", 0))
                        if in_channels > 0:
                            dev_name = info.get("name", f"Device {i}")
                            entry = {
                                "index": i,
                                "name": dev_name,
                                "sample_rate": int(info.get("defaultSampleRate", TARGET_RATE)),
                                "channels": max(1, in_channels),
                                "is_default": False,
                            }
                            if any(k in dev_name.lower() for k in virtual_keywords):
                                devices.append(entry)
                            else:
                                all_inputs.append(entry)
                    except Exception:
                        continue
                if not devices:
                    devices = all_inputs
        finally:
            pa.terminate()
    except Exception as exc:
        logger.error("Error enumerating loopback devices: %s", exc)
    return devices


def list_input_devices() -> List[dict]:
    """Return physical microphone input devices (excluding loopback)."""
    if _IS_LINUX and shutil.which("pactl"):
        devices = []
        try:
            res = subprocess.run(
                ["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=2
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        src_name = parts[1].strip()
                        if not src_name.endswith(".monitor"):
                            devices.append({
                                "index": f"pulse:{src_name}",
                                "name": src_name,
                                "sample_rate": TARGET_RATE,
                                "channels": TARGET_CHANNELS,
                                "is_default": False,
                            })
            if devices:
                return devices
        except Exception:
            pass
    if not _PYAUDIO_AVAILABLE:
        return []
    devices = []
    try:
        pa = pyaudio.PyAudio()
        try:
            default_in_idx = None
            try:
                def_info = pa.get_default_input_device_info()
                default_in_idx = def_info.get("index")
            except Exception:
                pass

            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                    max_in = info.get("maxInputChannels", 0)
                    is_loopback = info.get("isLoopbackDevice", False)
                    if max_in > 0 and not is_loopback:
                        entry = {
                            "index": i,
                            "name": info.get("name", f"Mic {i}"),
                            "sample_rate": int(info.get("defaultSampleRate", TARGET_RATE)),
                            "channels": max(1, max_in),
                            "is_default": (i == default_in_idx),
                        }
                        if i == default_in_idx:
                            devices.insert(0, entry)
                        else:
                            devices.append(entry)
                except Exception:
                    continue
        finally:
            pa.terminate()
    except Exception as exc:
        logger.error("Error enumerating input devices: %s", exc)
    return devices


# ---------------------------------------------------------------------------
# Capture engine
# ---------------------------------------------------------------------------

CHUNK_FRAMES = 240  # 5ms at 48kHz


class AudioCapture:
    """
    WASAPI loopback capture that feeds 5ms int16 stereo frames into a queue.

    Usage:
        capture = AudioCapture(device_index=idx, output_queue=q)
        capture.start()
        ...
        capture.stop()
    """

    def __init__(
        self,
        device_index: Optional[int],
        output_queue: "queue.Queue[bytes]",
        on_error: Optional[Callable[[str], None]] = None,
        frame_ms: int = 5,
    ):
        self._device_index = device_index
        self._output_queue = output_queue
        self._on_error = on_error
        self._frame_ms = frame_ms
        self._remainder = bytearray()
        self.active_device_name: str = ""
        self._resampler = None
        self.peak_level: float = 0.0
        self._stream = None
        self._pa: Optional["pyaudio.PyAudio"] = None
        self._parec_proc: Optional[subprocess.Popen] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._in_rate = TARGET_RATE
        self._in_channels = TARGET_CHANNELS
        self._in_format: Optional[int] = (
            pyaudio.paFloat32 if _PYAUDIO_AVAILABLE else None
        )
        self._last_diag_time: float = 0.0

    def _report_error(self, msg: str) -> None:
        logger.error(msg)
        if self._on_error:
            try:
                self._on_error(msg)
            except Exception:
                pass

    def _start_linux_parec(self, source_name: str) -> bool:
        """Capture 48kHz stereo s16le directly from PipeWire/PulseAudio via parec."""
        parec_bin = shutil.which("parec")
        if not parec_bin:
            self._report_error("parec not found. Install pulseaudio-utils or pipewire-pulse.")
            return False

        cmd = [
            parec_bin,
            "--format=s16le",
            f"--rate={TARGET_RATE}",
            f"--channels={TARGET_CHANNELS}",
            "--latency-msec=5",
        ]
        if source_name and source_name != "default" and source_name != "@DEFAULT_MONITOR@":
            cmd.append(f"--device={source_name}")

        try:
            self._parec_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
            )
            self.active_device_name = source_name
            self._running = True
            self._thread = threading.Thread(
                target=self._linux_parec_loop, name="LinuxParecCapture", daemon=True
            )
            self._thread.start()
            logger.info("Linux PipeWire/PulseAudio capture started: source=%s", source_name)
            return True
        except Exception as exc:
            self._report_error(f"Failed to start parec capture: {exc}")
            return False

    def _linux_parec_loop(self) -> None:
        while self._running and getattr(self, "_parec_proc", None):
            chunk_bytes = ((TARGET_RATE * self._frame_ms) // 1000) * TARGET_CHANNELS * 2
            try:
                raw = self._parec_proc.stdout.read(chunk_bytes)
                if not raw:
                    break
                int16_arr = np.frombuffer(raw, dtype=np.int16)
                if len(int16_arr) > 0:
                    self.peak_level = float(np.max(np.abs(int16_arr))) / 32768.0
                self._remainder.extend(raw)
                while len(self._remainder) >= chunk_bytes:
                    frame = bytes(self._remainder[:chunk_bytes])
                    del self._remainder[:chunk_bytes]
                    if self._output_queue.qsize() < 50:
                        self._output_queue.put_nowait(frame)
            except Exception as exc:
                if self._running:
                    logger.debug("Linux parec read error: %s", exc)
                break

    def start(self) -> bool:
        """Open the capture stream. Returns True on success."""
        with self._lock:
            if self._running:
                return True

            if _IS_LINUX and shutil.which("parec"):
                if self._device_index is None or isinstance(self._device_index, str):
                    src = "@DEFAULT_MONITOR@"
                    if isinstance(self._device_index, str) and self._device_index.startswith("pulse:"):
                        src = self._device_index.split("pulse:", 1)[1]
                    else:
                        mons = _list_linux_pulse_monitors()
                        if mons:
                            src = str(mons[0]["index"]).replace("pulse:", "")
                    return self._start_linux_parec(src)

            if not _PYAUDIO_AVAILABLE:
                self._report_error("PyAudioWPatch is not installed — cannot capture audio.")
                return False

            try:
                self._pa = pyaudio.PyAudio()
                # Determine device info
                if self._device_index is None:
                    # Try to find first loopback device
                    devices = list_loopback_devices()
                    if not devices:
                        self._report_error("No WASAPI loopback devices found.")
                        self._pa.terminate()
                        self._pa = None
                        return False
                    self._device_index = devices[0]["index"]

                info = self._pa.get_device_info_by_index(self._device_index)
                self.active_device_name = info.get("name", f"Device {self._device_index}")
                self._in_rate = int(info.get("defaultSampleRate", TARGET_RATE))
                self._in_channels = max(1, int(info.get("maxInputChannels", 2)))

                if _SOXR_AVAILABLE and self._in_rate != TARGET_RATE:
                    try:
                        self._resampler = soxr.ResampleStream(self._in_rate, TARGET_RATE, TARGET_CHANNELS, dtype="float32", quality="HQ")
                    except Exception:
                        self._resampler = None
                else:
                    self._resampler = None

                # Choose format: prefer float32, fall back to int16
                try:
                    self._pa.is_format_supported(
                        self._in_rate,
                        input_device=self._device_index,
                        input_channels=self._in_channels,
                        input_format=pyaudio.paFloat32,
                    )
                    self._in_format = pyaudio.paFloat32
                except Exception:
                    self._in_format = pyaudio.paInt16

                # Chunk size in input frames
                chunk = max(64, (self._in_rate * self._frame_ms) // 1000)

                open_kwargs = dict(
                    format=self._in_format,
                    channels=self._in_channels,
                    rate=self._in_rate,
                    input=True,
                    input_device_index=self._device_index,
                    frames_per_buffer=chunk,
                    stream_callback=self._callback,
                )
                try:
                    # PyAudioWPatch exposes loopback endpoints directly as input devices
                    self._stream = self._pa.open(**open_kwargs)
                except TypeError:
                    self._stream = self._pa.open(**open_kwargs)
                except Exception:
                    # Fallback for legacy PyAudio WASAPI forks that require as_loopback=True
                    open_kwargs["as_loopback"] = True
                    self._stream = self._pa.open(**open_kwargs)
                self._stream.start_stream()
                self._running = True
                logger.info(
                    "Audio capture started: device=%s rate=%d channels=%d format=%s(%s)",
                    self._device_index,
                    self._in_rate,
                    self._in_channels,
                    sample_format_name(self._in_format),
                    self._in_format,
                )
                return True
            except Exception as exc:
                self._report_error(f"Failed to start audio capture: {exc}")
                if self._pa:
                    try:
                        self._pa.terminate()
                    except Exception:
                        pass
                    self._pa = None
                return False

    def _callback(
        self,
        in_data: bytes,
        frame_count: int,
        time_info: dict,
        status: int,
    ) -> Tuple[None, int]:
        """PyAudio stream callback — called from audio thread."""
        try:
            converted = convert_to_target(
                in_data, self._in_rate, self._in_channels, self._in_format, resampler=self._resampler
            )
            
            # Compute peak audio level for UI level meter
            if converted:
                int16_arr = np.frombuffer(converted, dtype=np.int16)
                if len(int16_arr) > 0:
                    self.peak_level = float(np.max(np.abs(int16_arr))) / 32768.0

            # Accumulate converted bytes to prevent boundary sample drops
            self._remainder.extend(converted)

            chunk_bytes = ((TARGET_RATE * self._frame_ms) // 1000) * TARGET_CHANNELS * 2
            expected_bytes = chunk_bytes
            while len(self._remainder) >= chunk_bytes:
                chunk = bytes(self._remainder[:chunk_bytes])
                del self._remainder[:chunk_bytes]

                if len(chunk) != expected_bytes:
                    logger.error(
                        "Dropping malformed PCM frame: expected=%d actual=%d",
                        expected_bytes,
                        len(chunk),
                    )
                    continue

                if self._output_queue.qsize() < 50:
                    self._output_queue.put_nowait(chunk)
                # else: drop oldest — bounded queue overflow

            # Periodic diagnostic log (every ~2s)
            now = time.monotonic()
            if now - self._last_diag_time >= 2.0:
                self._last_diag_time = now
                logger.info(
                    "Capture diagnostic: format=%s(%s) rate=%d channels=%d frame_bytes=%d peak=%.3f queue_size=%d",
                    sample_format_name(self._in_format),
                    self._in_format,
                    self._in_rate,
                    self._in_channels,
                    chunk_bytes,
                    self.peak_level,
                    self._output_queue.qsize(),
                )
        except Exception as exc:
            logger.error("Audio callback error: %s", exc)
        return (None, pyaudio.paContinue)

    def set_frame_ms(self, frame_ms: int) -> None:
        if frame_ms in (5, 10, 20) and frame_ms != self._frame_ms:
            with self._lock:
                self._frame_ms = frame_ms
                self._remainder.clear()
                if _SOXR_AVAILABLE and self._in_rate != TARGET_RATE:
                    try:
                        self._resampler = soxr.ResampleStream(
                            self._in_rate, TARGET_RATE, TARGET_CHANNELS, dtype="float32", quality="HQ"
                        )
                    except Exception:
                        self._resampler = None
            logger.info("AudioCapture frame_ms updated to %d ms", frame_ms)

    def stop(self) -> None:
        """Stop the capture stream and clean up."""
        with self._lock:
            self._running = False
            self._resampler = None
            if getattr(self, "_parec_proc", None) is not None:
                try:
                    self._parec_proc.terminate()
                    self._parec_proc.wait(timeout=1.0)
                except Exception:
                    pass
                self._parec_proc = None
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=1.0)
                self._thread = None
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception as exc:
                    logger.warning("Error stopping stream: %s", exc)
                self._stream = None
            if self._pa is not None:
                try:
                    self._pa.terminate()
                except Exception as exc:
                    logger.warning("Error terminating PyAudio: %s", exc)
                self._pa = None
        logger.info("Audio capture stopped")

    @property
    def is_running(self) -> bool:
        return self._running
