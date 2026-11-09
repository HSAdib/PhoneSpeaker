"""
network.py — UDP sender, control listener, adapter enumeration, and frame pacing.
"""

import json
import logging
import queue
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import psutil

from protocol import (
    AUDIO_PORT,
    CONTROL_PORT,
    CODEC_PCM16,
    CODEC_OPUS,
    SILENCE_PCM_5MS,
    SILENCE_PCM_10MS,
    SILENCE_PCM_20MS,
    build_header,
    build_pcm_packet,
    build_silence_packet,
    current_timestamp_ms,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Windows timer resolution
# ---------------------------------------------------------------------------
_winmm = None
try:
    import ctypes
    _winmm = ctypes.windll.winmm
except Exception:
    pass


def set_timer_resolution(period_ms: int = 1) -> None:
    if _winmm:
        _winmm.timeBeginPeriod(period_ms)


def clear_timer_resolution(period_ms: int = 1) -> None:
    if _winmm:
        _winmm.timeEndPeriod(period_ms)


# ---------------------------------------------------------------------------
# Adapter filtering
# ---------------------------------------------------------------------------

_EXCLUDE_PATTERNS = [
    "virtualbox", "host-only", "vmware", "vethernet", "hyper-v",
    "wsl", "loopback", "tap", "tun", "vpn", "local area connection*",
]

_EXCLUDE_IP_PREFIXES = [
    "192.168.56.",
    "169.254.",
    "127.",
    "::1",
]


def _is_excluded_iface(name: str, addrs) -> bool:
    name_lower = name.lower()
    for pat in _EXCLUDE_PATTERNS:
        pat_clean = pat.rstrip("*")
        if pat_clean in name_lower:
            return True
    # Check IPs on this interface
    for addr in addrs:
        ip = addr.address or ""
        for prefix in _EXCLUDE_IP_PREFIXES:
            if ip.startswith(prefix):
                return True
    return False


def list_real_adapters() -> List[Dict[str, Any]]:
    """
    Enumerate real network adapters (Wi-Fi, Ethernet, USB RNDIS, Mobile Hotspot),
    excluding virtual/VM/loopback interfaces.

    Returns list of dicts: {name, ip, is_up}
    """
    result = []
    try:
        all_addrs = psutil.net_if_addrs()
        all_stats = psutil.net_if_stats()

        for iface_name, addrs in all_addrs.items():
            if _is_excluded_iface(iface_name, addrs):
                continue

            stats = all_stats.get(iface_name)
            is_up = stats.isup if stats else False

            # Collect IPv4 addresses
            ipv4s = [
                a.address for a in addrs
                if a.family == socket.AF_INET
                and not a.address.startswith("127.")
                and not a.address.startswith("169.254.")
                and not a.address.startswith("192.168.56.")
            ]
            if not ipv4s:
                continue

            for ip in ipv4s:
                result.append({
                    "name": iface_name,
                    "ip": ip,
                    "is_up": is_up,
                })
        result.sort(key=lambda a: (not a["is_up"], a["name"]))
    except Exception as exc:
        logger.error("Error enumerating adapters: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Bounded audio frame ring buffer (lock-free via queue)
# ---------------------------------------------------------------------------

QUEUE_MAX_FRAMES = 30  # ~150ms at 5ms/frame


class AudioQueue:
    """Thread-safe bounded queue for audio frames. Drops oldest on overflow."""

    def __init__(self, maxsize: int = QUEUE_MAX_FRAMES):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)

    def put(self, frame: bytes) -> None:
        while self._q.full():
            try:
                self._q.get_nowait()  # Drop oldest
            except queue.Empty:
                break
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            pass

    def get_nowait(self) -> Optional[bytes]:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    def qsize(self) -> int:
        return self._q.qsize()

    def clear(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break


# ---------------------------------------------------------------------------
# UDP sender with pacing
# ---------------------------------------------------------------------------

class UDPSender:
    """
    Sends audio frames over UDP with strict 5ms pacing.

    - Pulls frames from AudioQueue
    - Synthesizes silence when no audio for ≥10ms
    - Runs on a dedicated thread
    """

    def __init__(
        self,
        audio_queue: AudioQueue,
        opus_encoder=None,  # Optional OpusEncoder instance
        on_stats: Optional[Callable[[int], None]] = None,  # pps callback
    ):
        self._audio_queue = audio_queue
        self._opus_encoder = opus_encoder
        self._on_stats = on_stats

        self._target_ip: str = ""
        self._target_port: int = AUDIO_PORT
        self._codec: int = CODEC_PCM16
        self._frame_ms: int = 5
        self._bind_ip: str = ""

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._seq: int = 0
        self._pps_counter: int = 0
        self._pps_timer: float = 0.0
        self._muted: bool = False
        self._volume_gain: float = 1.0
        self._send_enabled: bool = True
        self._lock = threading.Lock()

    def configure(
        self,
        target_ip: str,
        target_port: int = AUDIO_PORT,
        codec: int = CODEC_PCM16,
        frame_ms: int = 5,
        bind_ip: str = "",
    ) -> None:
        with self._lock:
            self._target_ip = target_ip
            self._target_port = target_port
            self._codec = codec
            self._frame_ms = frame_ms
            self._bind_ip = bind_ip

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)

        with self._lock:
            target_ip = self._target_ip
            bind_ip = self._bind_ip

        chosen_bind_ip = None
        target_octets = target_ip.split(".") if target_ip else []
        bind_octets = bind_ip.split(".") if bind_ip else []
        if len(target_octets) == 4 and len(bind_octets) == 4:
            if target_octets[:3] == bind_octets[:3]:
                chosen_bind_ip = bind_ip

        if chosen_bind_ip:
            try:
                self._sock.bind((chosen_bind_ip, 0))
                logger.info("UDPSender bound to local adapter IP %s", chosen_bind_ip)
            except Exception as exc:
                logger.warning("Could not bind to %s (using default route): %s", chosen_bind_ip, exc)
        else:
            logger.info("UDPSender left unbound (0.0.0.0) for OS routing to %s", target_ip)

        self._thread = threading.Thread(
            target=self._run, name="UDPSender", daemon=True
        )
        self._thread.start()
        logger.info("UDPSender started")

    def update_target(self, target_ip: str, target_port: int = AUDIO_PORT) -> None:
        with self._lock:
            old_ip = self._target_ip
            self._target_ip = target_ip
            self._target_port = target_port
            bind_ip = self._bind_ip
            target_octets = target_ip.split(".") if target_ip else []
            bind_octets = bind_ip.split(".") if bind_ip else []
            if len(target_octets) == 4 and len(bind_octets) == 4 and target_octets[:3] != bind_octets[:3]:
                try:
                    if self._sock:
                        try:
                            self._sock.close()
                        except Exception:
                            pass
                    self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                    logger.info("UDPSender recreated unbound socket for subnet switch to %s", target_ip)
                except Exception as exc:
                    logger.warning("Error recreating socket in update_target: %s", exc)
        logger.info("UDPSender target updated to %s:%d (was %s)", target_ip, target_port, old_ip)

    def set_codec_and_bitrate(self, codec: int, bitrate_bps: int, frame_ms: int) -> None:
        with self._lock:
            frame_changed = (self._frame_ms != frame_ms)
            self._codec = codec
            self._frame_ms = frame_ms
            if frame_changed:
                self._audio_queue.clear()
            if codec == CODEC_OPUS:
                if self._opus_encoder and self._opus_encoder.available:
                    try:
                        self._opus_encoder.set_bitrate(bitrate_bps)
                        self._opus_encoder.set_frame_ms(frame_ms)
                    except Exception as e:
                        logger.warning("Error updating Opus encoder: %s", e)
                else:
                    try:
                        from opus_codec import OpusEncoder
                        self._opus_encoder = OpusEncoder(bitrate=bitrate_bps, frame_ms=frame_ms)
                    except Exception as e:
                        logger.warning("Error creating Opus encoder: %s", e)

    def set_routing_and_gain(self, send_enabled: bool, gain: float, muted: bool) -> None:
        with self._lock:
            self._send_enabled = bool(send_enabled)
            self._volume_gain = max(0.0, min(2.0, float(gain)))
            self._muted = bool(muted)

    def set_gain_and_mute(self, gain: float, muted: bool, send_enabled: bool = True) -> None:
        with self._lock:
            self._volume_gain = max(0.0, min(2.0, float(gain)))
            self._muted = bool(muted)
            self._send_enabled = bool(send_enabled)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        with self._lock:
            if self._opus_encoder is not None:
                try:
                    self._opus_encoder.destroy()
                except Exception:
                    pass
                self._opus_encoder = None
        logger.info("UDPSender stopped")

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return self._seq

    def _build_packet(self, pcm_frame: bytes) -> bytes:
        seq = self._next_seq()
        ts = current_timestamp_ms()
        with self._lock:
            codec = self._codec
            muted = self._muted
            gain = self._volume_gain
            frame_ms = self._frame_ms

        expected_pcm_len = ((48000 * frame_ms) // 1000) * 4
        if len(pcm_frame) != expected_pcm_len:
            if len(pcm_frame) > expected_pcm_len:
                pcm_frame = pcm_frame[:expected_pcm_len]
            else:
                pcm_frame = pcm_frame + b'\x00' * (expected_pcm_len - len(pcm_frame))

        if muted or gain <= 0.001:
            pcm_frame = b'\x00' * len(pcm_frame)
        elif abs(gain - 1.0) > 0.01:
            try:
                import numpy as np
                pcm_arr = np.frombuffer(pcm_frame, dtype="<i2").astype(np.float32) * gain
                if gain > 1.0:
                    # Soft-knee limiting above 100% so 100%-200% boost never clips harshly
                    norm = pcm_arr / 32767.0
                    pcm_arr = np.tanh(norm) * 32767.0
                else:
                    pcm_arr = np.clip(pcm_arr, -32768.0, 32767.0)
                pcm_frame = pcm_arr.astype("<i2").tobytes()
            except Exception:
                pass

        if codec == CODEC_OPUS and self._opus_encoder and self._opus_encoder.available:
            encoded = self._opus_encoder.encode(pcm_frame)
            if encoded is not None:
                header = build_header(seq, ts, CODEC_OPUS)
                return header + encoded
            logger.warning("Opus encode failed, sending PCM")

        header = build_header(seq, ts, CODEC_PCM16)
        return header + pcm_frame

    def _run(self) -> None:
        """Main sender loop — strictly paced at frame_ms intervals."""
        set_timer_resolution(1)

        with self._lock:
            frame_ms = self._frame_ms
        interval = frame_ms / 1000.0
        silence_threshold = 0.085  # Only send keep-alive silence after 85ms of true audio silence (never between 10-20ms WASAPI callbacks!)
        silence_frame = (
            SILENCE_PCM_20MS if frame_ms == 20
            else (SILENCE_PCM_10MS if frame_ms == 10 else SILENCE_PCM_5MS)
        )

        next_tick = time.monotonic()
        last_audio_time = time.monotonic()
        pps_window_start = time.monotonic()
        pps_count = 0

        while self._running:
            now = time.monotonic()
            with self._lock:
                target_ip = self._target_ip
                target_port = self._target_port
                send_enabled = self._send_enabled
                sock = self._sock
                if self._frame_ms != frame_ms:
                    frame_ms = self._frame_ms
                    interval = frame_ms / 1000.0
                    silence_threshold = 0.085
                    silence_frame = (
                        SILENCE_PCM_20MS if frame_ms == 20
                        else (SILENCE_PCM_10MS if frame_ms == 10 else SILENCE_PCM_5MS)
                    )

            if target_ip and sock is not None:
                frame = self._audio_queue.get_nowait()

                if frame is not None:
                    last_audio_time = now
                    packet = self._build_packet(frame) if send_enabled else None
                else:
                    # Check if we need silence / keep-alive
                    silence_needed = (now - last_audio_time) >= silence_threshold
                    if silence_needed and send_enabled:
                        packet = self._build_packet(silence_frame)
                    else:
                        packet = None

                if packet is not None and send_enabled:
                    try:
                        sock.sendto(packet, (target_ip, target_port))
                        pps_count += 1
                    except OSError as exc:
                        logger.warning("Send error on bound socket (%s), falling back to unbound socket", exc)
                        try:
                            with self._lock:
                                if self._sock:
                                    try:
                                        self._sock.close()
                                    except Exception:
                                        pass
                                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                                sock = self._sock
                            sock.sendto(packet, (target_ip, target_port))
                            pps_count += 1
                        except Exception as retry_exc:
                            logger.error("Retry send error on unbound socket: %s", retry_exc)
                    except Exception as exc:
                        logger.error("Send error: %s", exc)

            # PPS stat reporting (every 1 second)
            elapsed = now - pps_window_start
            if elapsed >= 1.0:
                if self._on_stats:
                    try:
                        self._on_stats(int(pps_count / elapsed))
                    except Exception:
                        pass
                pps_count = 0
                pps_window_start = now

            # Strict pacing
            next_tick += interval
            sleep_time = next_tick - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -interval:
                # We're behind — reset tick to now to avoid spiral
                next_tick = time.monotonic()


# ---------------------------------------------------------------------------
# Control / Discovery listener (port 5005)
# ---------------------------------------------------------------------------

class ControlListener:
    """
    Listens on UDP port 5005 for BEACON, STATS, and CONFIG_SYNC messages.
    """

    def __init__(
        self,
        on_beacon: Optional[Callable[[dict], None]] = None,
        on_stats: Optional[Callable[[dict], None]] = None,
        on_config_sync: Optional[Callable[[dict], None]] = None,
        bind_port: int = CONTROL_PORT,
    ):
        self._on_beacon = on_beacon
        self._on_stats = on_stats
        self._on_config_sync = on_config_sync
        self._bind_port = bind_port
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("", self._bind_port))
            self._sock.settimeout(0.5)
        except Exception as exc:
            logger.error("Failed to bind control port %d: %s", self._bind_port, exc)
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="ControlListener", daemon=True
        )
        self._thread.start()
        logger.info("ControlListener started on port %d", self._bind_port)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock:
            self._sock.close()
            self._sock = None

    def _run(self) -> None:
        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
                try:
                    msg = json.loads(data.decode("utf-8"))
                    msg["_sender_ip"] = addr[0]
                    msg_type = msg.get("type", "")
                    if msg_type == "BEACON":
                        try:
                            reply = json.dumps({
                                "type": "PC_ANNOUNCE",
                                "app": "PhoneSpeakerPC",
                                "version": 2,
                                "pc_name": socket.gethostname(),
                            }).encode("utf-8")
                            self._sock.sendto(reply, (addr[0], self._bind_port))
                        except Exception:
                            pass
                        if self._on_beacon:
                            self._on_beacon(msg)
                    elif msg_type == "STATS" and self._on_stats:
                        now_ms = current_timestamp_ms()
                        echo_ts = msg.get("echo_pc_ts", 0)
                        hold_ms = msg.get("hold_ms", 0)
                        if isinstance(echo_ts, int) and echo_ts > 0:
                            rtt = ((now_ms - echo_ts) & 0xFFFFFFFF) - int(hold_ms)
                            if 0 <= rtt <= 500:
                                msg["_net_oneway_ms"] = max(1.0, rtt / 2.0)
                        try:
                            ping_pkt = json.dumps({
                                "type": "PING",
                                "pc_ts": now_ms,
                                "pc_name": socket.gethostname(),
                            }).encode("utf-8")
                            self._sock.sendto(ping_pkt, (addr[0], self._bind_port))
                        except Exception:
                            pass
                        self._on_stats(msg)
                    elif msg_type == "CONFIG_SYNC" and self._on_config_sync:
                        self._on_config_sync(msg)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    logger.debug("Control message parse error from %s: %s", addr, exc)
            except socket.timeout:
                continue
            except Exception as exc:
                if self._running:
                    logger.error("ControlListener error: %s", exc)


_ctrl_sock_lock = threading.Lock()
_ctrl_sock: Optional[socket.socket] = None

def send_control_message(target_ip: str, msg: dict, port: int = CONTROL_PORT) -> bool:
    """Send a JSON control message over UDP to target_ip:port using a persistent socket."""
    global _ctrl_sock
    if not target_ip:
        return False
    try:
        payload = dict(msg)
        payload.setdefault("app", "PhoneSpeakerPC")
        payload.setdefault("pc_name", socket.gethostname())
        data = json.dumps(payload).encode("utf-8")
        with _ctrl_sock_lock:
            if _ctrl_sock is None:
                _ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _ctrl_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                _ctrl_sock.sendto(data, (target_ip, port))
            except OSError:
                try:
                    _ctrl_sock.close()
                except Exception:
                    pass
                _ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _ctrl_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                _ctrl_sock.sendto(data, (target_ip, port))
        return True
    except Exception as exc:
        logger.debug("Error sending control message to %s:%d: %s", target_ip, port, exc)
        return False
