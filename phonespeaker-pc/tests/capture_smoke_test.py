"""
tests/capture_smoke_test.py — Live WASAPI loopback capture & diagnostic WAV verification.
"""

import math
import queue
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
sys.path.insert(0, str(_SRC))

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    import pyaudio

from audio_capture import AudioCapture, list_loopback_devices, convert_to_target


def play_test_tone(duration_sec: float = 2.0, freq: float = 440.0) -> None:
    """Play a test tone to the default Windows playback device."""
    try:
        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paFloat32,
            channels=2,
            rate=48000,
            output=True,
        )

        sample_count = int(48000 * duration_sec)
        t = np.arange(sample_count, dtype=np.float32) / 48000.0
        tone = (0.3 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
        stereo = np.column_stack((tone, tone)).tobytes()

        stream.write(stereo)
        stream.stop_stream()
        stream.close()
        pa.terminate()
    except Exception as exc:
        print(f"Warning: Could not play test tone: {exc}")


def main() -> None:
    print("=" * 60)
    print("PhoneSpeaker — Real WASAPI Loopback Smoke Test")
    print("=" * 60)

    devices = list_loopback_devices()
    print(f"Discovered {len(devices)} loopback device(s):")
    for d in devices:
        print(f"  [{d['index']}] {d['name']} (rate={d['sample_rate']}, ch={d['channels']})")

    if not devices:
        print("ERROR: No WASAPI loopback device found.")
        sys.exit(1)

    dev_idx = devices[0]["index"]
    print(f"\nUsing default loopback device [{dev_idx}]: {devices[0]['name']}")

    q: "queue.Queue[bytes]" = queue.Queue()
    capture = AudioCapture(device_index=dev_idx, output_queue=q, frame_ms=5)

    if not capture.start():
        print("ERROR: Failed to start AudioCapture.")
        sys.exit(1)

    print("AudioCapture started.")
    print("Starting background playback of 440 Hz test tone for 2.5 seconds...")

    # Start tone player in thread
    player_thread = threading.Thread(target=play_test_tone, args=(2.5, 440.0))
    player_thread.start()

    collected_frames = []
    start_time = time.monotonic()
    peak_observed = 0.0

    # Collect audio for 2.5 seconds
    while time.monotonic() - start_time < 2.5:
        try:
            chunk = q.get(timeout=0.05)
            assert len(chunk) == 960, f"Expected 960 bytes, got {len(chunk)}"
            collected_frames.append(chunk)

            pcm = np.frombuffer(chunk, dtype="<i2")
            chunk_peak = float(np.max(np.abs(pcm))) / 32768.0
            if chunk_peak > peak_observed:
                peak_observed = chunk_peak
        except queue.Empty:
            pass

    player_thread.join()
    capture.stop()

    print(f"\nCaptured {len(collected_frames)} frames (total {len(collected_frames) * 960} bytes).")
    print(f"Peak level observed: {peak_observed:.4f}")

    # Write diagnostic WAV file
    wav_path = _ROOT / "diagnostic.wav"
    all_pcm = b"".join(collected_frames)

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(all_pcm)

    print(f"Wrote diagnostic WAV: {wav_path} ({len(all_pcm)} bytes, duration={len(all_pcm) / (48000 * 4):.2f}s)")

    # Assertions
    assert len(collected_frames) >= 200, f"Expected >= 200 frames, got {len(collected_frames)}"
    assert peak_observed > 0.05, f"Peak level {peak_observed} too low (silence detected)"
    assert peak_observed <= 1.0, f"Peak level {peak_observed} clipped"

    # Analyze samples in diagnostic wave
    pcm_all = np.frombuffer(all_pcm, dtype="<i2").reshape(-1, 2)
    # Check that left and right channels correlate strongly (test tone was mono duplicated to stereo)
    corr = np.corrcoef(pcm_all[:, 0].astype(float), pcm_all[:, 1].astype(float))[0, 1]
    print(f"Stereo channel correlation: {corr:.4f}")
    assert corr > 0.95, f"Left/Right correlation too low: {corr}"

    # Check for smooth waveform (not white noise static)
    diff = np.abs(np.diff(pcm_all[:, 0]))
    mean_diff = float(np.mean(diff))
    print(f"Mean adjacent difference: {mean_diff:.1f} (static noise would be > 3000)")
    assert mean_diff < 1500, f"Mean adjacent difference {mean_diff} indicates static noise!"

    print("\n[SUCCESS] WASAPI Loopback smoke test PASSED!")
    print("Audio was cleanly captured from Windows playback, converted from Float32 to Int16, and verified non-static.")


if __name__ == "__main__":
    main()

