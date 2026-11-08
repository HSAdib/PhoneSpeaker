"""
opus_codec.py — ctypes wrapper around the bundled vendor/opus.dll.

Falls back to PCM-only mode if the DLL cannot be loaded.
"""

import ctypes
import ctypes.util
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Opus constants
# ---------------------------------------------------------------------------
OPUS_APPLICATION_VOIP = 2048
OPUS_APPLICATION_AUDIO = 2049
OPUS_APPLICATION_RESTRICTED_LOWDELAY = 2051

OPUS_SET_BITRATE_REQUEST = 4002
OPUS_SET_COMPLEXITY_REQUEST = 4010
OPUS_SET_INBAND_FEC_REQUEST = 4012
OPUS_SET_PACKET_LOSS_PERC_REQUEST = 4014
OPUS_SET_DTX_REQUEST = 4016
OPUS_SET_VBR_REQUEST = 4006
OPUS_SET_VBR_CONSTRAINT_REQUEST = 4020

OPUS_OK = 0
OPUS_AUTO = -1000


def _locate_dll() -> Optional[str]:
    """Find bundled or system libopus on Windows (opus.dll) and Linux (libopus.so.0)."""
    is_win = sys.platform.startswith("win")
    lib_names = ["opus.dll"] if is_win else ["libopus.so.0", "libopus.so"]
    candidates = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for name in lib_names:
            candidates.append(Path(meipass) / name)
            candidates.append(Path(meipass) / "vendor" / name)

    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        for name in lib_names:
            candidates.append(base / name)
            candidates.append(base / "vendor" / name)

    here = Path(__file__).resolve().parent
    for name in lib_names:
        candidates.append(here.parent / "vendor" / name)
        candidates.append(Path.cwd() / "vendor" / name)
        candidates.append(Path.cwd() / name)

    if not is_win:
        for sys_path in [
            "/usr/lib/libopus.so.0",
            "/usr/lib64/libopus.so.0",
            "/usr/lib/x86_64-linux-gnu/libopus.so.0",
            "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        ]:
            candidates.append(Path(sys_path))

    for p in candidates:
        if p.exists():
            return str(p)

    found = ctypes.util.find_library("opus")
    if found:
        return found

    return None


class OpusEncoder:
    """
    Thin ctypes wrapper around the libopus encoder.
    Thread-safe for a single encoder instance (call from one thread only).
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        channels: int = 2,
        application: int = OPUS_APPLICATION_AUDIO,
        bitrate: int = 160_000,
        frame_ms: int = 5,
        complexity: int = 5,
    ):
        self._lib: Optional[ctypes.CDLL] = None
        self._encoder = None
        self._channels = channels
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._frame_size = (sample_rate * frame_ms) // 1000  # samples per channel

        dll_path = _locate_dll()
        if dll_path is None:
            logger.warning("opus.dll not found — Opus codec unavailable; using PCM mode")
            return

        try:
            lib = ctypes.CDLL(str(dll_path))
        except OSError as exc:
            logger.error("Failed to load opus.dll from %s: %s", dll_path, exc)
            return

        # Declare function signatures
        lib.opus_encoder_get_size.restype = ctypes.c_int
        lib.opus_encoder_get_size.argtypes = [ctypes.c_int]

        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [
            ctypes.c_int,   # Fs
            ctypes.c_int,   # channels
            ctypes.c_int,   # application
            ctypes.POINTER(ctypes.c_int),  # error
        ]

        lib.opus_encode.restype = ctypes.c_int
        lib.opus_encode.argtypes = [
            ctypes.c_void_p,                # encoder
            ctypes.POINTER(ctypes.c_int16), # pcm
            ctypes.c_int,                   # frame_size
            ctypes.POINTER(ctypes.c_ubyte), # data
            ctypes.c_int32,                 # max_data_bytes
        ]

        lib.opus_encoder_ctl.restype = ctypes.c_int
        # variadic — set argtypes per-call or leave unset (platform-dependent)

        lib.opus_encoder_destroy.restype = None
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]

        lib.opus_strerror.restype = ctypes.c_char_p
        lib.opus_strerror.argtypes = [ctypes.c_int]

        error = ctypes.c_int(0)
        enc = lib.opus_encoder_create(
            ctypes.c_int(sample_rate),
            ctypes.c_int(channels),
            ctypes.c_int(application),
            ctypes.byref(error),
        )
        if error.value != OPUS_OK or enc is None:
            err_str = lib.opus_strerror(error.value)
            logger.error("opus_encoder_create failed: %s (code %d)", err_str, error.value)
            return

        self._lib = lib
        self._encoder = enc

        # Apply settings
        self._ctl(OPUS_SET_BITRATE_REQUEST, bitrate)
        self._ctl(OPUS_SET_COMPLEXITY_REQUEST, complexity)
        self._ctl(OPUS_SET_INBAND_FEC_REQUEST, 1)
        self._ctl(OPUS_SET_PACKET_LOSS_PERC_REQUEST, 10)
        self._ctl(OPUS_SET_VBR_REQUEST, 0)        # CBR
        self._ctl(OPUS_SET_VBR_CONSTRAINT_REQUEST, 1)  # constrained VBR (CBR mode)
        self._ctl(OPUS_SET_DTX_REQUEST, 0)

        logger.info(
            "Opus encoder ready: %dHz %dch %dms frames %dbps",
            sample_rate, channels, frame_ms, bitrate,
        )

    def _ctl(self, request: int, value: int) -> int:
        if self._lib is None or self._encoder is None:
            return -1
        ret = self._lib.opus_encoder_ctl(
            ctypes.c_void_p(self._encoder),
            ctypes.c_int(request),
            ctypes.c_int(value),
        )
        if ret != OPUS_OK:
            logger.warning("opus_encoder_ctl(%d, %d) returned %d", request, value, ret)
        return ret

    @property
    def available(self) -> bool:
        """True if the Opus encoder is loaded and ready."""
        return self._lib is not None and self._encoder is not None

    def encode(self, pcm_int16: bytes) -> Optional[bytes]:
        """
        Encode one frame of int16 PCM audio.

        Parameters
        ----------
        pcm_int16:
            Raw bytes of interleaved int16 stereo PCM samples.
            Must contain exactly frame_size * channels * 2 bytes.

        Returns
        -------
        Encoded Opus packet bytes, or None on error.
        """
        if not self.available:
            return None

        frame_size = self._frame_size
        expected = frame_size * self._channels * 2
        if len(pcm_int16) != expected:
            logger.error(
                "encode: expected %d bytes, got %d", expected, len(pcm_int16)
            )
            return None

        pcm_array = (ctypes.c_int16 * (frame_size * self._channels)).from_buffer_copy(
            pcm_int16
        )
        max_out = 1500
        out_buf = (ctypes.c_ubyte * max_out)()

        n = self._lib.opus_encode(
            ctypes.c_void_p(self._encoder),
            pcm_array,
            ctypes.c_int(frame_size),
            out_buf,
            ctypes.c_int32(max_out),
        )
        if n < 0:
            err_str = self._lib.opus_strerror(n)
            logger.error("opus_encode failed: %s (code %d)", err_str, n)
            return None

        return bytes(out_buf[:n])

    def set_bitrate(self, bitrate: int) -> None:
        self._ctl(OPUS_SET_BITRATE_REQUEST, bitrate)

    def set_frame_ms(self, frame_ms: int) -> None:
        self._frame_ms = frame_ms
        self._frame_size = (self._sample_rate * frame_ms) // 1000

    def destroy(self) -> None:
        if self._lib is not None and self._encoder is not None:
            self._lib.opus_encoder_destroy(ctypes.c_void_p(self._encoder))
            self._encoder = None
            logger.info("Opus encoder destroyed")

    def __del__(self) -> None:
        self.destroy()


class OpusDecoder:
    """
    Thin ctypes wrapper around libopus decoder for Phone -> PC streaming.
    Thread-safe for a single decoder instance.
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 2):
        self._lib: Optional[ctypes.CDLL] = None
        self._decoder = None
        self._channels = channels
        self._sample_rate = sample_rate

        dll_path = _locate_dll()
        if dll_path is None:
            logger.warning("opus.dll not found — OpusDecoder unavailable")
            return

        try:
            lib = ctypes.CDLL(str(dll_path))
        except OSError as exc:
            logger.error("Failed to load opus.dll from %s: %s", dll_path, exc)
            return

        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [
            ctypes.c_int,   # Fs
            ctypes.c_int,   # channels
            ctypes.POINTER(ctypes.c_int), # error
        ]

        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [
            ctypes.c_void_p,                # decoder
            ctypes.POINTER(ctypes.c_ubyte), # data
            ctypes.c_int32,                 # len
            ctypes.POINTER(ctypes.c_int16), # out_pcm
            ctypes.c_int,                   # frame_size
            ctypes.c_int,                   # decode_fec
        ]

        lib.opus_decoder_destroy.restype = None
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]

        lib.opus_strerror.restype = ctypes.c_char_p
        lib.opus_strerror.argtypes = [ctypes.c_int]

        error = ctypes.c_int(0)
        dec = lib.opus_decoder_create(
            ctypes.c_int(sample_rate),
            ctypes.c_int(channels),
            ctypes.byref(error),
        )
        if error.value != OPUS_OK or dec is None:
            err_str = lib.opus_strerror(error.value)
            logger.error("opus_decoder_create failed: %s (code %d)", err_str, error.value)
            return

        self._lib = lib
        self._decoder = dec

    @property
    def available(self) -> bool:
        return self._lib is not None and self._decoder is not None

    def decode(self, payload: Optional[bytes], frame_size: int = 960, fec: bool = False) -> Optional[bytes]:
        if not self.available:
            return None

        out_pcm = (ctypes.c_int16 * (frame_size * self._channels))()
        if payload and len(payload) > 0:
            in_buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
            n = self._lib.opus_decode(
                ctypes.c_void_p(self._decoder),
                in_buf,
                ctypes.c_int32(len(payload)),
                out_pcm,
                ctypes.c_int(frame_size),
                ctypes.c_int(1 if fec else 0),
            )
        else:
            n = self._lib.opus_decode(
                ctypes.c_void_p(self._decoder),
                None,
                ctypes.c_int32(0),
                out_pcm,
                ctypes.c_int(frame_size),
                ctypes.c_int(1 if fec else 0),
            )

        if n < 0:
            return None

        total_bytes = n * self._channels * 2
        return bytes(out_pcm)[:total_bytes]

    def destroy(self) -> None:
        if self._lib is not None and self._decoder is not None:
            self._lib.opus_decoder_destroy(ctypes.c_void_p(self._decoder))
            self._decoder = None

    def __del__(self) -> None:
        self.destroy()
