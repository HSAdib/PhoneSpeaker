r"""
tests/verify_pc.py — Self-contained verification tests for PhoneSpeaker PC.

Run with: .venv\Scripts\python tests\verify_pc.py
"""

import os
import socket
import struct
import sys
import time
from pathlib import Path

import numpy as np

# Add src/ to path
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
sys.path.insert(0, str(_SRC))

PASS = "PASS"
FAIL = "FAIL"
results = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    results.append((name, status))


# ---------------------------------------------------------------------------
# Test 1: Adapter filter — 192.168.56.1 is excluded
# ---------------------------------------------------------------------------
def test_adapter_filter() -> None:
    name = "Adapter filter excludes 192.168.56.1"
    try:
        from network import _is_excluded_iface

        class _FakeAddr:
            def __init__(self, ip):
                self.address = ip
                self.family = None

        addrs = [_FakeAddr("192.168.56.1")]
        excluded = _is_excluded_iface("VirtualBox Host-Only Ethernet", addrs)
        check(name, excluded, "192.168.56.1 correctly filtered")
    except Exception as exc:
        check(name, False, str(exc))


# ---------------------------------------------------------------------------
# Test 2: PCM16 5ms frame packet size = 972 bytes
# ---------------------------------------------------------------------------
def test_pcm_packet_size() -> None:
    name = "PCM16 5ms packet size == 972 bytes"
    try:
        from protocol import build_silence_packet, HEADER_SIZE, pcm_payload_size, FRAME_MS_5

        pkt = build_silence_packet(1)
        expected = HEADER_SIZE + pcm_payload_size(FRAME_MS_5)
        check(name, len(pkt) == 972,
              f"got {len(pkt)} bytes (header={HEADER_SIZE}, payload={pcm_payload_size(FRAME_MS_5)}, expected={expected})")
    except Exception as exc:
        check(name, False, str(exc))


# ---------------------------------------------------------------------------
# Test 3: Opus encoder creates and encodes 5ms silent frame
# ---------------------------------------------------------------------------
def test_opus_encoder() -> None:
    name = "Opus encoder creates and encodes 5ms silent frame"
    try:
        from opus_codec import OpusEncoder

        enc = OpusEncoder(
            sample_rate=48000,
            channels=2,
            bitrate=160_000,
            frame_ms=5,
        )
        if not enc.available:
            check(name, False, "opus.dll not found — encoder unavailable (PCM fallback mode)")
            return

        # 5ms @ 48kHz stereo int16 = 240 samples/ch * 2 ch * 2 bytes = 960 bytes
        samples_per_ch = (48000 * 5) // 1000  # 240
        silent_frame = b'\x00' * (samples_per_ch * 2 * 2)  # 960 bytes
        encoded = enc.encode(silent_frame)
        enc.destroy()

        check(name, encoded is not None and len(encoded) > 0,
              f"encoded {len(encoded) if encoded else 0} bytes")
    except Exception as exc:
        check(name, False, str(exc))


# ---------------------------------------------------------------------------
# Test 4: Silence frame generator at ≥180 pps (5ms intervals)
# ---------------------------------------------------------------------------
def test_silence_pacing() -> None:
    name = "Silence frame pacing ≥180 pps (targeting 200 pps / 5ms)"
    try:
        import ctypes
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

        from protocol import build_silence_packet

        INTERVAL = 0.005
        DURATION = 1.0  # measure over 1 second

        count = 0
        next_tick = time.monotonic()
        end = time.monotonic() + DURATION

        while time.monotonic() < end:
            pkt = build_silence_packet(count)
            count += 1
            next_tick += INTERVAL
            sleep_time = next_tick - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)

        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass

        pps = count
        check(name, pps >= 180, f"{pps} pps achieved")
    except Exception as exc:
        check(name, False, str(exc))


# ---------------------------------------------------------------------------
# Test 5: AudioCapture opens default loopback cleanly
# ---------------------------------------------------------------------------
def test_audio_capture_open() -> None:
    name = "AudioCapture opens default loopback cleanly"
    try:
        import queue
        from audio_capture import list_loopback_devices, AudioCapture

        devices = list_loopback_devices()
        dev_idx = devices[0]["index"] if devices else None

        q = queue.Queue(maxsize=100)
        capture = AudioCapture(
            device_index=dev_idx,
            output_queue=q,
            frame_ms=5,
        )

        ok = capture.start()
        time.sleep(0.2)
        capture.stop()

        check(name, ok, f"device_index={dev_idx}, devices found={len(devices)}")
    except Exception as exc:
        check(name, False, str(exc))


# ---------------------------------------------------------------------------
# Test 6: Deterministic format regression & 440 Hz waveform conversion
# ---------------------------------------------------------------------------
def test_format_constants_and_sine_conversion() -> None:
    name = "Sample format constants & 440Hz sine conversion (Float32 & Int16)"
    try:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            import pyaudio

        from audio_capture import convert_to_target

        # 1. Assert PyAudio sample format constants to prevent regression
        assert pyaudio.paFloat32 == 1, f"Expected paFloat32=1, got {pyaudio.paFloat32}"
        assert pyaudio.paInt32 == 2, f"Expected paInt32=2, got {pyaudio.paInt32}"
        assert pyaudio.paInt24 == 4, f"Expected paInt24=4, got {pyaudio.paInt24}"
        assert pyaudio.paInt16 == 8, f"Expected paInt16=8, got {pyaudio.paInt16}"
        assert pyaudio.paInt8 == 16, f"Expected paInt8=16, got {pyaudio.paInt8}"
        assert pyaudio.paUInt8 == 32, f"Expected paUInt8=32, got {pyaudio.paUInt8}"

        # 2. Generate known 440 Hz stereo sine wave (5ms @ 48kHz = 240 frames)
        frames = 240
        t = np.arange(frames, dtype=np.float32) / 48000.0
        tone = 0.25 * np.sin(2.0 * np.pi * 440.0 * t)
        stereo = np.column_stack((tone, tone)).astype(np.float32)

        # Test Float32 input
        raw_float = stereo.astype("<f4").tobytes()
        converted_float = convert_to_target(
            raw_float,
            48000,
            2,
            pyaudio.paFloat32,
        )

        assert len(converted_float) == 960, f"Expected 960 bytes, got {len(converted_float)}"
        pcm_float = np.frombuffer(converted_float, dtype="<i2").reshape(-1, 2)

        peak_float = int(np.max(np.abs(pcm_float)))
        assert 7500 <= peak_float <= 8500, f"Expected peak ~8192 (7500-8500), got {peak_float}"
        assert np.array_equal(pcm_float[:, 0], pcm_float[:, 1]), "Left and right channels do not match"

        # Waveform smoothness check: max step difference between adjacent samples for 440Hz @ 48kHz
        max_diff = int(np.max(np.abs(np.diff(pcm_float[:, 0]))))
        assert max_diff < 1500, f"Waveform appears corrupted/static: max_diff={max_diff}"

        # Test Int16 input
        raw_int16 = np.rint(stereo * 32767.0).astype("<i2").tobytes()
        converted_int16 = convert_to_target(
            raw_int16,
            48000,
            2,
            pyaudio.paInt16,
        )

        assert len(converted_int16) == 960, f"Expected 960 bytes, got {len(converted_int16)}"
        pcm_int16 = np.frombuffer(converted_int16, dtype="<i2").reshape(-1, 2)
        orig_int16 = np.frombuffer(raw_int16, dtype="<i2").reshape(-1, 2)
        assert np.max(np.abs(pcm_int16 - orig_int16)) <= 1, "Int16 conversion deviated from original"

        check(name, True, f"peak={peak_float}, max_diff={max_diff}, int16 error <= 1")
    except Exception as exc:
        check(name, False, str(exc))


# ---------------------------------------------------------------------------
# Test 7: Local loopback UDP integration (Float32 capture -> convert -> UDP)
# ---------------------------------------------------------------------------
def test_udp_localhost_integration() -> None:
    name = "Local loopback UDP integration (Float32 -> convert -> 972-byte UDP -> verify)"
    try:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            import pyaudio

        from audio_capture import convert_to_target
        from protocol import CODEC_PCM16

        # 1. Generate 440Hz Float32 stereo block
        frames = 240
        t = np.arange(frames, dtype=np.float32) / 48000.0
        tone = 0.25 * np.sin(2.0 * np.pi * 440.0 * t)
        stereo = np.column_stack((tone, tone)).astype(np.float32)
        raw_float = stereo.astype("<f4").tobytes()

        # 2. Convert through convert_to_target
        converted_pcm = convert_to_target(raw_float, 48000, 2, pyaudio.paFloat32)
        assert len(converted_pcm) == 960

        # 3. Build 12-byte header + PCM payload
        seq = 42
        timestamp_ms = 123456
        codec = CODEC_PCM16  # 0
        channels = 2
        sample_rate_div100 = 480
        header = struct.pack("<IIBBH", seq, timestamp_ms, codec, channels, sample_rate_div100)
        datagram = header + converted_pcm
        assert len(datagram) == 972

        # 4. Send & receive on localhost UDP socket
        rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx_sock.bind(("127.0.0.1", 0))
        rx_port = rx_sock.getsockname()[1]
        rx_sock.settimeout(1.0)

        tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx_sock.sendto(datagram, ("127.0.0.1", rx_port))

        data, _ = rx_sock.recvfrom(2048)
        rx_sock.close()
        tx_sock.close()

        # 5. Parse and verify
        assert len(data) == 972, f"Expected 972 bytes, got {len(data)}"
        r_seq, r_ts, r_codec, r_ch, r_sr = struct.unpack("<IIBBH", data[:12])
        r_payload = data[12:]

        assert r_seq == seq, f"seq mismatch: {r_seq} != {seq}"
        assert r_ts == timestamp_ms, f"ts mismatch: {r_ts} != {timestamp_ms}"
        assert r_codec == CODEC_PCM16, f"codec mismatch: {r_codec} != {CODEC_PCM16}"
        assert r_ch == 2, f"channels mismatch: {r_ch} != 2"
        assert r_sr == 480, f"sample_rate_div100 mismatch: {r_sr} != 480"
        assert r_payload == converted_pcm, "Payload does not match converted PCM"

        # Verify payload is smooth 440Hz sine wave
        pcm_arr = np.frombuffer(r_payload, dtype="<i2").reshape(-1, 2)
        peak = int(np.max(np.abs(pcm_arr)))
        assert 7500 <= peak <= 8500, f"Peak {peak} out of range"

        check(name, True, f"received {len(data)} bytes, codec={r_codec}, peak={peak}")
    except Exception as exc:
        check(name, False, str(exc))


# ---------------------------------------------------------------------------
# Test 8: Protocol v2.0 bidirectional header & CONFIG_SYNC validation
# ---------------------------------------------------------------------------
def test_protocol_v2_and_sync() -> None:
    name = "Protocol v2.0 bidirectional header & CONFIG_SYNC validation"
    try:
        from protocol import (
            PHONE_AUDIO_PORT,
            DEFAULT_CONFIG,
            build_header,
            parse_header,
            CODEC_OPUS,
        )
        from opus_codec import OpusDecoder

        assert PHONE_AUDIO_PORT == 5006, f"Expected port 5006, got {PHONE_AUDIO_PORT}"
        assert DEFAULT_CONFIG["type"] == "CONFIG_SYNC"
        assert "direction" in DEFAULT_CONFIG
        assert "output_targets" in DEFAULT_CONFIG
        assert "pc_sync_delay_ms" in DEFAULT_CONFIG

        from ui import _QueueAdapter
        from network import AudioQueue
        assert _QueueAdapter(AudioQueue()).qsize() == 0, "QueueAdapter.qsize() check failed"

        hdr = build_header(42, 1000, CODEC_OPUS, channels=2, sample_rate=48000)
        parsed = parse_header(hdr)
        assert parsed is not None
        assert parsed["seq"] == 42
        assert parsed["timestamp_ms"] == 1000
        assert parsed["codec"] == CODEC_OPUS
        assert parsed["channels"] == 2
        assert parsed["sample_rate"] == 48000

        dec = OpusDecoder(48000, 2)
        assert dec.available, "OpusDecoder should be available via opus.dll"
        plc_decoded = dec.decode(None, frame_size=240)
        dec.destroy()
        assert plc_decoded is not None and len(plc_decoded) == 240 * 2 * 2

        check(name, True, f"ports, config and OpusDecoder verified")
    except Exception as exc:
        check(name, False, str(exc))


# ---------------------------------------------------------------------------
# Test 9: Windows/Linux System Volume & Mute COM interface
# ---------------------------------------------------------------------------
def test_win_volume() -> None:
    name = "Windows/Linux system volume and mute query"
    try:
        from win_volume import get_system_volume_and_mute
        res = get_system_volume_and_mute()
        if res is not None:
            vol, muted = res
            check(name, 0.0 <= vol <= 2.0 and isinstance(muted, bool),
                  f"volume={vol:.2f}, muted={muted}")
        else:
            check(name, True, "get_system_volume_and_mute executed cleanly (no active endpoint)")
    except Exception as exc:
        check(name, False, str(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("PhoneSpeaker PC — Verification Tests")
    print("=" * 60)
    print()

    test_adapter_filter()
    test_pcm_packet_size()
    test_opus_encoder()
    test_silence_pacing()
    test_audio_capture_open()
    test_format_constants_and_sine_conversion()
    test_udp_localhost_integration()
    test_protocol_v2_and_sync()
    test_win_volume()

    print()
    print("=" * 60)
    passed = sum(1 for _, s in results if s == PASS)
    failed = sum(1 for _, s in results if s == FAIL)
    print(f"Results: {passed} PASS / {failed} FAIL out of {len(results)} tests")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
