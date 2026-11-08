"""
win_volume.py — Cached, zero-leak two-way synchronization with the Windows Taskbar Sound Bar
(via ctypes CoreAudio IAudioEndpointVolume COM interface) and Linux pactl.
"""

import ctypes
from ctypes import POINTER, Structure, byref, c_float, c_int, c_uint, c_void_p, HRESULT
import logging
import re
import shutil
import subprocess
import sys
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_IS_WIN = sys.platform.startswith("win")
_IS_LINUX = sys.platform.startswith("linux")


class GUID(Structure):
    _fields_ = [
        ("Data1", c_uint),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, l, w1, w2, b1, b2, b3, b4, b5, b6, b7, b8):
        super().__init__()
        self.Data1 = l
        self.Data2 = w1
        self.Data3 = w2
        self.Data4[:] = (b1, b2, b3, b4, b5, b6, b7, b8)


CLSID_MMDeviceEnumerator = GUID(
    0xBCDE0395, 0xE52F, 0x467C, 0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E
)
IID_IMMDeviceEnumerator = GUID(
    0xA95664D2, 0x9614, 0x4F35, 0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6
)
IID_IAudioEndpointVolume = GUID(
    0x5CDF2C82, 0x841E, 0x4546, 0x97, 0x22, 0x0C, 0xF7, 0x40, 0x78, 0x22, 0x9A
)

_CACHED_ENUM: Optional[c_void_p] = None
_CACHED_DEV: Optional[c_void_p] = None
_CACHED_EP_VOL: Optional[c_void_p] = None
_LAST_ENDPOINT_CHECK: float = 0.0


def _release_com_ptrs(*ptrs: Optional[c_void_p]) -> None:
    for p in ptrs:
        if p and p.value:
            try:
                vt = ctypes.cast(p, POINTER(POINTER(c_void_p))).contents
                rel = ctypes.WINFUNCTYPE(c_uint, c_void_p)(vt[2])
                rel(p)
            except Exception:
                pass


def _reset_cached_endpoint() -> None:
    global _CACHED_ENUM, _CACHED_DEV, _CACHED_EP_VOL
    _release_com_ptrs(_CACHED_EP_VOL, _CACHED_DEV, _CACHED_ENUM)
    _CACHED_EP_VOL = None
    _CACHED_DEV = None
    _CACHED_ENUM = None


def _get_cached_endpoint_volume() -> Optional[c_void_p]:
    """Return cached IAudioEndpointVolume pointer, re-initializing every 3s in case user switched output device."""
    global _CACHED_ENUM, _CACHED_DEV, _CACHED_EP_VOL, _LAST_ENDPOINT_CHECK
    if not _IS_WIN:
        return None

    import time
    now = time.monotonic()
    if _CACHED_EP_VOL and _CACHED_EP_VOL.value and (now - _LAST_ENDPOINT_CHECK) < 3.0:
        return _CACHED_EP_VOL

    _reset_cached_endpoint()
    _LAST_ENDPOINT_CHECK = now

    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx.argtypes = [c_void_p, c_uint]
        ole32.CoInitializeEx.restype = HRESULT
        ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED

        ole32.CoCreateInstance.argtypes = [
            POINTER(GUID), c_void_p, c_uint, POINTER(GUID), POINTER(c_void_p)
        ]
        ole32.CoCreateInstance.restype = HRESULT

        enumerator = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(CLSID_MMDeviceEnumerator),
            None,
            7,  # CLSCTX_ALL
            byref(IID_IMMDeviceEnumerator),
            byref(enumerator),
        )
        if hr != 0 or not enumerator.value:
            return None

        enum_vtable = ctypes.cast(enumerator, POINTER(POINTER(c_void_p))).contents
        get_default_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_int, c_int, POINTER(c_void_p))(enum_vtable[4])
        device = c_void_p()
        hr = get_default_fn(enumerator, 0, 0, byref(device))
        if hr != 0 or not device.value:
            _release_com_ptrs(enumerator)
            return None

        dev_vtable = ctypes.cast(device, POINTER(POINTER(c_void_p))).contents
        activate_fn = ctypes.WINFUNCTYPE(
            HRESULT, c_void_p, POINTER(GUID), c_uint, c_void_p, POINTER(c_void_p)
        )(dev_vtable[3])
        ep_vol = c_void_p()
        hr = activate_fn(device, byref(IID_IAudioEndpointVolume), 7, None, byref(ep_vol))
        if hr != 0 or not ep_vol.value:
            _release_com_ptrs(device, enumerator)
            return None

        _CACHED_ENUM = enumerator
        _CACHED_DEV = device
        _CACHED_EP_VOL = ep_vol
        return _CACHED_EP_VOL
    except Exception:
        _reset_cached_endpoint()
        return None


def get_system_volume_and_mute() -> Optional[Tuple[float, bool]]:
    """
    Read the OS master volume scalar (0.0 to 1.0) and mute state (bool)
    from the Windows Sound Bar or Linux PipeWire/PulseAudio default sink.
    """
    if _IS_WIN:
        ep_p = _get_cached_endpoint_volume()
        if not ep_p:
            return None
        try:
            vt = ctypes.cast(ep_p, POINTER(POINTER(c_void_p))).contents
            get_vol_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_float))(vt[9])
            get_mute_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))(vt[15])

            vol = c_float(1.0)
            muted = c_int(0)
            hr1 = get_vol_fn(ep_p, byref(vol))
            hr2 = get_mute_fn(ep_p, byref(muted))
            if hr1 == 0 and hr2 == 0:
                return float(vol.value), bool(muted.value)
            _reset_cached_endpoint()
            return None
        except Exception:
            _reset_cached_endpoint()
            return None

    if _IS_LINUX and shutil.which("pactl"):
        try:
            res_v = subprocess.run(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=1
            )
            res_m = subprocess.run(
                ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=1
            )
            m = re.search(r"(\d+)%", res_v.stdout)
            vol = (int(m.group(1)) / 100.0) if m else 1.0
            muted = "yes" in res_m.stdout.lower()
            return min(2.0, max(0.0, vol)), muted
        except Exception:
            return None

    return None


def set_system_volume_and_mute(volume_scalar: float, muted: Optional[bool] = None) -> bool:
    """
    Set the OS master volume (0.0 to 1.0 on Windows, 0% to 200% on Linux) and mute state.
    """
    if _IS_WIN:
        ep_p = _get_cached_endpoint_volume()
        if not ep_p:
            return False
        try:
            vt = ctypes.cast(ep_p, POINTER(POINTER(c_void_p))).contents
            set_vol_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_float, c_void_p)(vt[7])
            set_mute_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_int, c_void_p)(vt[14])

            clamped = max(0.0, min(1.0, float(volume_scalar)))
            hr = set_vol_fn(ep_p, c_float(clamped), None)
            if muted is not None:
                set_mute_fn(ep_p, c_int(1 if muted else 0), None)
            if hr != 0:
                _reset_cached_endpoint()
                return False
            return True
        except Exception:
            _reset_cached_endpoint()
            return False

    if _IS_LINUX and shutil.which("pactl"):
        try:
            pct = int(round(max(0.0, min(2.0, float(volume_scalar))) * 100))
            subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"], timeout=1)
            if muted is not None:
                subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"], timeout=1)
            return True
        except Exception:
            return False

    return False
