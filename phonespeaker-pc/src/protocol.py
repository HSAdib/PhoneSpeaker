"""
protocol.py — Shared wire protocol definitions for PhoneSpeaker v2.0.
"""

import struct
import time
from typing import Any, Dict, Optional

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_MS_5 = 5
FRAME_MS_10 = 10
FRAME_MS_20 = 20

# Codec identifiers
CODEC_PCM16 = 0
CODEC_OPUS = 1

# Control & audio ports
AUDIO_PORT = 5004          # PC -> Phone audio stream
CONTROL_PORT = 5005        # Bidirectional control & 2-way settings sync
PHONE_AUDIO_PORT = 5006    # Phone -> PC audio stream (Mic / Apps)

# Buffer presets
BUFFER_PRESETS = {
    "LOW": 20.0,
    "MEDIUM": 40.0,
    "HIGH": 80.0,
}

# Default v2.0 Configuration
DEFAULT_CONFIG: Dict[str, Any] = {
    "type": "CONFIG_SYNC",
    "rev": 1,
    "direction": "PC_TO_PHONE",         # PC_TO_PHONE, PHONE_TO_PC, FULL_DUPLEX
    "pc_source": "SYSTEM_AUDIO",        # SYSTEM_AUDIO, MICROPHONE
    "phone_source": "MIC",              # MIC, APPS
    "output_targets": "PHONE_ONLY",     # PHONE_ONLY, PC_ONLY, BOTH_SYNCED
    "pc_sync_delay_ms": 25,             # 0 to 150 ms delay for PC speaker in BOTH_SYNCED
    "codec": CODEC_OPUS,
    "opus_bitrate_kbps": 128,
    "frame_ms": 5,
    "buffer_mode": "AUTO",              # LOW, MEDIUM, HIGH, AUTO, CUSTOM
    "custom_buffer_ms": 30.0,
    "auto_buffer": True,
    "volume_gain": 1.0,                 # 0.0 to 2.0 (200% boost)
    "muted": False,
    "audio_output_engine": "OBOE_LOW_LATENCY",  # OBOE_LOW_LATENCY, AUDIOTRACK_STANDARD
    "enable_audio_effects": False,
    "exclusive_audio_mode": "MIX_WITH_OTHERS",   # MIX_WITH_OTHERS, EXCLUSIVE, ALWAYS_PLAY
    "stop_on_headset_disconnect": False,
}


def samples_per_frame(frame_ms: int, sample_rate: int = SAMPLE_RATE) -> int:
    """Return number of samples per channel for a given frame duration."""
    return (sample_rate * frame_ms) // 1000


def pcm_payload_size(frame_ms: int, channels: int = CHANNELS) -> int:
    """Return the byte size of a raw PCM16 payload for frame_ms."""
    return samples_per_frame(frame_ms) * channels * 2  # 2 bytes per int16 sample


# Pre-computed silence frames
SILENCE_PCM_5MS: bytes = b'\x00' * pcm_payload_size(FRAME_MS_5)
SILENCE_PCM_10MS: bytes = b'\x00' * pcm_payload_size(FRAME_MS_10)
SILENCE_PCM_20MS: bytes = b'\x00' * pcm_payload_size(FRAME_MS_20)

# Header format: little-endian, uint32 seq, uint32 timestamp_ms, uint8 codec,
#                uint8 channels, uint16 sample_rate_div100
_HEADER_FMT = '<IIBBH'
HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 12 bytes


def build_header(
    seq: int,
    timestamp_ms: int,
    codec: int,
    channels: int = CHANNELS,
    sample_rate: int = SAMPLE_RATE,
) -> bytes:
    """Pack a 12-byte protocol header."""
    return struct.pack(
        _HEADER_FMT,
        seq & 0xFFFFFFFF,
        timestamp_ms & 0xFFFFFFFF,
        codec & 0xFF,
        channels & 0xFF,
        (sample_rate // 100) & 0xFFFF,
    )


def parse_header(data: bytes) -> Optional[Dict[str, int]]:
    """Unpack a 12-byte protocol header."""
    if len(data) < HEADER_SIZE:
        return None
    seq, ts, codec, channels, sr_div100 = struct.unpack_from(_HEADER_FMT, data, 0)
    return {
        "seq": seq,
        "timestamp_ms": ts,
        "codec": codec,
        "channels": channels,
        "sample_rate": sr_div100 * 100,
    }


def current_timestamp_ms() -> int:
    """Return monotonic timestamp in milliseconds, masked to uint32."""
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def build_pcm_packet(seq: int, payload: bytes, codec: int = CODEC_PCM16, channels: int = CHANNELS, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Build a full datagram (header + PCM payload)."""
    ts = current_timestamp_ms()
    header = build_header(seq, ts, codec, channels=channels, sample_rate=sample_rate)
    return header + payload


def build_silence_packet(seq: int) -> bytes:
    """Build a 5ms silence PCM16 keep-alive packet."""
    return build_pcm_packet(seq, SILENCE_PCM_5MS, CODEC_PCM16)
