"""
config.py — Cross-platform configuration (Windows %APPDATA% & Linux ~/.config).
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

APP_NAME = "PhoneSpeaker"
if sys.platform.startswith("win"):
    _BASE_DIR = Path(os.environ.get("APPDATA", os.path.expanduser("~")))
elif sys.platform == "darwin":
    _BASE_DIR = Path.home() / "Library" / "Application Support"
else:
    _BASE_DIR = Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")))

_CONFIG_DIR = _BASE_DIR / APP_NAME
_CONFIG_FILE = _CONFIG_DIR / "config.json"
LOG_FILE = _CONFIG_DIR / "phonespeaker.log"

_DEFAULTS: Dict[str, Any] = {
    "target_ip": "",
    "target_port": 5004,
    "phone_ip": "",
    "phone_port": 5004,
    "audio_profile": "BALANCED",
    "codec": 1,          # Unified default: 1=Opus, 0=PCM16
    "bitrate": 192000,
    "opus_bitrate_kbps": 192,
    "frame_ms": 5,
    "buffer_mode": "AUTO",
    "custom_buffer_ms": 30.0,
    "volume_gain": 1.0,
    "muted": False,
    "mic_enabled": True,
    "direction": "PC_TO_PHONE",
    "adapter_name": "",
    "minimize_to_tray": True,
    "audio_output_engine": "OBOE_LOW_LATENCY",
    "enable_audio_effects": False,
    "exclusive_audio_mode": "MIX_WITH_OTHERS",
    "stop_on_headset_disconnect": False,
    "party_mode": False,
    "party_ips": [],
    "config_rev": 1,
}


def ensure_config_dir() -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load() -> Dict[str, Any]:
    ensure_config_dir()
    cfg = dict(_DEFAULTS)
    if _CONFIG_FILE.exists():
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in _DEFAULTS})
        except Exception as exc:
            logger.warning("Could not read config file: %s", exc)
    return cfg


def save(cfg: Dict[str, Any]) -> None:
    ensure_config_dir()
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception as exc:
        logger.error("Could not save config file: %s", exc)


def setup_logging() -> None:
    ensure_config_dir()
    handlers: list = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(str(LOG_FILE), encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
