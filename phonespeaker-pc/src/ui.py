"""
ui.py — Tkinter-based GUI for PhoneSpeaker v2.0 Studio.

Theme: black background, white foreground/borders, Consolas font.
Tabs: Stream / Roles | Audio & Buffer Settings | Windows Virtual Device
"""

import collections
import json
import logging
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from win_volume import get_system_volume_and_mute, set_system_volume_and_mute

from network import (
    ControlListener,
    UDPSender,
    AudioQueue,
    list_real_adapters,
    send_control_message,
)
from protocol import (
    AUDIO_PORT,
    CONTROL_PORT,
    PHONE_AUDIO_PORT,
    CODEC_PCM16,
    CODEC_OPUS,
    DEFAULT_CONFIG,
)
from opus_codec import OpusEncoder
from audio_capture import AudioCapture, list_loopback_devices, list_input_devices
from audio_output import (
    PhoneAudioReceiver,
    list_output_devices,
    normalize_device_name,
)
from virtual_device import (
    detect_virtual_devices,
    open_windows_sound_settings,
    open_app_volume_settings,
    open_classic_sound_control_panel,
    set_virtual_device_friendly_name,
    download_and_install_vbcable,
)
from config import save

logger = logging.getLogger(__name__)

OPUS_BITRATES = ["10", "24", "32", "64", "96", "128", "160", "192", "256", "320"]
FRAME_SIZES_MS = ["5", "10", "20"]

# ---------------------------------------------------------------------------
# Optional tray support
# ---------------------------------------------------------------------------
try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
    _TRAY_AVAILABLE = True
except Exception as exc:
    pystray = None
    try:
        from PIL import Image, ImageDraw, ImageTk
    except Exception:
        Image = None
        ImageDraw = None
        ImageTk = None
    _TRAY_AVAILABLE = False
    logger.warning("System tray icon unavailable (%s) — running without tray", exc)

try:
    import qrcode
    _QR_AVAILABLE = True
except Exception:
    qrcode = None
    _QR_AVAILABLE = False


# ---------------------------------------------------------------------------
# Color / font constants
# ---------------------------------------------------------------------------
BG = "#000000"
PANEL_BG = "#0D0D0D"
FG = "#FFFFFF"
FG_MUTED = "#888888"
ACCENT = "#00FF00"
WARN = "#FF8800"
ERROR_COLOR = "#FF4444"
_MONO_FAMILY = "Menlo" if sys.platform == "darwin" else ("DejaVu Sans Mono" if sys.platform.startswith("linux") else "Consolas")
FONT_MONO = (_MONO_FAMILY, 10)
FONT_MONO_BOLD = (_MONO_FAMILY, 10, "bold")
FONT_MONO_LG = (_MONO_FAMILY, 12, "bold")
FONT_MONO_SM = (_MONO_FAMILY, 9)


def _make_tray_image(size: int = 64) -> Optional["Image.Image"]:
    if Image is None or ImageDraw is None:
        return None
    try:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        body = [(size // 4, size // 3), (size * 2 // 5, size // 3),
                (size * 2 // 3, size // 6), (size * 2 // 3, size * 5 // 6),
                (size * 2 // 5, size * 2 // 3), (size // 4, size * 2 // 3)]
        d.polygon(body, fill="white")
        cx = size * 2 // 3
        cy = size // 2
        for r, lw in [(size // 6, 2), (size // 4, 2)]:
            d.arc([cx - r, cy - r, cx + r, cy + r], -60, 60, fill="white", width=lw)
        return img
    except Exception:
        return None


class _QueueAdapter:
    def __init__(self, audio_queue: AudioQueue, mirror_callback: Optional[Any] = None):
        self._q = audio_queue
        self._mirror = mirror_callback

    def put_nowait(self, frame: bytes) -> None:
        self._q.put(frame)
        if self._mirror:
            try:
                self._mirror(frame)
            except Exception:
                pass

    def qsize(self) -> int:
        return self._q.qsize()


# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------

class PhoneSpeakerApp:
    def __init__(self, root: tk.Tk, cfg: Dict[str, Any]):
        self.root = root
        self.cfg = cfg

        # Streaming & Engine state
        self._streaming = False
        self._normal_streaming = False
        self._party_streaming = False
        self._party_qr_photo = None
        self._pps = 0
        self._rx_pps = 0
        self._peak_level = 0.0
        self._phone_stats: dict = {}
        self._config_rev = int(cfg.get("config_rev", 1))
        self._last_sync_id = int(time.time() * 1000) & 0x7FFFFFFF
        self._suppress_sync = False
        self._delay_history = collections.deque([0.0] * 60, maxlen=60)
        self._target_history = collections.deque([30.0] * 60, maxlen=60)
        self._last_graph_tick = 0.0
        self._connected_phone_name = "Waiting for phone..."
        self._last_os_vol: Optional[float] = None
        self._last_os_mute: Optional[bool] = None
        self.sync_os_vol_var = tk.BooleanVar(value=True)
        self._gain_sync_after_id: Optional[str] = None
        self._phone_audio_engine = self.cfg.get("audio_output_engine", "OBOE_LOW_LATENCY")
        self._phone_audio_fx = bool(self.cfg.get("enable_audio_effects", False))
        self._phone_exclusive_mode = self.cfg.get("exclusive_audio_mode", "MIX_WITH_OTHERS")
        self._phone_headset_disc = bool(self.cfg.get("stop_on_headset_disconnect", False))

        # Core StringVars & IntVars initialized early so all tabs can share them
        self.codec_var = tk.IntVar(value=int(self.cfg.get("codec", CODEC_OPUS)))
        self.bitrate_var = tk.StringVar(value=str(self.cfg.get("opus_bitrate_kbps", 192)))
        self.frame_ms_var = tk.StringVar(value=str(self.cfg.get("frame_ms", 5)))
        self.buffer_mode_var = tk.StringVar(value=self.cfg.get("buffer_mode", "AUTO"))
        self.custom_buffer_var = tk.DoubleVar(value=float(self.cfg.get("custom_buffer_ms", 30.0)))
        self.volume_gain_var = tk.DoubleVar(value=float(self.cfg.get("volume_gain", 1.0)))
        self.muted_var = tk.BooleanVar(value=bool(self.cfg.get("muted", False)))
        self.direction_var = tk.StringVar(value=self.cfg.get("direction", "PC_TO_PHONE"))
        self.audio_profile_var = tk.StringVar(value=self.cfg.get("audio_profile", "BALANCED"))
        self.expected_delay_var = tk.StringVar(value="")
        self.delay_header_var = tk.StringVar(value="Delay: -- ms (Est. Total: -- ms) | Target: 30.0 ms | Loss: 0")
        self._manual_ip_override: Optional[str] = None
        self._last_phone_seen_time: float = 0.0
        self.party_mode_var = tk.BooleanVar(value=False)
        self._party_view_state = "CHOOSER"
        self._party_joined_active = False
        self._party_extend_active = False
        self._party_devices: Dict[str, Dict[str, Any]] = {}  # ip -> {"name": str, "last_seen": float, "port": int}
        self.party_status_var = tk.StringVar(value="🎉 Party Mode: Waiting for devices to join...")
        self.party_role_var = tk.StringVar(value="HOST")
        self.party_join_host_ip_var = tk.StringVar(value="")
        self.party_join_port_var = tk.StringVar(value=str(AUDIO_PORT))
        self.party_join_status_var = tk.StringVar(value="🎧 Joiner Standby: Enter Party Host IP or select a discovered host")
        self.party_extend_var = tk.BooleanVar(value=False)
        self._party_extend_qr_photo = None
        self._party_relay_sock: Optional[socket.socket] = None
        self._discovered_party_hosts: Dict[str, Dict[str, Any]] = {}
        self._party_join_receiver: Optional[PhoneAudioReceiver] = None
        self._party_join_rx_pps = 0
        self._mic_active_requested: bool = False

        # Audio threads & engines
        self._audio_capture: Optional[AudioCapture] = None
        self._udp_sender: Optional[UDPSender] = None
        self._phone_receiver: Optional[PhoneAudioReceiver] = None
        self._control_listener: Optional[ControlListener] = None
        self._opus_encoder: Optional[OpusEncoder] = None
        self._audio_queue: Optional[AudioQueue] = None
        self._queue_adapter: Optional[_QueueAdapter] = None

        self._tray_icon = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._adapter_data: List[Dict] = []
        self._audio_devices: List[Dict] = []
        self._pc_output_devices: List[Dict] = []
        self._last_phone_mic_state: Optional[bool] = None
        self._mic_control_cooldown_until: float = 0.0

        # Setup
        self._setup_window()
        self._build_tabs()
        self._load_config_into_ui()
        self._refresh_adapters()
        self._refresh_audio_devices()
        self._refresh_pc_outputs()

        # Start Phone -> PC receiver on port 5006 automatically
        pc_out_idx = None
        out_sel = self.pc_output_combo.current()
        if 0 <= out_sel < len(self._pc_output_devices):
            pc_out_idx = self._pc_output_devices[out_sel]["index"]
        self._phone_receiver = PhoneAudioReceiver(
            port=PHONE_AUDIO_PORT,
            output_device_index=pc_out_idx,
            on_stats=self._on_rx_pps_update,
        )
        self._phone_receiver.start()

        # Start control listener immediately
        self._control_listener = ControlListener(
            on_beacon=self._on_beacon,
            on_stats=self._on_phone_stats,
            on_config_sync=self._on_config_sync_received,
        )
        self._control_listener.start()

        self._schedule_ui_update()

    def _setup_window(self) -> None:
        self.root.title("PhoneSpeaker v2.0 Studio")
        self.root.configure(bg=BG)
        self.root.resizable(True, True)
        self.root.minsize(560, 650)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Set window / dock icon
        try:
            if ImageTk is not None and Image is not None:
                tray_img = _make_tray_image(64)
                if tray_img is not None:
                    self._app_icon_photo = ImageTk.PhotoImage(tray_img)
                    self.root.iconphoto(True, self._app_icon_photo)
        except Exception as exc:
            logger.debug("Window icon unavailable: %s", exc)

        # Ensure Combobox dropdown listboxes use dark theme
        self.root.option_add("*TCombobox*Listbox*Background", "#141414")
        self.root.option_add("*TCombobox*Listbox*Foreground", FG)
        self.root.option_add("*TCombobox*Listbox*selectBackground", "#2A4B5C")
        self.root.option_add("*TCombobox*Listbox*selectForeground", ACCENT)

        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background="#1A1A1A", foreground=FG_MUTED,
                        font=FONT_MONO_BOLD, padding=[14, 5])
        style.map("TNotebook.Tab",
                  background=[("selected", "#2A2A2A"), ("active", "#222222")],
                  foreground=[("selected", ACCENT), ("active", FG)])
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=FONT_MONO)
        style.configure("TButton", background="#1A1A1A", foreground=FG,
                        font=FONT_MONO_SM, borderwidth=1, focusthickness=0, padding=[8, 4])
        style.map("TButton",
                  background=[("active", "#2A2A2A"), ("pressed", "#333333")],
                  foreground=[("active", ACCENT)])
        style.configure("TCombobox", fieldbackground="#141414", background="#222222",
                        foreground=FG, arrowcolor=ACCENT, font=FONT_MONO_SM)
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#141414")],
                  selectbackground=[("readonly", "#141414")],
                  selectforeground=[("readonly", FG)])
        style.configure("TEntry", fieldbackground="#141414", foreground=FG, font=FONT_MONO)
        style.configure("TScale", background=BG, troughcolor="#222222")

    def _build_tabs(self) -> None:
        # Title banner
        title_frame = tk.Frame(self.root, bg=BG, bd=1, relief="solid")
        title_frame.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(
            title_frame, text="◀ PhoneSpeaker v2.0 Studio ▶",
            bg=BG, fg=FG, font=FONT_MONO_LG
        ).pack(side="left", padx=8, pady=4)

        tk.Button(
            title_frame, text="? Help", bg="#222222", fg=ACCENT,
            font=FONT_MONO_BOLD, relief="ridge", bd=1, padx=6, pady=2,
            command=self._show_help_dialog
        ).pack(side="right", padx=6)

        self.sync_rev_label = tk.Label(
            title_frame, text=f"SYNC REV: {self._config_rev}",
            bg=BG, fg=FG_MUTED, font=FONT_MONO_SM
        )
        self.sync_rev_label.pack(side="right", padx=4)

        # Tab Notebook
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=4)

        self.tab_stream = tk.Frame(self.notebook, bg=BG)
        self.tab_party = tk.Frame(self.notebook, bg=BG)
        self.tab_audio = tk.Frame(self.notebook, bg=BG)
        self.tab_virtual = tk.Frame(self.notebook, bg=BG)

        self.notebook.add(self.tab_stream, text="  Stream / Roles  ")
        self.notebook.add(self.tab_party, text="  🎉 Party Mode  ")
        self.notebook.add(self.tab_audio, text="  Audio & Buffer  ")
        self.notebook.add(self.tab_virtual, text="  Virtual Device (Win+Ctrl+V)  ")

        self._build_tab_stream()
        self._build_tab_party()
        self._build_tab_audio()
        self._build_tab_virtual()

    # ------------------------------------------------------------------
    # TAB 1: Stream / Roles
    # ------------------------------------------------------------------

    def _build_tab_stream(self) -> None:
        f = self.tab_stream
        pad = {"padx": 8, "pady": 3}

        # 1. Connections Card (Connected Phone Model, IP, Volume Bar & Mute Icon)
        conn_card = tk.LabelFrame(
            f, text=" Connections ", bg="#12161A", fg="#38B6FF",
            font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=4
        )
        conn_card.pack(fill="x", padx=8, pady=(4, 3))

        conn_top = tk.Frame(conn_card, bg="#12161A")
        conn_top.pack(fill="x", padx=6, pady=(2, 2))

        tk.Label(
            conn_top, text="📱", bg="#12161A", fg="#38B6FF", font=(_MONO_FAMILY, 16)
        ).pack(side="left", padx=(0, 8))

        conn_info = tk.Frame(conn_top, bg="#12161A")
        conn_info.pack(side="left", fill="x", expand=True)

        self.conn_device_name_var = tk.StringVar(value="Waiting for phone connection...")
        tk.Label(
            conn_info, textvariable=self.conn_device_name_var,
            bg="#12161A", fg="#FFFFFF", font=FONT_MONO_LG, anchor="w"
        ).pack(fill="x")

        self.conn_device_ip_var = tk.StringVar(value="Open PhoneSpeaker on your phone (Auto-Discovery active)")
        tk.Label(
            conn_info, textvariable=self.conn_device_ip_var,
            bg="#12161A", fg="#99AABB", font=FONT_MONO_SM, anchor="w"
        ).pack(fill="x")

        vol_bar_row = tk.Frame(conn_card, bg="#12161A")
        vol_bar_row.pack(fill="x", padx=6, pady=(2, 4))

        self.conn_vol_slider = tk.Scale(
            vol_bar_row, from_=0.0, to=2.0, resolution=0.02, orient="horizontal",
            variable=self.volume_gain_var, showvalue=False,
            bg="#12161A", fg="#38B6FF", troughcolor="#223344",
            activebackground="#38B6FF", highlightthickness=0, bd=0,
            command=self._on_gain_slider_changed
        )
        self.conn_vol_slider.pack(side="left", fill="x", expand=True, padx=(2, 8))

        self.conn_vol_pct_label = tk.Label(
            vol_bar_row, text=f"{int(self.volume_gain_var.get() * 100)}%",
            bg="#12161A", fg="#38B6FF", font=FONT_MONO_BOLD, width=5, anchor="e"
        )
        self.conn_vol_pct_label.pack(side="left", padx=(0, 6))

        self.conn_mute_icon_btn = tk.Button(
            vol_bar_row, text="🔊", bg="#1E262E", fg=FG,
            font=(_MONO_FAMILY, 11), relief="raised", bd=1, padx=6, pady=0,
            command=self._toggle_mute
        )
        self.conn_mute_icon_btn.pack(side="right")

        os_sync_row = tk.Frame(conn_card, bg="#12161A")
        os_sync_row.pack(fill="x", padx=6, pady=(0, 2))
        tk.Checkbutton(
            os_sync_row, text="Link with PC Volume Keys / Windows Sound Bar (100% = Phone's Current Volume)",
            variable=self.sync_os_vol_var, bg="#12161A", fg=FG_MUTED,
            selectcolor="#1E262E", activebackground="#12161A", activeforeground=FG, font=FONT_MONO_SM
        ).pack(side="left")

        # 2. Network & Stream Role Frame (Aligned Grid)
        nf = tk.LabelFrame(f, text=" Network & Stream Role ", bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=3)
        nf.pack(fill="x", **pad)
        nf.columnconfigure(1, weight=1)

        tk.Label(nf, text="Local Adapter:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, width=15, anchor="e").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.adapter_combo = ttk.Combobox(nf, state="readonly", font=FONT_MONO_SM)
        self.adapter_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        self.adapter_combo.bind("<<ComboboxSelected>>", self._on_adapter_selected)
        ttk.Button(nf, text="↻", width=3, command=self._refresh_all_devices).grid(row=0, column=2, padx=(2, 4), pady=2)

        tk.Label(nf, text="Phone IP / Port:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, width=15, anchor="e").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ip_row = tk.Frame(nf, bg=BG)
        ip_row.grid(row=1, column=1, sticky="ew", padx=4, pady=2)
        self.ip_var = tk.StringVar(value=self.cfg.get("phone_ip", ""))
        self.ip_entry = tk.Entry(ip_row, textvariable=self.ip_var, width=16, bg="#141414", fg=FG, insertbackground=FG, relief="solid", bd=1, font=FONT_MONO_SM)
        self.ip_entry.pack(side="left")
        self.ip_entry.bind("<Return>", self._on_manual_ip_applied)
        tk.Label(ip_row, text=" : ", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left")
        self.ip_port_var = tk.StringVar(value=str(self.cfg.get("phone_port", AUDIO_PORT)))
        self.port_entry = tk.Entry(ip_row, textvariable=self.ip_port_var, width=6, bg="#141414", fg=FG, insertbackground=FG, relief="solid", bd=1, font=FONT_MONO_SM)
        self.port_entry.pack(side="left")
        self.port_entry.bind("<Return>", self._on_manual_ip_applied)
        ttk.Button(ip_row, text="Apply IP", command=self._on_manual_ip_applied).pack(side="left", padx=(6, 0))

        tk.Label(nf, text="Stream Mode:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, width=15, anchor="e").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        role_row = tk.Frame(nf, bg=BG)
        role_row.grid(row=2, column=1, sticky="w", padx=2, pady=2)
        for text, val in [
            ("PC Sound → Phone", "PC_TO_PHONE"),
            ("Phone Mic → PC", "PHONE_TO_PC"),
            ("Both (Speaker + Mic)", "FULL_DUPLEX"),
        ]:
            tk.Radiobutton(
                role_row, text=text, variable=self.direction_var, value=val,
                bg=BG, fg=FG, selectcolor="#222222", font=FONT_MONO_SM,
                activebackground=BG, activeforeground=ACCENT,
                command=self._on_role_change
            ).pack(side="left", padx=(0, 8))

        # 3. Phone Mic Output Device on PC (Port 5006)
        of = tk.LabelFrame(f, text=" Phone Mic Output on PC (Port 5006 → Discord / Zoom / Speakers) ", bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=3)
        of.pack(fill="x", **pad)
        of.columnconfigure(1, weight=1)

        tk.Label(of, text="Mic Output Dev:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, width=15, anchor="e").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.pc_output_combo = ttk.Combobox(of, state="readonly", font=FONT_MONO_SM)
        self.pc_output_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        self.pc_output_combo.bind("<<ComboboxSelected>>", self._on_pc_output_selected)
        ttk.Button(of, text="Auto-Route Virtual Mic", command=self._auto_route_virtual_mic).grid(row=0, column=2, padx=(4, 2), pady=2)

        # 4. Audio Codec & Bitrate (Sound Mode + Single Aligned Row + Expected Delay)
        qf_tab1 = tk.LabelFrame(f, text=" Audio Codec & Bitrate ", bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=3)
        qf_tab1.pack(fill="x", **pad)

        prof_row = tk.Frame(qf_tab1, bg=BG)
        prof_row.pack(fill="x", padx=4, pady=(2, 2))
        tk.Label(prof_row, text="Sound Mode:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left")
        for text, val in [
            ("🎵 Best Quality (Music)", "QUALITY"),
            ("⚡ Low Delay (Gaming)", "GAMING"),
            ("⚖️ Balanced (Auto)", "BALANCED"),
            ("🎛️ Custom", "CUSTOM"),
        ]:
            tk.Radiobutton(
                prof_row, text=text, variable=self.audio_profile_var, value=val,
                bg=BG, fg=ACCENT if val != "CUSTOM" else FG, selectcolor="#222222",
                font=FONT_MONO_SM, activebackground=BG, activeforeground=ACCENT,
                command=self._on_audio_profile_selected
            ).pack(side="left", padx=(4, 4))

        cb_row = tk.Frame(qf_tab1, bg=BG)
        cb_row.pack(fill="x", padx=4, pady=2)
        tk.Label(cb_row, text="Codec:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left")
        tk.Radiobutton(cb_row, text="Opus", variable=self.codec_var, value=CODEC_OPUS,
                       bg=BG, fg=FG, selectcolor="#222222", font=FONT_MONO_SM,
                       command=self._on_codec_changed).pack(side="left", padx=(4, 6))
        tk.Radiobutton(cb_row, text="PCM16 (Lossless)", variable=self.codec_var, value=CODEC_PCM16,
                       bg=BG, fg=FG, selectcolor="#222222", font=FONT_MONO_SM,
                       command=self._on_codec_changed).pack(side="left", padx=(0, 12))

        tk.Label(cb_row, text="Bitrate:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left")
        self.bitrate_combo_tab1 = ttk.Combobox(cb_row, textvariable=self.bitrate_var, values=OPUS_BITRATES, width=5, state="readonly", font=FONT_MONO_SM)
        self.bitrate_combo_tab1.pack(side="left", padx=4)
        self.bitrate_combo_tab1.bind("<<ComboboxSelected>>", lambda e: self._on_bitrate_changed())
        tk.Label(cb_row, text="kbps", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left")

        tk.Label(qf_tab1, textvariable=self.expected_delay_var, bg=BG, fg="#00EEEE", font=FONT_MONO_SM, anchor="w").pack(fill="x", padx=4, pady=(1, 2))

        # 5. Independent Action Buttons: Speaker Stream (5004) & Mic Receiver (5006)
        btn_container = tk.Frame(f, bg=BG)
        btn_container.pack(fill="x", padx=8, pady=4)

        self.start_btn = tk.Button(
            btn_container, text="▶  START SPEAKER STREAM (PC → PHONE :5004)",
            bg="#002B00", fg=ACCENT, font=FONT_MONO_BOLD,
            activebackground="#004400", activeforeground=ACCENT,
            relief="raised", bd=2, padx=8, pady=4, cursor="hand2",
            command=self._toggle_streaming,
        )
        self.start_btn.pack(fill="x", pady=(0, 4))

        self.mic_btn = tk.Button(
            btn_container, text="🎤  START MIC STREAM / RECEIVER (PHONE → PC :5006)",
            bg="#112222", fg="#00EEEE", font=FONT_MONO_BOLD,
            activebackground="#003B55", activeforeground="#00EEEE",
            relief="raised", bd=2, padx=8, pady=4, cursor="hand2",
            command=self._toggle_mic_receiver,
        )
        self.mic_btn.pack(fill="x")

        # 6. Live Status, Level Bar, Aligned 2-Col Telemetry & Live Delay Graph
        status_frame = tk.LabelFrame(f, text=" Live Telemetry & Delay Graph ", bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=3)
        status_frame.pack(fill="both", expand=True, **pad)

        self.status_var = tk.StringVar(value="SPEAKER: IDLE | MIC RX: READY (:5006)")
        self.status_label = tk.Label(status_frame, textvariable=self.status_var, bg=BG, fg=ACCENT, font=FONT_MONO_BOLD, anchor="w")
        self.status_label.pack(fill="x", padx=4, pady=(1, 2))

        level_frame = tk.Frame(status_frame, bg=BG)
        level_frame.pack(fill="x", padx=4, pady=2)
        tk.Label(level_frame, text="Audio Level:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left")
        self._level_canvas = tk.Canvas(level_frame, height=12, bg="#111111", highlightthickness=1, highlightbackground="#444444")
        self._level_canvas.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._level_bar = self._level_canvas.create_rectangle(0, 0, 0, 12, fill=ACCENT, outline="")

        sg = tk.Frame(status_frame, bg="#0A0A0A", bd=1, relief="solid")
        sg.pack(fill="x", padx=4, pady=3)
        sg.columnconfigure(0, weight=1, uniform="stat")
        sg.columnconfigure(1, weight=1, uniform="stat")

        self.pps_var = tk.StringVar(value="Speaker Packets: 0/s")
        self.rx_pps_var = tk.StringVar(value="Mic Packets: 0/s")
        self.latency_var = tk.StringVar(value="Phone Buffer: --")
        self.loss_var = tk.StringVar(value="Dropped: --")
        self.phone_ip_var = tk.StringVar(value="Phone: --")
        self.mic_state_var = tk.StringVar(value="Phone Mic: READY")

        for i, var in enumerate([self.pps_var, self.rx_pps_var, self.latency_var, self.loss_var, self.phone_ip_var, self.mic_state_var]):
            tk.Label(sg, textvariable=var, bg="#0A0A0A", fg=FG, font=FONT_MONO_SM, anchor="w", padx=6, pady=1).grid(
                row=i // 2, column=i % 2, sticky="ew"
            )

        tk.Label(status_frame, textvariable=self.delay_header_var, bg=BG, fg="#00EEEE", font=FONT_MONO_SM, anchor="w").pack(fill="x", padx=4, pady=(2, 1))

        self._delay_canvas = tk.Canvas(status_frame, width=480, height=68, bg="#080808", highlightthickness=1, highlightbackground="#333333")
        self._delay_canvas.pack(fill="both", expand=True, padx=4, pady=(1, 3))

    # ------------------------------------------------------------------
    # TAB 2: Dedicated Party Mode
    # ------------------------------------------------------------------

    def _build_tab_party(self) -> None:
        f = self.tab_party
        pad = {"padx": 8, "pady": 4}

        # -------------------------------------------------------------
        # 1. INITIAL SCREEN: CHOOSER FRAME (Only 2 Big Buttons)
        # -------------------------------------------------------------
        self.party_chooser_frame = tk.Frame(f, bg=BG)
        self.party_chooser_frame.pack(fill="both", expand=True)

        chooser_card = tk.LabelFrame(
            self.party_chooser_frame, text=" 🎉 PARTY MODE — MULTI-DEVICE WIRELESS AUDIO ",
            bg=BG, fg="#38B6FF", font=FONT_MONO_BOLD, bd=1, relief="solid", padx=16, pady=16
        )
        chooser_card.pack(fill="both", expand=True, padx=16, pady=16)

        tk.Label(
            chooser_card,
            text="Independent from Normal 1-to-1 Mode.\nChoose how you want to use Party Mode:",
            bg=BG, fg=FG, font=FONT_MONO, justify="center"
        ).pack(pady=(10, 16))

        btn_choose_host = tk.Button(
            chooser_card,
            text="👑  HOST A PARTY\n(Generate Host IP & QR Code • Broadcast PC Sound to Multiple Phones)",
            bg="#002B00", fg="#00FF00", font=FONT_MONO_BOLD,
            activebackground="#004400", activeforeground="#00FF00",
            relief="raised", bd=2, padx=12, pady=16, cursor="hand2",
            command=self._enter_party_host_mode
        )
        btn_choose_host.pack(fill="x", padx=12, pady=(8, 12))

        btn_choose_join = tk.Button(
            chooser_card,
            text="🎧  JOIN A PARTY\n(Scan for Hosts on Wi-Fi / Hotspot • Listen & Optionally Extend via Hotspot)",
            bg="#001822", fg="#00EEEE", font=FONT_MONO_BOLD,
            activebackground="#002D3F", activeforeground="#00EEEE",
            relief="raised", bd=2, padx=12, pady=16, cursor="hand2",
            command=self._enter_party_join_mode
        )
        btn_choose_join.pack(fill="x", padx=12, pady=(6, 16))

        tk.Label(
            chooser_card,
            text="💡 Tip: Normal Mode (Tab 1) streams directly PC ↔ Phone.\n"
                 "Party Mode lets everyone connect to one host and daisy-chain via hotspot.",
            bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, justify="center"
        ).pack(pady=(8, 0))

        # -------------------------------------------------------------
        # 2. HOST FRAME (Visible ONLY when user chooses HOST)
        # -------------------------------------------------------------
        self.party_host_frame = tk.Frame(f, bg=BG)
        # Hidden by default

        host_top_bar = tk.Frame(self.party_host_frame, bg=BG)
        host_top_bar.pack(fill="x", padx=8, pady=(4, 2))
        ttk.Button(host_top_bar, text="← Back to Party Menu", command=self._back_to_party_chooser).pack(side="left")
        tk.Label(host_top_bar, text="👑 Party Host Mode (Broadcasting to Multiple Devices)", bg=BG, fg="#00FF00", font=FONT_MONO_BOLD).pack(side="left", padx=8)

        # Host Banner & Host Info + Live QR Code
        banner = tk.LabelFrame(
            self.party_host_frame, text=" 👑 Host Controls & Live QR Code ",
            bg="#101820", fg="#00EEEE", font=FONT_MONO_BOLD, bd=1, relief="solid", padx=8, pady=6
        )
        banner.pack(fill="x", **pad)

        top_host_frame = tk.Frame(banner, bg="#101820")
        top_host_frame.pack(fill="x", pady=2)

        host_info_left = tk.Frame(top_host_frame, bg="#101820")
        host_info_left.pack(side="left", fill="both", expand=True)

        self.party_host_ip_label = tk.Label(
            host_info_left, text="HOST IP ADDRESS: Loading...",
            bg="#101820", fg="#00FF00", font=FONT_MONO_BOLD, anchor="w"
        )
        self.party_host_ip_label.pack(fill="x")

        tk.Label(
            host_info_left,
            text="Any phone on your Wi-Fi, PC Hotspot, or USB Tether can join\nthis party to hear PC sound in real-time sync with other phones!",
            bg="#101820", fg=FG, font=FONT_MONO_SM, justify="left", anchor="w"
        ).pack(fill="x", pady=(2, 6))

        self.party_start_btn = tk.Button(
            host_info_left, text="👑  START PARTY BROADCAST (HOST)",
            bg="#002B00", fg=ACCENT, font=FONT_MONO_BOLD,
            activebackground="#004400", activeforeground=ACCENT,
            relief="raised", bd=2, padx=8, pady=5, cursor="hand2",
            command=self._toggle_party_broadcast,
        )
        self.party_start_btn.pack(fill="x", pady=(2, 2))

        # QR Code frame on the right
        qr_frame = tk.Frame(top_host_frame, bg="#101820", padx=6)
        qr_frame.pack(side="right")
        self.party_qr_label = tk.Label(qr_frame, bg="#FFFFFF", width=120, height=120)
        self.party_qr_label.pack()
        tk.Label(qr_frame, text="Scan with Phone\n(Click to copy link)", bg="#101820", fg=FG_MUTED, font=FONT_MONO_SM).pack(pady=(2, 0))

        # Live Joined Party Devices List
        lf = tk.LabelFrame(
            self.party_host_frame, text=" Joined Party Devices (Auto-Discovered + Manual) ",
            bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=8, pady=6
        )
        lf.pack(fill="both", expand=True, **pad)

        tk.Label(
            lf, textvariable=self.party_status_var,
            bg="#0D151A", fg="#38B6FF", font=FONT_MONO_BOLD, anchor="w", padx=6, pady=4
        ).pack(fill="x", pady=(0, 4))

        list_frame = tk.Frame(lf, bg=BG)
        list_frame.pack(fill="both", expand=True, pady=2)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        self.party_listbox = tk.Listbox(
            list_frame, bg="#0A0A0A", fg=ACCENT, selectbackground="#1E3A5F",
            selectforeground="#FFFFFF", font=FONT_MONO, height=5, bd=1, relief="solid",
            yscrollcommand=scrollbar.set
        )
        self.party_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.party_listbox.yview)

        btn_row = tk.Frame(lf, bg=BG)
        btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(btn_row, text="❌ Remove Selected Device", command=self._remove_selected_party_device).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="🗑 Clear All Party Devices", command=self._clear_party_devices).pack(side="left")
        ttk.Button(btn_row, text="🔄 Sync All Settings Now", command=self._on_local_setting_changed).pack(side="right")

        # Manually Add Device to Party
        af = tk.LabelFrame(
            self.party_host_frame, text=" Add Device by IP (Hotspot / Manual Join) ",
            bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=8, pady=6
        )
        af.pack(fill="x", **pad)

        add_row = tk.Frame(af, bg=BG)
        add_row.pack(fill="x")
        tk.Label(add_row, text="Device IP:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left", padx=(0, 4))
        self.party_add_ip_var = tk.StringVar(value="")
        self.party_add_ip_entry = tk.Entry(
            add_row, textvariable=self.party_add_ip_var, width=18,
            bg="#141414", fg=FG, insertbackground=FG, relief="solid", bd=1, font=FONT_MONO_SM
        )
        self.party_add_ip_entry.pack(side="left", padx=(0, 6))
        self.party_add_ip_entry.bind("<Return>", self._on_add_party_device_manual)

        tk.Label(add_row, text="Name:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left", padx=(0, 4))
        self.party_add_name_var = tk.StringVar(value="")
        tk.Entry(
            add_row, textvariable=self.party_add_name_var, width=14,
            bg="#141414", fg=FG, insertbackground=FG, relief="solid", bd=1, font=FONT_MONO_SM
        ).pack(side="left", padx=(0, 6))

        ttk.Button(add_row, text="+ Add to Party", command=self._on_add_party_device_manual).pack(side="left")

        # Hotspot Quick Guide
        hf = tk.LabelFrame(
            self.party_host_frame, text=" How Party Mode Works with Mobile Hotspot ",
            bg=BG, fg=FG_MUTED, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=8, pady=4
        )
        hf.pack(fill="x", **pad)
        tk.Label(
            hf,
            text="• Phone Hotspot: Turn on Hotspot on Phone 1 -> Connect PC & other phones to it -> Open app on all phones!\n"
                 "• PC Hotspot / Wi-Fi: Connect all phones to the same Wi-Fi or PC Hotspot -> They auto-join here within 1s!",
            bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, justify="left", anchor="w"
        ).pack(fill="x")

        # -------------------------------------------------------------
        # 3. JOIN FRAME (Visible ONLY when user chooses JOIN)
        # -------------------------------------------------------------
        self.party_join_frame = tk.Frame(f, bg=BG)
        # Hidden by default

        join_top_bar = tk.Frame(self.party_join_frame, bg=BG)
        join_top_bar.pack(fill="x", padx=8, pady=(4, 2))
        ttk.Button(join_top_bar, text="← Back to Party Menu", command=self._back_to_party_chooser).pack(side="left")
        tk.Label(join_top_bar, text="🎧 Party Joiner Mode (Listen to Host)", bg=BG, fg="#00EEEE", font=FONT_MONO_BOLD).pack(side="left", padx=8)

        # Discovered Hosts Section
        jf_disc = tk.LabelFrame(
            self.party_join_frame, text=" 📡 Discovered Party Hosts on Network (Auto-Found on Wi-Fi / Hotspot) ",
            bg=BG, fg="#00EEEE", font=FONT_MONO_BOLD, bd=1, relief="solid", padx=8, pady=6
        )
        jf_disc.pack(fill="both", expand=True, **pad)

        tk.Label(
            jf_disc, text="Double-click or click a host below to connect to its party broadcast:",
            bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, anchor="w"
        ).pack(fill="x", pady=(0, 4))

        disc_list_frame = tk.Frame(jf_disc, bg=BG)
        disc_list_frame.pack(fill="both", expand=True, pady=2)

        disc_scrollbar = ttk.Scrollbar(disc_list_frame, orient="vertical")
        disc_scrollbar.pack(side="right", fill="y")

        self.party_join_discovered_listbox = tk.Listbox(
            disc_list_frame, bg="#0A0A0A", fg="#00EEEE", selectbackground="#1E3A5F",
            selectforeground="#FFFFFF", font=FONT_MONO, height=5, bd=1, relief="solid",
            yscrollcommand=disc_scrollbar.set
        )
        self.party_join_discovered_listbox.pack(side="left", fill="both", expand=True)
        disc_scrollbar.config(command=self.party_join_discovered_listbox.yview)
        self.party_join_discovered_listbox.bind("<Double-Button-1>", self._on_select_discovered_host)
        self.party_join_discovered_listbox.bind("<<ListboxSelect>>", self._on_select_discovered_host)

        disc_btn_row = tk.Frame(jf_disc, bg=BG)
        disc_btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(disc_btn_row, text="🎯 Connect / Use Selected Host", command=self._on_select_discovered_host).pack(side="left")
        ttk.Button(disc_btn_row, text="↻ Rescan Hosts", command=self._refresh_discovered_hosts_ui).pack(side="left", padx=6)

        # Host Address & QR Link / Paste Section
        jf_addr = tk.LabelFrame(
            self.party_join_frame, text=" Party Host Address (Manual or QR Code / Link) ",
            bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=8, pady=6
        )
        jf_addr.pack(fill="x", **pad)

        addr_row = tk.Frame(jf_addr, bg=BG)
        addr_row.pack(fill="x", pady=2)

        tk.Label(addr_row, text="Host IP:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left", padx=(0, 4))
        self.party_join_host_ip_entry = tk.Entry(
            addr_row, textvariable=self.party_join_host_ip_var, width=18,
            bg="#141414", fg=FG, insertbackground=FG, relief="solid", bd=1, font=FONT_MONO_SM
        )
        self.party_join_host_ip_entry.pack(side="left", padx=(0, 6))

        tk.Label(addr_row, text="Port:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left", padx=(0, 4))
        self.party_join_port_entry = tk.Entry(
            addr_row, textvariable=self.party_join_port_var, width=6,
            bg="#141414", fg=FG, insertbackground=FG, relief="solid", bd=1, font=FONT_MONO_SM
        )
        self.party_join_port_entry.pack(side="left", padx=(0, 6))

        ttk.Button(addr_row, text="📋 Paste QR / Join Link", command=self._on_paste_party_join_link).pack(side="left", padx=(0, 6))

        # Join Action Button & Status
        self.party_join_btn = tk.Button(
            self.party_join_frame, text="🎧  JOIN PARTY STREAM (LISTEN TO HOST)",
            bg="#002B00", fg=ACCENT, font=FONT_MONO_BOLD,
            activebackground="#004400", activeforeground=ACCENT,
            relief="raised", bd=2, padx=8, pady=6, cursor="hand2",
            command=self._toggle_party_join,
        )
        self.party_join_btn.pack(fill="x", padx=8, pady=6)

        self.party_join_status_label = tk.Label(
            self.party_join_frame, textvariable=self.party_join_status_var,
            bg="#0D151A", fg="#38B6FF", font=FONT_MONO_BOLD, anchor="w", padx=8, pady=6, bd=1, relief="solid"
        )
        self.party_join_status_label.pack(fill="x", padx=8, pady=(0, 4))

        # 4. POST-JOIN EXTEND PARTY CARD (Hidden until successfully joined a host!)
        self.party_extend_card = tk.LabelFrame(
            self.party_join_frame, text=" 📡 Extend This Party (Hotspot Repeater) ",
            bg=BG, fg="#00EEEE", font=FONT_MONO_BOLD, bd=1, relief="solid", padx=8, pady=6
        )
        # Hidden initially! Packed when user has joined a party!

        tk.Label(
            self.party_extend_card,
            text="You have joined the party! Turn on your PC Hotspot / Wi-Fi and click Extend below to\n"
                 "generate your own Extended Host IP & QR code so other phones can connect to you:",
            bg=BG, fg=FG, font=FONT_MONO_SM, justify="left", anchor="w"
        ).pack(fill="x", pady=(0, 4))

        self.btn_party_extend = tk.Button(
            self.party_extend_card,
            text="[ 📡 EXTEND PARTY (GENERATE HOTSPOT QR & IP) ]",
            bg="#002B00", fg="#00EEEE", font=FONT_MONO_BOLD,
            activebackground="#004400", activeforeground="#00EEEE",
            relief="raised", bd=2, padx=8, pady=5, cursor="hand2",
            command=self._toggle_party_extend_pc
        )
        self.btn_party_extend.pack(fill="x", pady=3)

        self.party_extend_details_frame = tk.Frame(self.party_extend_card, bg=BG)
        # Packed when extend is active!

        self.party_extend_ip_label = tk.Label(
            self.party_extend_details_frame, text="EXTENDED HOST IP: Detecting...",
            bg="#102418", fg=ACCENT, font=FONT_MONO_BOLD, anchor="w", padx=8, pady=4, bd=1, relief="solid"
        )
        self.party_extend_ip_label.pack(fill="x", pady=2)

        self.party_extend_qr_label = tk.Label(self.party_extend_details_frame, bg="#FFFFFF")
        self.party_extend_qr_label.pack(pady=4)

        ttk.Button(self.party_extend_details_frame, text="↻ Refresh Extended IP & QR Code", command=self._refresh_party_extend_qr).pack(pady=2)

        self.party_extend_peers_label = tk.Label(
            self.party_extend_details_frame, text="Extended Party Peers: Waiting for friends to connect/join...",
            bg="#081018", fg=FG_MUTED, font=FONT_MONO_SM, anchor="w", padx=8, pady=4
        )
        self.party_extend_peers_label.pack(fill="x", pady=2)

    # ------------------------------------------------------------------
    # TAB 3: Audio & Buffer Settings (Two-Way Synced)
    # ------------------------------------------------------------------

    def _build_tab_audio(self) -> None:
        f = self.tab_audio
        pad = {"padx": 8, "pady": 4}

        # Codec & Quality Frame
        qf = tk.LabelFrame(f, text=" Audio Quality & Codec ", bg=BG, fg=FG, font=FONT_MONO, bd=1, relief="solid")
        qf.pack(fill="x", **pad)

        tk.Label(qf, text="Codec:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).grid(row=0, column=0, sticky="e", padx=4, pady=3)
        codec_box = tk.Frame(qf, bg=BG)
        codec_box.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        tk.Radiobutton(codec_box, text="Compressed (Opus)", variable=self.codec_var, value=CODEC_OPUS,
                       bg=BG, fg=FG, selectcolor="#333333", font=FONT_MONO, command=self._on_codec_changed).pack(side="left")
        tk.Radiobutton(codec_box, text="Lossless PCM16", variable=self.codec_var, value=CODEC_PCM16,
                       bg=BG, fg=FG, selectcolor="#333333", font=FONT_MONO, command=self._on_codec_changed).pack(side="left", padx=8)

        tk.Label(qf, text="Opus Bitrate:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).grid(row=1, column=0, sticky="e", padx=4, pady=3)
        self.bitrate_combo_tab2 = ttk.Combobox(qf, textvariable=self.bitrate_var, values=OPUS_BITRATES, width=8, state="readonly", font=FONT_MONO)
        self.bitrate_combo_tab2.grid(row=1, column=1, sticky="w", padx=4, pady=3)
        self.bitrate_combo_tab2.bind("<<ComboboxSelected>>", lambda e: self._on_bitrate_changed())

        tk.Label(qf, text="Frame Size:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).grid(row=2, column=0, sticky="e", padx=4, pady=3)
        self.frame_combo_tab2 = ttk.Combobox(qf, textvariable=self.frame_ms_var, values=FRAME_SIZES_MS, width=8, state="readonly", font=FONT_MONO)
        self.frame_combo_tab2.grid(row=2, column=1, sticky="w", padx=4, pady=3)
        self.frame_combo_tab2.bind("<<ComboboxSelected>>", lambda e: self._on_frame_ms_changed())

        # Expected delay label
        tk.Label(qf, textvariable=self.expected_delay_var, bg=BG, fg="#00EEEE", font=FONT_MONO_SM, anchor="w").grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=2)

        # Buffer Presets & Target Frame
        bf = tk.LabelFrame(f, text=" Playout Buffer Modes (Phone Playout) ", bg=BG, fg=FG, font=FONT_MONO, bd=1, relief="solid")
        bf.pack(fill="x", **pad)

        buf_modes = [
            ("Low (20 ms) — Video & Gaming", "LOW"),
            ("Medium (40 ms) — Balanced", "MEDIUM"),
            ("High (80 ms) — Maximum Music Stability", "HIGH"),
            ("Auto Adaptive (+5 ms / -2.5 ms)", "AUTO"),
            ("Custom Target Buffer", "CUSTOM"),
        ]
        for text, val in buf_modes:
            tk.Radiobutton(
                bf, text=text, variable=self.buffer_mode_var, value=val,
                bg=BG, fg=FG, selectcolor="#333333", font=FONT_MONO,
                command=self._on_buffer_mode_changed
            ).pack(anchor="w", padx=8, pady=1)

        custom_row = tk.Frame(bf, bg=BG)
        custom_row.pack(fill="x", padx=8, pady=4)
        tk.Label(custom_row, text="Target Buffer:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left")
        self.buffer_slider = tk.Scale(
            custom_row, from_=10.0, to=200.0, resolution=2.5, orient="horizontal",
            variable=self.custom_buffer_var, showvalue=False,
            bg=BG, fg=FG, troughcolor="#222222", highlightthickness=0, bd=0, length=150,
            command=self._on_buffer_slider_changed
        )
        self.buffer_slider.pack(side="left", padx=4)
        self.buffer_val_label = tk.Label(custom_row, text=f"{self.custom_buffer_var.get():.1f} ms", bg=BG, fg=ACCENT, font=FONT_MONO_SM)
        self.buffer_val_label.pack(side="left")

        # Volume / Gain Boost
        vf = tk.LabelFrame(f, text=" Volume & Boost Control ", bg=BG, fg=FG, font=FONT_MONO, bd=1, relief="solid")
        vf.pack(fill="x", **pad)

        v_row = tk.Frame(vf, bg=BG)
        v_row.pack(fill="x", padx=8, pady=4)
        tk.Label(v_row, text="Gain Boost:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).pack(side="left")
        self.gain_slider = tk.Scale(
            v_row, from_=0.0, to=2.0, resolution=0.05, orient="horizontal",
            variable=self.volume_gain_var, showvalue=False,
            bg=BG, fg=FG, troughcolor="#222222", highlightthickness=0, bd=0, length=140,
            command=self._on_gain_slider_changed
        )
        self.gain_slider.pack(side="left", padx=4)
        self.gain_val_label = tk.Label(v_row, text=f"{int(self.volume_gain_var.get() * 100)}%", bg=BG, fg=ACCENT, font=FONT_MONO_SM)
        self.gain_val_label.pack(side="left")

        self.mute_btn = tk.Button(
            v_row, text="MUTE", bg="#333333", fg=FG, font=FONT_MONO_SM,
            command=self._toggle_mute
        )
        self.mute_btn.pack(side="right", padx=8)

    # ------------------------------------------------------------------
    # TAB 3: Windows Virtual Device & Tools
    # ------------------------------------------------------------------

    def _build_tab_virtual(self) -> None:
        f = self.tab_virtual
        pad = {"padx": 8, "pady": 4}
        is_linux = sys.platform.startswith("linux")
        is_mac = (sys.platform == "darwin")

        if is_mac:
            vf_title = " macOS CoreAudio & BlackHole Virtual Audio "
        elif is_linux:
            vf_title = " Linux PipeWire / PulseAudio Virtual Devices "
        else:
            vf_title = " Windows Virtual Audio Cable & Sound Settings "

        vf = tk.LabelFrame(f, text=vf_title, bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=4)
        vf.pack(fill="x", **pad)

        desc_text = (
            "Use BlackHole 2ch to route your Phone Mic into Discord, Zoom, FaceTime, or OBS,\n"
            "or capture system audio via Multi-Output Device into PhoneSpeaker:"
            if is_mac else (
                "Create virtual speaker & microphone sinks via pactl so you can stream isolated\nPC audio to your phone or route your Phone Mic into Discord, Zoom, or OBS:"
                if is_linux else
                "Use VB-CABLE / Virtual Audio Driver to route your Phone Mic into Discord, Zoom, Teams, or OBS,\nor to stream isolated PC audio without playing through PC speakers:"
            )
        )
        tk.Label(vf, text=desc_text, bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, justify="left", anchor="w").pack(fill="x", padx=6, pady=(2, 4))

        self.virt_status_label = tk.Label(vf, text="Scanning virtual audio devices...", bg="#0D151A", fg=ACCENT, font=FONT_MONO_BOLD, anchor="w", padx=6, pady=4)
        self.virt_status_label.pack(fill="x", padx=6, pady=2)

        btn_row1 = tk.Frame(vf, bg=BG)
        btn_row1.pack(fill="x", padx=6, pady=4)
        install_lbl = "📥 Install BlackHole 2ch (CoreAudio)" if is_mac else ("📥 Create Virtual Speaker & Mic (pactl)" if is_linux else "📥 Install Virtual Audio Driver (VB-CABLE)")
        ttk.Button(btn_row1, text=install_lbl, command=self._install_driver).pack(side="left", padx=(0, 6))
        if not is_linux and not is_mac:
            ttk.Button(btn_row1, text="🏷 Rename Cable to PhoneSpeaker Audio", command=self._rename_device).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row1, text="↻ Rescan Devices", command=self._refresh_all_devices).pack(side="left")

        btn_row2 = tk.Frame(vf, bg=BG)
        btn_row2.pack(fill="x", padx=6, pady=(0, 4))
        sound_panel_title = "⚙ Audio MIDI Setup" if is_mac else "⚙ Classic Sound Panel (mmsys.cpl)"
        ttk.Button(btn_row2, text="🔊 Open Sound Settings", command=open_windows_sound_settings).pack(side="left", padx=(0, 6))
        if not is_linux and not is_mac:
            ttk.Button(btn_row2, text="🎚 App Volume & Device Mixer", command=open_app_volume_settings).pack(side="left", padx=(0, 6))
        if not is_linux:
            ttk.Button(btn_row2, text=sound_panel_title, command=open_classic_sound_control_panel).pack(side="left")

        # One-Click Quick Routing Card
        rf = tk.LabelFrame(f, text=" One-Click Virtual Routing (Mic & Capture Source) ", bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=4)
        rf.pack(fill="x", **pad)
        rf.columnconfigure(1, weight=1)

        tk.Label(rf, text="PC Capture Source:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.audio_dev_combo = ttk.Combobox(rf, width=30, state="readonly", font=FONT_MONO_SM)
        self.audio_dev_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self.audio_dev_combo.bind("<<ComboboxSelected>>", self._on_capture_source_selected)
        ttk.Button(rf, text="↻ Refresh", command=self._refresh_all_devices).grid(row=0, column=2, padx=4, pady=4)

        quick_row = tk.Frame(rf, bg=BG)
        quick_row.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=4)
        ttk.Button(quick_row, text="🎤 Route Phone Mic → Virtual Cable (For Discord / Zoom / OBS)", command=self._auto_route_virtual_mic).pack(side="left", padx=(0, 6))
        ttk.Button(quick_row, text="🎧 Capture Virtual Speaker Only", command=self._auto_select_virtual_capture).pack(side="left")

        # Detected Virtual Endpoints Listbox
        df = tk.LabelFrame(f, text=" Detected Virtual Audio Endpoints ", bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=4)
        df.pack(fill="both", expand=True, **pad)
        self.virt_listbox = tk.Listbox(df, bg="#0A0A0A", fg="#00EEEE", font=FONT_MONO_SM, height=4, bd=1, relief="solid")
        self.virt_listbox.pack(fill="both", expand=True, padx=4, pady=2)

        # Firewall & System Options
        sf = tk.LabelFrame(f, text=" System & Firewall ", bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=4)
        sf.pack(fill="x", **pad)

        ttk.Button(sf, text="🔥 Add Firewall Rules (UDP 5004, 5005, 5006)", command=self._show_firewall_dialog).pack(side="left", padx=6, pady=4)
        self.tray_var = tk.BooleanVar(value=self.cfg.get("minimize_to_tray", True))
        tk.Checkbutton(
            sf, text="Minimize to system tray on close", variable=self.tray_var,
            bg=BG, fg=FG, selectcolor="#333333", font=FONT_MONO_SM
        ).pack(side="right", padx=6, pady=4)

        self._check_virtual_device_status()

    # ------------------------------------------------------------------
    # Virtual Device & Device Queries
    # ------------------------------------------------------------------

    def _check_virtual_device_status(self) -> None:
        devs = detect_virtual_devices()
        virt = [d for d in devs if d.get("is_virtual")]
        if hasattr(self, "virt_listbox"):
            self.virt_listbox.delete(0, tk.END)
            if virt:
                for d in virt:
                    roles = []
                    if d.get("is_output"):
                        roles.append("Playback/Sink")
                    if d.get("is_input"):
                        roles.append("Recording/Mic")
                    role_str = "/".join(roles) or "Audio"
                    self.virt_listbox.insert(tk.END, f" ✔ {d['name']}  [{role_str}]")
            else:
                self.virt_listbox.insert(tk.END, " (No virtual audio driver detected yet — click Install Virtual Audio Driver above)")

        if virt:
            names = ", ".join(d["name"] for d in virt[:2])
            self.virt_status_label.config(text=f"✔ VIRTUAL AUDIO ACTIVE ({len(virt)} endpoint(s)): {names}", fg=ACCENT)
        else:
            self.virt_status_label.config(text="⚠ No Virtual Audio Driver detected. Click [ Install Virtual Audio Driver ] below.", fg=WARN)

    def _auto_route_virtual_mic(self) -> None:
        self._refresh_pc_outputs()
        for i, d in enumerate(self._pc_output_devices):
            name_l = d.get("name", "").lower()
            if d.get("is_virtual") or any(k in name_l for k in ("cable input", "phonespeaker_mic_sink", "blackhole", "soundflower", "virtual", "vb-audio")):
                self.pc_output_combo.current(i)
                self._on_pc_output_selected()
                messagebox.showinfo(
                    "Phone Mic Routed",
                    f"Phone Mic (UDP 5006) is now routed to:\n\n  → {d['name']}\n\n"
                    "In Discord, Zoom, Teams, or OBS, select 'CABLE Output (VB-Audio Virtual Cable)', "
                    "'BlackHole 2ch' (macOS), or 'PhoneSpeaker_Wireless_Mic' (Linux) as your Microphone input!",
                )
                return
        messagebox.showwarning(
            "Virtual Audio Not Found",
            "No virtual audio cable (VB-CABLE / BlackHole / PhoneSpeaker_Mic_Sink) was found.\n\n"
            "Go to the 'Virtual Device' tab and click 'Install Virtual Audio Driver' first!",
        )

    def _auto_select_virtual_capture(self) -> None:
        self._refresh_audio_devices()
        for i, d in enumerate(self._audio_devices):
            name_l = d.get("name", "").lower()
            if any(k in name_l for k in ("cable", "blackhole", "soundflower", "multi-output", "virtual", "phonespeaker", "vb-audio")):
                self.audio_dev_combo.current(i + 1)
                self._on_capture_source_selected()
                messagebox.showinfo(
                    "Virtual Capture Selected",
                    f"PC Capture Source set to:\n\n  → {d['name']}",
                )
                return
        messagebox.showwarning(
            "Virtual Speaker Not Found",
            "No virtual speaker loopback was found. Using Default System Loopback instead.",
        )

    def _refresh_all_devices(self) -> None:
        self._refresh_adapters()
        self._refresh_audio_devices()
        self._refresh_pc_outputs()
        self._check_virtual_device_status()

    def _on_capture_source_selected(self, event=None) -> None:
        if not self._streaming:
            return
        dev_idx = None
        sel = self.audio_dev_combo.current()
        if sel >= 1 and (sel - 1) < len(self._audio_devices):
            dev_idx = self._audio_devices[sel - 1]["index"]
        f_ms = 5 if self.codec_var.get() == CODEC_PCM16 else int(self.frame_ms_var.get())
        if self._audio_capture:
            self._audio_capture.stop()
            self._audio_capture = None
        if self._queue_adapter is not None:
            self._audio_capture = AudioCapture(
                device_index=dev_idx,
                output_queue=self._queue_adapter,
                on_error=self._on_audio_error,
                frame_ms=f_ms,
            )
            self._audio_capture.start()

    def _refresh_adapters(self) -> None:
        adapters = list_real_adapters()
        entries = [f"{a['name']} [{a['ip']}]" + (" (down)" if not a["is_up"] else "") for a in adapters]
        self._adapter_data = adapters
        self.adapter_combo["values"] = entries if entries else ["(none found)"]
        if entries:
            saved = self.cfg.get("adapter_name", "")
            for i, a in enumerate(adapters):
                if a["name"] == saved:
                    self.adapter_combo.current(i)
                    return
            self.adapter_combo.current(0)

    def _refresh_audio_devices(self) -> None:
        prev_idx = self.audio_dev_combo.current()
        loopbacks = list_loopback_devices()
        inputs = list_input_devices()
        self._audio_devices = loopbacks + inputs
        default_lbl = "Auto (Default System Loopback)" if not sys.platform.startswith("win") else "Auto (Windows Default Loopback)"
        entries = [default_lbl] + [f"[Loopback] {d['name']}" for d in loopbacks] + [f"[Mic] {d['name']}" for d in inputs]
        self.audio_dev_combo["values"] = entries
        if 0 <= prev_idx < len(entries):
            self.audio_dev_combo.current(prev_idx)
        else:
            self.audio_dev_combo.current(0)

    def _refresh_pc_outputs(self) -> None:
        prev_idx = self.pc_output_combo.current()
        outputs = list_output_devices()
        self._pc_output_devices = outputs
        entries = [f"{d['name']}" + (" (Default)" if d.get("is_default") else "") for d in outputs]
        self.pc_output_combo["values"] = entries if entries else ["(none found)"]
        if 0 <= prev_idx < len(entries):
            self.pc_output_combo.current(prev_idx)
        elif entries:
            self.pc_output_combo.current(0)

    def _on_adapter_selected(self, event=None) -> None:
        sel = self.adapter_combo.current()
        if 0 <= sel < len(self._adapter_data):
            self.cfg["adapter_name"] = self._adapter_data[sel]["name"]
        self._refresh_party_qr()

    def _on_pc_output_selected(self, event=None) -> None:
        sel = self.pc_output_combo.current()
        pc_out_idx = None
        if 0 <= sel < len(self._pc_output_devices):
            pc_out_idx = self._pc_output_devices[sel]["index"]
        if self._phone_receiver:
            self._phone_receiver.set_output_device(pc_out_idx)

    def _sanitize_ip_and_port(self) -> Tuple[str, int]:
        raw_ip = self.ip_var.get().strip()
        for prefix in ("http://", "https://", "udp://"):
            if raw_ip.lower().startswith(prefix):
                raw_ip = raw_ip[len(prefix):].strip()
        raw_ip = raw_ip.split("/")[0].strip()

        port_str = self.ip_port_var.get().strip()
        if ":" in raw_ip:
            parts = raw_ip.rsplit(":", 1)
            raw_ip = parts[0].strip()
            if parts[1].strip().isdigit():
                port_str = parts[1].strip()
                self.ip_port_var.set(port_str)

        if raw_ip != self.ip_var.get():
            self.ip_var.set(raw_ip)

        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                raise ValueError()
        except ValueError:
            port = AUDIO_PORT
            self.ip_port_var.set(str(AUDIO_PORT))

        return raw_ip, port

    def _get_all_target_ips(self) -> List[str]:
        cur_ip = self.ip_var.get().strip()
        if not self.party_mode_var.get():
            return [cur_ip] if cur_ip else []
        ips = []
        if cur_ip and cur_ip not in ips:
            ips.append(cur_ip)
        for dev_ip in self._party_devices.keys():
            if dev_ip and dev_ip not in ips:
                ips.append(dev_ip)
        return ips

    def _register_party_device(self, ip: str, name: str = "Android Phone", port: int = AUDIO_PORT, force: bool = False) -> None:
        ip = (ip or "").strip()
        if not ip or ip in ("0.0.0.0", "127.0.0.1", "255.255.255.255"):
            return
        local_ips = [a.get("ip") for a in self._adapter_data if a.get("ip")]
        if ip in local_ips:
            return
        # If not actively hosting or extending party, do not auto-pollute party devices from normal mode beacons
        if not force and getattr(self, "_party_view_state", "CHOOSER") != "HOST" and not getattr(self, "party_extend_var", tk.BooleanVar(value=False)).get() and not getattr(self, "party_mode_var", tk.BooleanVar(value=False)).get():
            return
        self._party_devices[ip] = {
            "name": name or "Android Phone",
            "last_seen": time.monotonic(),
            "port": int(port),
        }
        if self._udp_sender:
            self._udp_sender.add_party_target(ip, int(port))
        self._refresh_party_ui()

    def _refresh_party_ui(self) -> None:
        now = time.monotonic()
        to_prune = []
        for dev_ip, dev_info in self._party_devices.items():
            if dev_ip != self._manual_ip_override and (now - dev_info["last_seen"]) > 12.0:
                to_prune.append(dev_ip)
        for dev_ip in to_prune:
            self._party_devices.pop(dev_ip, None)
            if self._udp_sender:
                self._udp_sender.remove_party_target(dev_ip)

        n = len(self._party_devices)
        if n == 0:
            cur = self.ip_var.get().strip()
            if cur:
                self.party_status_var.set(f"🎉 Party: 1 Device ({cur})")
            else:
                self.party_status_var.set("🎉 Party Mode: Waiting for devices to join...")
        else:
            dev_str = ", ".join(f"{d['name']} ({ip})" for ip, d in self._party_devices.items())
            self.party_status_var.set(f"🎉 {n} Joined: {dev_str}")
            if self._party_view_state == "HOST" and self.party_mode_var.get() and not self._manual_ip_override:
                first_ip = next(iter(self._party_devices))
                first_name = self._party_devices[first_ip]["name"]
                self.conn_device_name_var.set(f"{first_name} (+{n-1} party)" if n > 1 else first_name)
                self.conn_device_ip_var.set(f"{first_ip}  •  PARTY ACTIVE")

        if hasattr(self, "party_listbox"):
            self.party_listbox.delete(0, tk.END)
            for idx, (dev_ip, dev_info) in enumerate(self._party_devices.items(), start=1):
                age = max(0.0, now - dev_info["last_seen"])
                state_tag = "ONLINE" if age < 5.0 else f"IDLE ({int(age)}s ago)"
                self.party_listbox.insert(
                    tk.END,
                    f" #{idx}  {dev_info['name']:<20} •  {dev_ip}:{dev_info['port']}  •  {state_tag}"
                )

        if hasattr(self, "party_extend_peers_label"):
            ext_peers = [f"{d['name']} ({ip})" for ip, d in self._party_devices.items()]
            if ext_peers:
                self.party_extend_peers_label.config(
                    text=f"Extended Peers ({len(ext_peers)}): {', '.join(ext_peers)} • Re-broadcasting",
                    fg=ACCENT
                )
            else:
                self.party_extend_peers_label.config(
                    text="Extended Party Peers: Waiting for friends to connect/join...",
                    fg=FG_MUTED
                )

    def _on_add_party_device_manual(self, event=None) -> None:
        raw = self.party_add_ip_var.get().strip()
        for prefix in ("http://", "https://", "udp://"):
            if raw.lower().startswith(prefix):
                raw = raw[len(prefix):].strip()
        raw = raw.split("/")[0].strip()
        port = AUDIO_PORT
        if ":" in raw:
            parts = raw.rsplit(":", 1)
            raw = parts[0].strip()
            if parts[1].strip().isdigit():
                port = int(parts[1].strip())
        if not raw:
            return
        name = self.party_add_name_var.get().strip() or f"Phone ({raw})"
        self._manual_ip_override = raw
        if not self.ip_var.get().strip():
            self.ip_var.set(raw)
        self._register_party_device(raw, name, port, force=True)
        self.party_add_ip_var.set("")
        self.party_add_name_var.set("")
        send_control_message(raw, self._get_current_config(), CONTROL_PORT)

    def _remove_selected_party_device(self) -> None:
        if not hasattr(self, "party_listbox"):
            return
        sel = self.party_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        ips = list(self._party_devices.keys())
        if 0 <= idx < len(ips):
            ip_to_remove = ips[idx]
            self._party_devices.pop(ip_to_remove, None)
            if self._udp_sender:
                self._udp_sender.remove_party_target(ip_to_remove)
            self._refresh_party_ui()

    def _on_party_mode_toggled(self) -> None:
        enabled = self.party_mode_var.get()
        if self._udp_sender:
            self._udp_sender.set_party_mode(enabled)
        self._refresh_party_ui()
        self._on_local_setting_changed()

    def _clear_party_devices(self) -> None:
        self._party_devices.clear()
        if self._udp_sender:
            self._udp_sender.clear_party_targets()
        self._refresh_party_ui()

    def _enter_party_host_mode(self) -> None:
        self._party_view_state = "HOST"
        self.party_mode_var.set(True)
        self.party_role_var.set("HOST")
        if self._control_listener:
            self._control_listener.set_party_mode(True)
        if hasattr(self, "party_chooser_frame"):
            self.party_chooser_frame.pack_forget()
        if hasattr(self, "party_join_frame"):
            self.party_join_frame.pack_forget()
        if hasattr(self, "party_host_frame"):
            self.party_host_frame.pack(fill="both", expand=True)
        self._refresh_party_qr()

    def _enter_party_join_mode(self) -> None:
        self._party_view_state = "JOIN"
        self.party_mode_var.set(False)
        self.party_role_var.set("JOIN")
        if self._control_listener:
            self._control_listener.set_party_mode(False)
        if hasattr(self, "party_chooser_frame"):
            self.party_chooser_frame.pack_forget()
        if hasattr(self, "party_host_frame"):
            self.party_host_frame.pack_forget()
        if hasattr(self, "party_join_frame"):
            self.party_join_frame.pack(fill="both", expand=True)
        self._refresh_discovered_hosts_ui()
        if not self._party_joined_active and hasattr(self, "party_extend_card"):
            self.party_extend_card.pack_forget()

    def _back_to_party_chooser(self) -> None:
        if self._party_streaming:
            self._stop_stream(from_party=True)
        if self._party_join_receiver and self._party_join_receiver.is_running:
            self._stop_party_join()
        self._party_view_state = "CHOOSER"
        self.party_mode_var.set(False)
        self._party_joined_active = False
        self.party_extend_var.set(False)
        if self._control_listener:
            self._control_listener.set_party_mode(False)
        if hasattr(self, "party_host_frame"):
            self.party_host_frame.pack_forget()
        if hasattr(self, "party_join_frame"):
            self.party_join_frame.pack_forget()
        if hasattr(self, "party_extend_card"):
            self.party_extend_card.pack_forget()
        if hasattr(self, "party_extend_details_frame"):
            self.party_extend_details_frame.pack_forget()
        if hasattr(self, "party_chooser_frame"):
            self.party_chooser_frame.pack(fill="both", expand=True)

    def _toggle_party_extend_pc(self) -> None:
        active = not self.party_extend_var.get()
        self.party_extend_var.set(active)
        if self._control_listener:
            self._control_listener.set_party_mode(active)
        if active:
            if hasattr(self, "party_extend_details_frame"):
                self.party_extend_details_frame.pack(fill="x", expand=True)
            if hasattr(self, "btn_party_extend"):
                self.btn_party_extend.config(
                    text="[ ■ STOP EXTENDING PARTY ]",
                    bg="#330000", fg=ERROR_COLOR,
                    activebackground="#550000", activeforeground=ERROR_COLOR
                )
            self._refresh_party_extend_qr()
        else:
            if hasattr(self, "party_extend_details_frame"):
                self.party_extend_details_frame.pack_forget()
            if hasattr(self, "btn_party_extend"):
                self.btn_party_extend.config(
                    text="[ 📡 EXTEND PARTY (GENERATE HOTSPOT QR & IP) ]",
                    bg="#002B00", fg="#00EEEE",
                    activebackground="#004400", activeforeground="#00EEEE"
                )
        self._refresh_party_ui()

    def _on_party_role_changed(self) -> None:
        role = self.party_role_var.get()
        if role == "HOST":
            self._enter_party_host_mode()
        else:
            self._enter_party_join_mode()

    def _on_party_extend_toggled(self) -> None:
        self._toggle_party_extend_pc()

    def _refresh_party_extend_qr(self) -> None:
        bind_ip = "127.0.0.1"
        try:
            adapter_sel = self.adapter_combo.current()
            if 0 <= adapter_sel < len(self._adapter_data):
                bind_ip = self._adapter_data[adapter_sel].get("ip", "")
            if not bind_ip or bind_ip.startswith("127."):
                adapters = list_real_adapters()
                if adapters:
                    bind_ip = adapters[0].get("ip", "127.0.0.1")
        except Exception:
            pass

        if hasattr(self, "party_extend_ip_label"):
            self.party_extend_ip_label.config(text=f"📡 EXTENDED HOST IP: {bind_ip} : {AUDIO_PORT}")

        if not hasattr(self, "party_extend_qr_label") or not _QR_AVAILABLE or Image is None or ImageTk is None:
            return

        uri = f"phonespeaker://join?host={bind_ip}&port={AUDIO_PORT}&name=PC%20Host%20(Extended)"
        try:
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=3,
                border=2,
            )
            qr.add_data(uri)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#000000", back_color="#FFFFFF").convert("RGB")
            img = img.resize((120, 120), Image.Resampling.NEAREST)
            self._party_extend_qr_photo = ImageTk.PhotoImage(img)
            self.party_extend_qr_label.config(image=self._party_extend_qr_photo, width=120, height=120)
            self.party_extend_qr_label.bind("<Button-1>", lambda e: self._copy_party_join_link(uri))
            self.party_extend_qr_label.config(cursor="hand2")
        except Exception as exc:
            logger.debug("Failed to render extend QR code: %s", exc)

    def _relay_party_packet(self, raw_data: bytes, sender_addr: Tuple[str, int]) -> None:
        if not getattr(self, "party_extend_var", None) or not self.party_extend_var.get():
            return
        if len(raw_data) < 12:
            return
        sender_ip = sender_addr[0]
        raw_codec = raw_data[8]
        hop_count = (raw_codec >> 4) & 0x07
        if hop_count >= 4:
            return
        new_codec = (raw_codec & 0x8F) | (((hop_count + 1) & 0x07) << 4)
        fwd_pkt = bytearray(raw_data)
        fwd_pkt[8] = new_codec
        fwd_bytes = bytes(fwd_pkt)
        peers = [ip for ip in self._party_devices.keys() if ip != sender_ip]
        if not peers:
            return
        if not hasattr(self, "_party_relay_sock") or self._party_relay_sock is None:
            try:
                self._party_relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            except Exception:
                return
        for p_ip in peers:
            try:
                self._party_relay_sock.sendto(fwd_bytes, (p_ip, AUDIO_PORT))
            except Exception:
                pass

    def _refresh_party_qr(self) -> None:
        bind_ip = "127.0.0.1"
        try:
            adapter_sel = self.adapter_combo.current()
            if 0 <= adapter_sel < len(self._adapter_data):
                bind_ip = self._adapter_data[adapter_sel].get("ip", "")
            if not bind_ip or bind_ip.startswith("127."):
                adapters = list_real_adapters()
                if adapters:
                    bind_ip = adapters[0].get("ip", "127.0.0.1")
        except Exception:
            pass

        if hasattr(self, "party_host_ip_label"):
            self.party_host_ip_label.config(text=f"HOST IP ADDRESS: {bind_ip} : {AUDIO_PORT}")

        if not hasattr(self, "party_qr_label") or not _QR_AVAILABLE or Image is None or ImageTk is None:
            return

        uri = f"phonespeaker://join?host={bind_ip}&port={AUDIO_PORT}&name=PC%20Host"
        try:
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=3,
                border=2,
            )
            qr.add_data(uri)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#000000", back_color="#FFFFFF").convert("RGB")
            img = img.resize((120, 120), Image.Resampling.NEAREST)
            self._party_qr_photo = ImageTk.PhotoImage(img)
            self.party_qr_label.config(image=self._party_qr_photo, width=120, height=120)
            self.party_qr_label.bind("<Button-1>", lambda e: self._copy_party_join_link(uri))
            self.party_qr_label.config(cursor="hand2")
        except Exception as exc:
            logger.debug("Failed to render QR code: %s", exc)

    def _copy_party_join_link(self, uri: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(uri)
            messagebox.showinfo(
                "Party Mode",
                f"Copied Party Join link to clipboard:\n\n{uri}\n\nShare this link or scan the QR code to join this party!",
            )
        except Exception:
            pass

    def _parse_join_uri(self, raw: str) -> Tuple[str, int]:
        raw = (raw or "").strip()
        if not raw:
            return ("", AUDIO_PORT)
        if "://" in raw:
            try:
                import urllib.parse
                parsed = urllib.parse.urlparse(raw)
                qs = urllib.parse.parse_qs(parsed.query)
                host = qs.get("host", [""])[0] or parsed.netloc.split(":")[0]
                port = int(qs.get("port", [str(AUDIO_PORT)])[0])
                if host:
                    return (host, port)
            except Exception:
                pass
        for p in ("http://", "https://", "udp://", "phonespeaker://"):
            if raw.lower().startswith(p):
                raw = raw[len(p):].strip()
        raw = raw.split("/")[0].strip()
        port = AUDIO_PORT
        if ":" in raw:
            parts = raw.split(":")
            raw = parts[0].strip()
            try:
                port = int(parts[1].strip())
            except ValueError:
                port = AUDIO_PORT
        return (raw, port)

    def _on_paste_party_join_link(self) -> None:
        try:
            raw = self.root.clipboard_get().strip()
        except Exception:
            raw = ""
        if not raw:
            messagebox.showinfo("Party Mode", "Clipboard is empty. Copy a party host QR link or IP address first.")
            return
        ip, port = self._parse_join_uri(raw)
        if ip:
            self.party_join_host_ip_var.set(ip)
            self.party_join_port_var.set(str(port))
            messagebox.showinfo("Party Mode", f"Successfully imported Host: {ip}:{port}")
        else:
            messagebox.showwarning("Party Mode", f"Could not recognize a valid host IP or QR link in clipboard:\n{raw[:100]}")

    def _refresh_discovered_hosts_ui(self) -> None:
        if not hasattr(self, "party_join_discovered_listbox"):
            return
        now = time.monotonic()
        to_del = [ip for ip, data in self._discovered_party_hosts.items() if (now - data.get("last_seen", 0)) > 15.0]
        for ip in to_del:
            self._discovered_party_hosts.pop(ip, None)

        cur_sel = self.party_join_discovered_listbox.curselection()
        selected_ip = None
        if cur_sel and cur_sel[0] < self.party_join_discovered_listbox.size():
            item_text = self.party_join_discovered_listbox.get(cur_sel[0])
            for ip in self._discovered_party_hosts.keys():
                if ip in item_text:
                    selected_ip = ip
                    break

        self.party_join_discovered_listbox.delete(0, tk.END)
        if not self._discovered_party_hosts:
            self.party_join_discovered_listbox.insert(tk.END, "  (No party hosts detected yet — waiting for beacons...)")
        else:
            idx_to_reselect = -1
            for i, (ip, data) in enumerate(self._discovered_party_hosts.items()):
                name = data.get("name", "Host")
                port = data.get("port", AUDIO_PORT)
                self.party_join_discovered_listbox.insert(tk.END, f"  📡 {name}  •  {ip}:{port}")
                if ip == selected_ip or ip == self.party_join_host_ip_var.get().strip():
                    idx_to_reselect = i
            if idx_to_reselect >= 0:
                self.party_join_discovered_listbox.selection_set(idx_to_reselect)

    def _on_select_discovered_host(self, event=None) -> None:
        if not hasattr(self, "party_join_discovered_listbox"):
            return
        sel = self.party_join_discovered_listbox.curselection()
        if not sel:
            return
        item = self.party_join_discovered_listbox.get(sel[0])
        for ip, data in self._discovered_party_hosts.items():
            if ip in item:
                self.party_join_host_ip_var.set(ip)
                self.party_join_port_var.set(str(data.get("port", AUDIO_PORT)))
                break

    def _toggle_party_join(self) -> None:
        if self._party_join_receiver and self._party_join_receiver.is_running:
            self._stop_party_join()
        else:
            self._start_party_join()

    def _start_party_join(self) -> None:
        if self._streaming:
            self._stop_stream(from_party=False)
            self._stop_stream(from_party=True)

        host_ip = self.party_join_host_ip_var.get().strip()
        if not host_ip:
            self._on_select_discovered_host()
            host_ip = self.party_join_host_ip_var.get().strip()

        if not host_ip:
            messagebox.showwarning("Party Mode", "Please enter or select a Party Host IP address first.")
            return

        try:
            port = int(self.party_join_port_var.get().strip())
        except ValueError:
            port = AUDIO_PORT
            self.party_join_port_var.set(str(AUDIO_PORT))

        pc_out_idx = None
        out_sel = self.pc_output_combo.current()
        if 0 <= out_sel < len(self._pc_output_devices):
            pc_out_idx = self._pc_output_devices[out_sel]["index"]

        try:
            self._party_join_receiver = PhoneAudioReceiver(
                port=port,
                output_device_index=pc_out_idx,
                on_stats=self._on_party_join_stats,
                on_packet=self._relay_party_packet,
            )
            self._party_join_receiver.start()
        except Exception as exc:
            messagebox.showerror("Party Mode", f"Failed to start party audio receiver on port {port}:\n{exc}")
            self._party_join_receiver = None
            return

        try:
            send_control_message(
                host_ip,
                {
                    "type": "BEACON",
                    "app": "PhoneSpeaker",
                    "version": 2,
                    "party_join": True,
                    "is_party_host": False,
                    "is_extended_host": bool(self.party_extend_var.get()),
                    "device": socket.gethostname(),
                    "audio_port": port,
                },
                CONTROL_PORT,
            )
        except Exception:
            pass

        self.party_join_btn.config(
            text="🛑  LEAVE PARTY STREAM", bg="#330000", fg=ERROR_COLOR,
            activebackground="#550000", activeforeground=ERROR_COLOR
        )
        self.party_join_status_var.set(f"🎧 Connected to Host {host_ip}:{port} • Receiving party sound...")
        self._party_joined_active = True
        if hasattr(self, "party_extend_card"):
            self.party_extend_card.pack(fill="x", padx=8, pady=6)
        logger.info("Joined party at %s:%d", host_ip, port)

    def _stop_party_join(self) -> None:
        if self._party_join_receiver:
            self._party_join_receiver.stop()
            self._party_join_receiver = None
        if hasattr(self, "_party_relay_sock") and self._party_relay_sock:
            try:
                self._party_relay_sock.close()
            except Exception:
                pass
            self._party_relay_sock = None
        if hasattr(self, "party_join_btn"):
            self.party_join_btn.config(
                text="🎧  JOIN PARTY STREAM (LISTEN TO HOST)", bg="#002B00", fg=ACCENT,
                activebackground="#004400", activeforeground=ACCENT
            )
        self._party_joined_active = False
        self.party_extend_var.set(False)
        if hasattr(self, "party_extend_card"):
            self.party_extend_card.pack_forget()
        if hasattr(self, "party_extend_details_frame"):
            self.party_extend_details_frame.pack_forget()
        if hasattr(self, "btn_party_extend"):
            self.btn_party_extend.config(
                text="[ 📡 EXTEND PARTY (GENERATE HOTSPOT QR & IP) ]",
                bg="#002B00", fg="#00EEEE",
                activebackground="#004400", activeforeground="#00EEEE"
            )
        self.party_join_status_var.set("🎧 Joiner Standby: Select a discovered host or enter Host IP")
        logger.info("Left party stream")

    def _on_party_join_stats(self, pps: int) -> None:
        self._party_join_rx_pps = pps
        if self._party_join_receiver and self._party_join_receiver.is_running:
            host_ip = self.party_join_host_ip_var.get().strip()
            port = self.party_join_port_var.get().strip()
            ext_suffix = ""
            if self.party_extend_var.get():
                n_ext = len(self._party_devices)
                ext_suffix = f" • 📡 Extending to {n_ext} peer(s)"
            self.party_join_status_var.set(
                f"🎧 Playing Host {host_ip}:{port} • {pps} pkts/s{ext_suffix}"
            )

    def _on_manual_ip_applied(self, event=None) -> None:
        new_ip, new_port = self._sanitize_ip_and_port()
        if not new_ip:
            self._manual_ip_override = None
            self.conn_device_ip_var.set("Auto-Discovery active (waiting for phone beacon...)")
            return
        self._manual_ip_override = new_ip
        self.phone_ip_var.set(f"Phone: {new_ip}")
        self.conn_device_ip_var.set(f"{new_ip}  •  TARGET SET")
        self._register_party_device(new_ip, "Manual Device", new_port)
        if self._streaming and self._udp_sender:
            self._udp_sender.update_target(new_ip, new_port)
            self._update_combined_status()
        self._save_config()
        send_control_message(new_ip, self._get_current_config(), CONTROL_PORT)

    # ------------------------------------------------------------------
    # Settings & 2-Way Sync Engine
    # ------------------------------------------------------------------

    def _compute_expected_delay_ms(self) -> Tuple[int, str]:
        codec = self.codec_var.get()
        try:
            bitrate = int(self.bitrate_var.get() or "128")
        except ValueError:
            bitrate = 128
        frame_ms = 5 if codec == CODEC_PCM16 else int(self.frame_ms_var.get() or "5")
        try:
            buf_ms = int(self.custom_buffer_var.get() or 30)
        except ValueError:
            buf_ms = 30
        capture_ms = 10  # WASAPI shared loopback period
        hw_ms = 10       # Android Oboe low-latency burst
        if codec == CODEC_PCM16:
            codec_net_ms = 3
            quality_note = "Lossless 1536 kbps"
        else:
            if bitrate <= 32:
                codec_net_ms = 9
                quality_note = f"Opus {bitrate}k (Voice/Low BW)"
            elif bitrate <= 96:
                codec_net_ms = 6
                quality_note = f"Opus {bitrate}k (Balanced)"
            elif bitrate <= 192:
                codec_net_ms = 5
                quality_note = f"Opus {bitrate}k (High Quality)"
            else:
                codec_net_ms = 6
                quality_note = f"Opus {bitrate}k (Studio Max)"
        total_ms = capture_ms + frame_ms + codec_net_ms + buf_ms + hw_ms
        return total_ms, f"Expected Delay: ~{total_ms} ms ({quality_note}, {frame_ms}ms frame + {buf_ms}ms buf)"

    def _update_expected_delay_label(self) -> None:
        _, text = self._compute_expected_delay_ms()
        self.expected_delay_var.set(text)

    def _get_current_config(self) -> Dict[str, Any]:
        sync_id = int(time.time() * 1000) & 0x7FFFFFFF
        self._last_sync_id = sync_id
        return {
            "type": "CONFIG_SYNC",
            "rev": self._config_rev,
            "sync_id": sync_id,
            "direction": self.direction_var.get(),
            "pc_source": "SYSTEM_AUDIO",
            "phone_source": "MIC",
            "audio_profile": self.audio_profile_var.get(),
            "codec": self.codec_var.get(),
            "opus_bitrate_kbps": int(self.bitrate_var.get()),
            "frame_ms": int(self.frame_ms_var.get()),
            "buffer_mode": self.buffer_mode_var.get(),
            "custom_buffer_ms": self.custom_buffer_var.get(),
            "auto_buffer": (self.buffer_mode_var.get() == "AUTO"),
            "volume_gain": self.volume_gain_var.get(),
            "muted": self.muted_var.get(),
            "pc_name": socket.gethostname(),
            "audio_output_engine": self._phone_audio_engine,
            "enable_audio_effects": self._phone_audio_fx,
            "exclusive_audio_mode": self._phone_exclusive_mode,
            "stop_on_headset_disconnect": self._phone_headset_disc,
            "party_mode": self.party_mode_var.get(),
            "party_ips": self._get_all_target_ips(),
        }

    def _on_local_setting_changed(self) -> None:
        if self._suppress_sync:
            return
        self._update_expected_delay_label()
        self._config_rev += 1
        self.sync_rev_label.config(text=f"SYNC REV: {self._config_rev}")
        cfg = self._get_current_config()
        self._save_config()

        if self._streaming:
            c = self.codec_var.get()
            f_ms = int(self.frame_ms_var.get())
            if c == CODEC_PCM16 and f_ms != 5:
                f_ms = 5
                self.frame_ms_var.set("5")
            br = int(self.bitrate_var.get()) * 1000
            gain = self.volume_gain_var.get()
            muted = self.muted_var.get()

            if self._audio_capture:
                self._audio_capture.set_frame_ms(f_ms)

            if self._udp_sender:
                self._udp_sender.set_codec_and_bitrate(c, br, f_ms)
                self._udp_sender.set_routing_and_gain(True, gain, muted)

        # Transmit to all joined phones in party
        for tip in self._get_all_target_ips():
            send_control_message(tip, cfg, CONTROL_PORT)

    def _on_audio_profile_selected(self) -> None:
        prof = self.audio_profile_var.get()
        if prof != "CUSTOM":
            self._apply_profile_presets(prof)
        self._on_local_setting_changed()

    def _apply_profile_presets(self, profile: str) -> None:
        self._suppress_sync = True
        try:
            if profile == "QUALITY":
                self.codec_var.set(CODEC_OPUS)
                self.bitrate_var.set("256")
                self.frame_ms_var.set("10")
                self.buffer_mode_var.set("HIGH")
                self.custom_buffer_var.set(80.0)
            elif profile == "GAMING":
                self.codec_var.set(CODEC_OPUS)
                self.bitrate_var.set("128")
                self.frame_ms_var.set("5")
                self.buffer_mode_var.set("LOW")
                self.custom_buffer_var.set(20.0)
            elif profile == "BALANCED":
                self.codec_var.set(CODEC_OPUS)
                self.bitrate_var.set("192")
                self.frame_ms_var.set("5")
                self.buffer_mode_var.set("AUTO")
                self.custom_buffer_var.set(30.0)
            if hasattr(self, "buffer_val_label"):
                self.buffer_val_label.config(text=f"{self.custom_buffer_var.get():.1f} ms")
            self._update_codec_ui_state()
        finally:
            self._suppress_sync = False

    def _update_codec_ui_state(self) -> None:
        is_opus = (self.codec_var.get() == CODEC_OPUS)
        state = "readonly" if is_opus else "disabled"
        if hasattr(self, "bitrate_combo_tab1"):
            self.bitrate_combo_tab1.config(state=state)
        if hasattr(self, "bitrate_combo_tab2"):
            self.bitrate_combo_tab2.config(state=state)
        if hasattr(self, "frame_combo_tab2"):
            self.frame_combo_tab2.config(state=state)
        if not is_opus and self.frame_ms_var.get() != "5":
            self.frame_ms_var.set("5")

    def _on_codec_changed(self) -> None:
        if not self._suppress_sync:
            self.audio_profile_var.set("CUSTOM")
        self._update_codec_ui_state()
        self._on_local_setting_changed()

    def _on_bitrate_changed(self) -> None:
        if not self._suppress_sync:
            self.audio_profile_var.set("CUSTOM")
        self._on_local_setting_changed()

    def _on_frame_ms_changed(self) -> None:
        if not self._suppress_sync:
            self.audio_profile_var.set("CUSTOM")
        self._on_local_setting_changed()

    def _on_role_change(self) -> None:
        direction = self.direction_var.get()
        phone_ip = self.ip_var.get().strip()

        if direction == "PHONE_TO_PC":
            if self._streaming:
                self._stop_streaming()
            if self._phone_receiver:
                self._phone_receiver.set_muted(False, manual=False)
            self.mic_state_var.set("Phone Mic: LISTENING (:5006)")
            self.mic_btn.config(
                text="■  STOP MIC RECEIVER (PHONE → PC :5006 ACTIVE)",
                bg="#003344", fg="#00FF00"
            )
            if phone_ip:
                send_control_message(phone_ip, {"type": "MIC_CONTROL", "active": True}, CONTROL_PORT)
        elif direction == "FULL_DUPLEX":
            if self._phone_receiver:
                self._phone_receiver.set_muted(False, manual=False)
            self.mic_state_var.set("Phone Mic: LISTENING (:5006)")
            self.mic_btn.config(
                text="■  STOP MIC RECEIVER (PHONE → PC :5006 ACTIVE)",
                bg="#003344", fg="#00FF00"
            )
            if phone_ip:
                send_control_message(phone_ip, {"type": "MIC_CONTROL", "active": True}, CONTROL_PORT)

        self._on_local_setting_changed()

    def _on_buffer_mode_changed(self) -> None:
        if not self._suppress_sync:
            self.audio_profile_var.set("CUSTOM")
        mode = self.buffer_mode_var.get()
        if mode == "LOW":
            self.custom_buffer_var.set(20.0)
        elif mode == "MEDIUM":
            self.custom_buffer_var.set(40.0)
        elif mode == "HIGH":
            self.custom_buffer_var.set(80.0)
        self.buffer_val_label.config(text=f"{self.custom_buffer_var.get():.1f} ms")
        self._on_local_setting_changed()

    def _on_buffer_slider_changed(self, val) -> None:
        if self._suppress_sync:
            return
        self.buffer_val_label.config(text=f"{float(val):.1f} ms")
        self.buffer_mode_var.set("CUSTOM")
        self.audio_profile_var.set("CUSTOM")
        self._on_local_setting_changed()

    def _on_gain_slider_changed(self, val) -> None:
        if self._suppress_sync:
            return
        gain = round(float(val), 2)
        self._suppress_sync = True
        try:
            self.volume_gain_var.set(gain)
        finally:
            self._suppress_sync = False

        pct_text = f"{int(round(gain * 100))}%"
        self.gain_val_label.config(text=pct_text)
        if hasattr(self, "conn_vol_pct_label"):
            self.conn_vol_pct_label.config(text=pct_text)

        muted = self.muted_var.get()
        if self._udp_sender:
            self._udp_sender.set_routing_and_gain(True, gain, muted)

        if getattr(self, "sync_os_vol_var", None) and self.sync_os_vol_var.get():
            os_target = min(1.0, max(0.0, gain))
            set_system_volume_and_mute(os_target, muted)
            self._last_os_vol = os_target

        if hasattr(self, "_gain_sync_after_id") and self._gain_sync_after_id:
            try:
                self.root.after_cancel(self._gain_sync_after_id)
            except Exception:
                pass
        self._gain_sync_after_id = self.root.after(60, self._on_local_setting_changed)

    def _toggle_mute(self) -> None:
        m = not self.muted_var.get()
        self.muted_var.set(m)
        self.mute_btn.config(bg="#990000" if m else "#333333", text="MUTED" if m else "MUTE")
        if hasattr(self, "conn_mute_icon_btn"):
            self.conn_mute_icon_btn.config(text="🔇" if m else "🔊")
        if getattr(self, "sync_os_vol_var", None) and self.sync_os_vol_var.get():
            set_system_volume_and_mute(self.volume_gain_var.get(), m)
            self._last_os_mute = m
        self._on_local_setting_changed()

    def _on_config_sync_received(self, msg: dict) -> None:
        self._ui_queue.put(("config_sync", msg))

    def _apply_incoming_config(self, msg: dict) -> None:
        incoming_rev = msg.get("rev", 0)
        incoming_sync_id = msg.get("sync_id")

        if incoming_sync_id is not None and incoming_sync_id == self._last_sync_id:
            return

        if incoming_sync_id is not None:
            self._last_sync_id = incoming_sync_id
        elif incoming_rev <= self._config_rev:
            return

        self._suppress_sync = True
        try:
            self._config_rev = max(self._config_rev, incoming_rev) + 1
            self.sync_rev_label.config(text=f"SYNC REV: {self._config_rev}")

            dev_name = msg.get("device", "")
            sender_ip = msg.get("_sender_ip", "")
            if dev_name and dev_name != "Android":
                self._connected_phone_name = dev_name
                self.conn_device_name_var.set(dev_name)
            if sender_ip:
                if not self.ip_var.get().strip():
                    self.ip_var.set(sender_ip)
                self.conn_device_ip_var.set(f"{sender_ip}  •  CONNECTED")

            if "codec" in msg:
                self.codec_var.set(int(msg["codec"]))
            if "opus_bitrate_kbps" in msg:
                self.bitrate_var.set(str(msg["opus_bitrate_kbps"]))
            if "audio_profile" in msg:
                prof = msg["audio_profile"]
                self.audio_profile_var.set(prof)
                if "frame_ms" not in msg:
                    if prof == "QUALITY":
                        self.frame_ms_var.set("10")
                    elif prof in ("GAMING", "BALANCED"):
                        self.frame_ms_var.set("5")
            if "frame_ms" in msg:
                self.frame_ms_var.set(str(msg["frame_ms"]))
            if "buffer_mode" in msg:
                self.buffer_mode_var.set(msg["buffer_mode"])
            if "custom_buffer_ms" in msg:
                c = float(msg["custom_buffer_ms"])
                self.custom_buffer_var.set(c)
                self.buffer_val_label.config(text=f"{c:.1f} ms")
            if "volume_gain" in msg:
                g = float(msg["volume_gain"])
                self.volume_gain_var.set(g)
                self.gain_val_label.config(text=f"{int(round(g * 100))}%")
                if hasattr(self, "conn_vol_pct_label"):
                    self.conn_vol_pct_label.config(text=f"{int(round(g * 100))}%")
            if "muted" in msg:
                m = bool(msg["muted"])
                self.muted_var.set(m)
                self.mute_btn.config(bg="#990000" if m else "#333333", text="MUTED" if m else "MUTE")
                if hasattr(self, "conn_mute_icon_btn"):
                    self.conn_mute_icon_btn.config(text="🔇" if m else "🔊")
            if "direction" in msg:
                self.direction_var.set(msg["direction"])
            if "audio_output_engine" in msg:
                self._phone_audio_engine = str(msg["audio_output_engine"])
            if "enable_audio_effects" in msg:
                self._phone_audio_fx = bool(msg["enable_audio_effects"])
            if "exclusive_audio_mode" in msg:
                self._phone_exclusive_mode = str(msg["exclusive_audio_mode"])
            if "stop_on_headset_disconnect" in msg:
                self._phone_headset_disc = bool(msg["stop_on_headset_disconnect"])

            self._update_codec_ui_state()
            self._update_expected_delay_label()

            if self._streaming:
                c = self.codec_var.get()
                f_ms = int(self.frame_ms_var.get())
                if c == CODEC_PCM16 and f_ms != 5:
                    f_ms = 5
                    self.frame_ms_var.set("5")
                br = int(self.bitrate_var.get()) * 1000
                gain = self.volume_gain_var.get()
                muted = self.muted_var.get()

                if self._audio_capture:
                    self._audio_capture.set_frame_ms(f_ms)

                if self._udp_sender:
                    self._udp_sender.set_codec_and_bitrate(c, br, f_ms)
                    self._udp_sender.set_routing_and_gain(True, gain, muted)

            self._save_config()
            logger.info("Applied incoming CONFIG_SYNC rev %d (sync_id=%s) from phone", incoming_rev, incoming_sync_id)
        finally:
            self._suppress_sync = False

    # ------------------------------------------------------------------
    # Streaming Start / Stop
    # ------------------------------------------------------------------

    def _toggle_streaming(self) -> None:
        """Start/Stop Normal Stream (PC -> 1 Phone, 1-to-1 point-to-point)."""
        if self._normal_streaming:
            self._stop_stream(from_party=False)
        else:
            self._start_streaming(as_party=False)

    def _toggle_party_broadcast(self) -> None:
        """Start/Stop Party Broadcast (1 Host -> Multiple Joiners)."""
        if self._party_streaming:
            self._stop_stream(from_party=True)
        else:
            self._start_streaming(as_party=True)

    def _start_streaming(self, as_party: bool = False) -> None:
        if self._party_join_receiver and self._party_join_receiver.is_running:
            self._stop_party_join()

        self._refresh_audio_devices()
        self._refresh_pc_outputs()

        direction = self.direction_var.get()
        target_ip, target_port = self._sanitize_ip_and_port()
        if as_party:
            if not target_ip and self._party_devices:
                target_ip = next(iter(self._party_devices))
            if not target_ip:
                target_ip = "127.0.0.1"
        else:
            if direction in ("PC_TO_PHONE", "FULL_DUPLEX") and not target_ip:
                messagebox.showwarning("PhoneSpeaker", "Please enter the phone's IP address or select a discovered phone.")
                return

        codec = self.codec_var.get()
        bitrate = int(self.bitrate_var.get()) * 1000
        frame_ms = int(self.frame_ms_var.get())

        self._save_config()

        # Selected local bind IP
        bind_ip = ""
        adapter_sel = self.adapter_combo.current()
        if 0 <= adapter_sel < len(self._adapter_data):
            bind_ip = self._adapter_data[adapter_sel].get("ip", "")

        # If already streaming (the other mode was active), reconfigure UDPSender mode
        if self._udp_sender is not None and self._audio_capture is not None:
            if as_party:
                self._udp_sender.set_party_mode(True)
                for dev_ip, dev_info in self._party_devices.items():
                    self._udp_sender.add_party_target(dev_ip, dev_info.get("port", AUDIO_PORT))
            else:
                self._udp_sender.update_target(target_ip, target_port)
                if not self._party_streaming:
                    self._udp_sender.set_party_mode(False)
        else:
            # 1. Start PC -> Phone Stream if in PC_TO_PHONE or FULL_DUPLEX
            if direction in ("PC_TO_PHONE", "FULL_DUPLEX"):
                if codec == CODEC_OPUS:
                    self._opus_encoder = OpusEncoder(bitrate=bitrate, frame_ms=frame_ms)
                    if not self._opus_encoder.available:
                        codec = CODEC_PCM16
                        self._opus_encoder = None
                else:
                    self._opus_encoder = None

                if codec == CODEC_PCM16 and frame_ms != 5:
                    frame_ms = 5
                    self.frame_ms_var.set("5")

                self._audio_queue = AudioQueue()

                self._udp_sender = UDPSender(
                    audio_queue=self._audio_queue,
                    opus_encoder=self._opus_encoder,
                    on_stats=self._on_pps_update,
                )
                self._udp_sender.set_party_mode(as_party or self.party_mode_var.get())
                self._udp_sender.configure(target_ip, target_port, codec, frame_ms, bind_ip=bind_ip)
                if as_party or self.party_mode_var.get():
                    for dev_ip, dev_info in self._party_devices.items():
                        self._udp_sender.add_party_target(dev_ip, dev_info.get("port", AUDIO_PORT))
                self._udp_sender.set_routing_and_gain(True, self.volume_gain_var.get(), self.muted_var.get())
                self._udp_sender.start()

                # Audio capture device
                dev_idx = None
                sel = self.audio_dev_combo.current()
                if sel >= 1 and (sel - 1) < len(self._audio_devices):
                    dev_idx = self._audio_devices[sel - 1]["index"]

                self._queue_adapter = _QueueAdapter(self._audio_queue, mirror_callback=None)
                self._audio_capture = AudioCapture(
                    device_index=dev_idx,
                    output_queue=self._queue_adapter,
                    on_error=self._on_audio_error,
                    frame_ms=frame_ms,
                )
                ok = self._audio_capture.start()
                if not ok:
                    self._stop_streaming()
                    self._status_set("CAPTURE ERROR", ERROR_COLOR)
                    return

        # 2. Always start Phone -> PC Receiver (UDP port 5006) so phone mic can connect at any time
        if self._phone_receiver is None:
            pc_out_idx = None
            out_sel = self.pc_output_combo.current()
            if 0 <= out_sel < len(self._pc_output_devices):
                pc_out_idx = self._pc_output_devices[out_sel]["index"]
            self._phone_receiver = PhoneAudioReceiver(
                port=PHONE_AUDIO_PORT,
                output_device_index=pc_out_idx,
                on_stats=self._on_rx_pps_update,
            )
            self._phone_receiver.start()

        self._streaming = True
        if as_party:
            self._party_streaming = True
            if hasattr(self, "party_start_btn"):
                self.party_start_btn.config(
                    text="■  STOP PARTY BROADCAST", bg="#330000", fg=ERROR_COLOR,
                    activebackground="#550000", activeforeground=ERROR_COLOR
                )
        else:
            self._normal_streaming = True
            self.start_btn.config(
                text="■  STOP SPEAKER STREAM", bg="#330000", fg=ERROR_COLOR,
                activebackground="#550000", activeforeground=ERROR_COLOR
            )

        self._update_combined_status()
        self._refresh_party_ui()
        logger.info("Streaming started (as_party=%s, target=%s:%d)", as_party, target_ip, target_port)
        cfg = self._get_current_config()
        for tip in self._get_all_target_ips():
            try:
                send_control_message(tip, cfg, CONTROL_PORT)
            except Exception:
                pass

    def _stop_stream(self, from_party: bool = False) -> None:
        if from_party:
            self._party_streaming = False
            if hasattr(self, "party_start_btn"):
                self.party_start_btn.config(
                    text="👑  START PARTY BROADCAST (HOST)",
                    bg="#002B00", fg=ACCENT,
                    activebackground="#004400", activeforeground=ACCENT
                )
            if self._normal_streaming and self._udp_sender:
                self._udp_sender.set_party_mode(False)
        else:
            self._normal_streaming = False
            self.start_btn.config(
                text="▶  START SPEAKER STREAM (PC → PHONE :5004)", bg="#003300", fg=ACCENT,
                activebackground="#005500", activeforeground=ACCENT
            )
            if self._party_streaming and self._udp_sender:
                self._udp_sender.set_party_mode(True)

        if not self._normal_streaming and not self._party_streaming:
            if self._audio_capture:
                self._audio_capture.stop()
                self._audio_capture = None
            self._queue_adapter = None
            if self._udp_sender:
                self._udp_sender.stop()
                self._udp_sender = None
            # Keep self._phone_receiver running so phone mic can connect at any time!
            if self._opus_encoder:
                try:
                    self._opus_encoder.destroy()
                except Exception:
                    pass
                self._opus_encoder = None

            self._streaming = False
            self._peak_level = 0.0
            self.pps_var.set("Speaker Packets: 0/s")
            self._level_canvas.coords(self._level_bar, 0, 0, 0, 12)

        self._update_combined_status()
        self._refresh_party_ui()
        logger.info("Streaming stopped (from_party=%s, normal=%s, party=%s)", from_party, self._normal_streaming, self._party_streaming)

    def _stop_streaming(self) -> None:
        self._stop_stream(from_party=False)
        self._stop_stream(from_party=True)
        self._stop_party_join()

    def _toggle_mic_receiver(self) -> None:
        """Start/Stop Phone -> PC Mic stream (Port 5006) and signal Phone via MIC_CONTROL."""
        if self._phone_receiver is None:
            pc_out_idx = None
            out_sel = self.pc_output_combo.current()
            if 0 <= out_sel < len(self._pc_output_devices):
                pc_out_idx = self._pc_output_devices[out_sel]["index"]
            self._phone_receiver = PhoneAudioReceiver(
                port=PHONE_AUDIO_PORT,
                output_device_index=pc_out_idx,
                on_stats=self._on_rx_pps_update,
            )
            self._phone_receiver.start()

        is_currently_active = bool(self._mic_active_requested or (self._rx_pps > 0 and not getattr(self._phone_receiver, "_manual_mute", False)))
        new_active = not is_currently_active
        self._mic_active_requested = new_active
        self._phone_receiver.set_muted(not new_active, manual=not new_active)
        self._mic_control_cooldown_until = time.monotonic() + 5.0
        self._last_phone_mic_state = new_active

        targets = self._get_all_target_ips()
        if not new_active:
            self.rx_pps_var.set("Mic Packets: 0/s")
            self.mic_state_var.set("Phone Mic: OFF")
            self.mic_btn.config(
                text="🎤  START MIC STREAM / RECEIVER (PHONE → PC :5006)",
                bg="#112222", fg="#00EEEE"
            )
            for tip in targets:
                send_control_message(tip, {"type": "MIC_CONTROL", "active": False}, CONTROL_PORT)
        else:
            self.mic_state_var.set("Phone Mic: LISTENING (:5006)")
            self.mic_btn.config(
                text="■  STOP MIC RECEIVER (PHONE → PC :5006 ACTIVE)",
                bg="#003344", fg="#00FF00"
            )
            for tip in targets:
                send_control_message(tip, {"type": "MIC_CONTROL", "active": True}, CONTROL_PORT)
        self._update_combined_status()

    def _update_combined_status(self) -> None:
        spk = f"SPEAKER: STREAMING → {self.ip_var.get().strip()}" if self._streaming else "SPEAKER: IDLE"
        mic_muted = getattr(self._phone_receiver, "_muted", False) if self._phone_receiver else True
        mic_on = bool(self._phone_receiver and self._phone_receiver.is_running and not mic_muted)
        if mic_on and self._rx_pps > 0:
            mic = f"MIC RX: LIVE ({self._rx_pps} pps)"
        elif mic_on:
            mic = "MIC RX: LISTENING (:5006)"
        else:
            mic = "MIC RX: STOPPED"
        self._status_set(f"{spk} | {mic}", ACCENT if (self._streaming or (mic_on and self._rx_pps > 0)) else FG)

    # ------------------------------------------------------------------
    # Background Event Callbacks & UI Queue
    # ------------------------------------------------------------------

    def _on_pps_update(self, pps: int) -> None:
        self._pps = pps
        self._ui_queue.put(("pps", pps))

    def _on_rx_pps_update(self, pps: int) -> None:
        self._rx_pps = pps
        self._ui_queue.put(("rx_pps", pps))

    def _on_audio_error(self, msg: str) -> None:
        self._ui_queue.put(("error", msg))

    def _on_beacon(self, msg: dict) -> None:
        self._ui_queue.put(("beacon", msg))

    def _on_phone_stats(self, stats: dict) -> None:
        self._phone_stats = stats
        self._ui_queue.put(("stats", stats))

    def _draw_delay_graph(self, current_buf: float, target_buf: float, est_total: int, losses: int) -> None:
        self._delay_history.append(current_buf)
        self._target_history.append(target_buf)

        self.delay_header_var.set(
            f"Delay: {current_buf:.1f} ms (Est. Total: ~{est_total} ms) | Target: {target_buf:.0f} ms | Loss: {losses}"
        )

        c = self._delay_canvas
        w = c.winfo_width()
        if w <= 10:
            w = 360
        h = c.winfo_height()
        if h <= 10:
            h = 75
        max_ms = 160.0

        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill="#080808", outline="")

        # Grid lines at 50, 100, 150 ms
        for ms in [50.0, 100.0, 150.0]:
            y = max(1.0, min(float(h - 1), h - (ms / max_ms) * h))
            c.create_line(0, y, w, y, fill="#222222", width=1)

        # Title overlay
        c.create_text(8, 10, text="LIVE DELAY GRAPH (0 - 160 ms)", fill="#555555", anchor="w", font=FONT_MONO_SM)

        # Target line (dashed)
        target_y = max(2.0, min(float(h - 2), h - (min(target_buf, max_ms) / max_ms) * h))
        c.create_line(0, target_y, w, target_y, fill="#888888", width=1, dash=(4, 4))

        # Actual buffer line
        points = []
        n_pts = len(self._delay_history)
        step_x = float(w) / max(1, n_pts - 1)
        for idx, val in enumerate(self._delay_history):
            x = idx * step_x
            y = max(2.0, min(float(h - 2), h - (min(val, max_ms) / max_ms) * h))
            points.extend([x, y])

        line_color = WARN if (current_buf > target_buf + 20.0) else ACCENT
        if len(points) >= 4:
            c.create_line(*points, fill=line_color, width=2)

    def _schedule_ui_update(self) -> None:
        self._process_ui_queue()

        cap_level = getattr(self._audio_capture, "peak_level", 0.0) if (self._streaming and self._audio_capture) else 0.0
        recv_level = getattr(self._phone_receiver, "peak_level", 0.0) if self._phone_receiver else 0.0
        target_level = max(cap_level, recv_level)
        if self._audio_capture:
            self._audio_capture.peak_level = 0.0
        if self._phone_receiver:
            self._phone_receiver.peak_level = 0.0

        if target_level > self._peak_level:
            self._peak_level = target_level
        else:
            self._peak_level = max(0.0, self._peak_level * 0.80 - 0.01)
        canvas_w = max(100, self._level_canvas.winfo_width())
        bar_w = int(min(1.0, self._peak_level) * canvas_w)
        self._level_canvas.coords(self._level_bar, 0, 0, bar_w, 12)

        # Live Delay Graph update at 100 ms interval
        now = time.monotonic()
        if (now - self._last_graph_tick) >= 0.1:
            self._last_graph_tick = now
            buf_val = 0.0
            tgt_val = float(self.custom_buffer_var.get() or 30.0)
            losses = 0
            if self._phone_stats:
                b = self._phone_stats.get("buffer_ms")
                if isinstance(b, (int, float)):
                    buf_val = float(b)
                t = self._phone_stats.get("target_ms")
                if isinstance(t, (int, float)):
                    tgt_val = float(t)
                losses = self._phone_stats.get("lost_packets", 0)
            est_total, _ = self._compute_expected_delay_ms()
            self._draw_delay_graph(buf_val, tgt_val, est_total, losses)

        # Poll Windows Sound Bar / Linux system volume (every ~100ms)
        if getattr(self, "sync_os_vol_var", None) and self.sync_os_vol_var.get():
            if not hasattr(self, "_last_vol_poll"):
                self._last_vol_poll = 0.0
            if (now - self._last_vol_poll) >= 0.10:
                self._last_vol_poll = now
                os_state = get_system_volume_and_mute()
                if os_state is not None:
                    os_vol, os_mute = os_state
                    if self._last_os_vol is None:
                        self._last_os_vol = os_vol
                        self._last_os_mute = os_mute
                    elif abs(os_vol - self._last_os_vol) >= 0.015 or os_mute != self._last_os_mute:
                        self._last_os_vol = os_vol
                        self._last_os_mute = os_mute
                        self._suppress_sync = True
                        try:
                            self.volume_gain_var.set(round(os_vol, 2))
                            self.muted_var.set(os_mute)
                            self.gain_val_label.config(text=f"{int(os_vol * 100)}%")
                            if hasattr(self, "conn_vol_pct_label"):
                                self.conn_vol_pct_label.config(text=f"{int(os_vol * 100)}%")
                            self.mute_btn.config(bg="#990000" if os_mute else "#333333", text="MUTED" if os_mute else "MUTE")
                            if hasattr(self, "conn_mute_icon_btn"):
                                self.conn_mute_icon_btn.config(text="🔇" if os_mute else "🔊")
                        finally:
                            self._suppress_sync = False
                        self._on_local_setting_changed()

        # Check phone connection timeout (heartbeat > 5.0s)
        if hasattr(self, "_last_phone_seen_time") and self._last_phone_seen_time > 0:
            if (now - self._last_phone_seen_time) > 5.0:
                self.conn_device_ip_var.set(f"{self.ip_var.get().strip() or 'Phone'}  •  WAITING / OFFLINE")

        if not hasattr(self, "_last_party_refresh"):
            self._last_party_refresh = 0.0
        if (now - self._last_party_refresh) >= 1.0:
            self._last_party_refresh = now
            self._refresh_party_ui()
            self._refresh_discovered_hosts_ui()
            if getattr(self, "_party_view_state", "CHOOSER") == "HOST":
                self._refresh_party_qr()
            elif getattr(self, "party_extend_var", None) and self.party_extend_var.get():
                self._refresh_party_extend_qr()

            # Keep-alive re-announcement to host if PC is an active party joiner
            if getattr(self, "_party_joined_active", False) and self._party_join_receiver and self._party_join_receiver.is_running:
                host_ip = self.party_join_host_ip_var.get().strip()
                if host_ip:
                    try:
                        port = int(self.party_join_port_var.get().strip())
                    except ValueError:
                        port = AUDIO_PORT
                    try:
                        send_control_message(
                            host_ip,
                            {
                                "type": "BEACON",
                                "app": "PhoneSpeaker",
                                "version": 2,
                                "party_join": True,
                                "is_party_host": False,
                                "is_extended_host": bool(self.party_extend_var.get()),
                                "device": socket.gethostname(),
                                "audio_port": port,
                            },
                            CONTROL_PORT,
                        )
                    except Exception:
                        pass

        self.root.after(50, self._schedule_ui_update)

    def _process_ui_queue(self) -> None:
        try:
            while True:
                item = self._ui_queue.get_nowait()
                kind, data = item[0], item[1]

                if kind in ("beacon", "stats", "config_sync"):
                    self._last_phone_seen_time = time.monotonic()

                if kind == "pps":
                    self.pps_var.set(f"Speaker Packets: {data}/s")
                elif kind == "rx_pps":
                    self._rx_pps = data
                    self.rx_pps_var.set(f"Mic Packets: {data}/s")
                    if data > 0 and time.monotonic() >= getattr(self, "_mic_control_cooldown_until", 0.0):
                        self._mic_active_requested = True
                        self._last_phone_mic_state = True
                        if self._phone_receiver:
                            self._phone_receiver.set_muted(False, manual=False)
                        self.mic_btn.config(
                            text="■  STOP MIC RECEIVER (PHONE → PC :5006 ACTIVE)",
                            bg="#003344", fg="#00FF00"
                        )
                        self.mic_state_var.set("Phone Mic: STREAMING")
                    else:
                        self.mic_state_var.set("Phone Mic: STREAMING" if data > 0 else "Phone Mic: LISTENING")
                    self._update_combined_status()
                elif kind == "driver_done":
                    ok, msg = data
                    if ok:
                        messagebox.showinfo("Virtual Audio Setup", msg)
                        self._check_virtual_device_status()
                        self._refresh_audio_devices()
                        self._refresh_pc_outputs()
                        self._status_set("VIRTUAL AUDIO READY", ACCENT)
                    else:
                        messagebox.showerror("Virtual Audio Error", msg)
                        self._status_set("VIRTUAL AUDIO FAILED", ERROR_COLOR)
                elif kind == "error":
                    self._status_set(f"ERROR: {data}", ERROR_COLOR)
                    if self._streaming:
                        self._stop_streaming()
                elif kind == "config_sync":
                    sender_ip = data.get("_sender_ip")
                    if sender_ip:
                        if data.get("party_join") or (self._party_view_state == "HOST" and self.party_mode_var.get()):
                            self._register_party_device(sender_ip, data.get("device", "Android Phone"), data.get("audio_port", AUDIO_PORT), force=True)
                        if data.get("party_mode") or data.get("party_host") or data.get("role") == "HOST":
                            self._discovered_party_hosts[sender_ip] = {
                                "name": data.get("device", "Phone Host"),
                                "last_seen": time.monotonic(),
                                "port": data.get("audio_port", AUDIO_PORT),
                            }
                    self._apply_incoming_config(data)
                elif kind == "beacon":
                    msg = data
                    sender_ip = msg.get("_sender_ip")
                    ips = msg.get("ips", [])
                    candidates = []
                    if sender_ip and sender_ip not in candidates:
                        candidates.append(sender_ip)
                    for ip in ips:
                        if ip and ip not in candidates:
                            candidates.append(ip)

                    if not candidates:
                        continue

                    adapter_sel = self.adapter_combo.current()
                    active_adapter_ip = ""
                    if 0 <= adapter_sel < len(self._adapter_data):
                        active_adapter_ip = self._adapter_data[adapter_sel].get("ip", "")

                    all_up_adapters = [(i, a) for i, a in enumerate(self._adapter_data) if a.get("is_up")]
                    best_ip = None
                    matching_adapter_idx = None

                    if active_adapter_ip:
                        act_prefix = ".".join(active_adapter_ip.split(".")[:3]) + "."
                        for c in candidates:
                            if c.startswith(act_prefix):
                                best_ip = c
                                break

                    if not best_ip:
                        for idx, a in all_up_adapters:
                            a_ip = a.get("ip", "")
                            if a_ip:
                                a_prefix = ".".join(a_ip.split(".")[:3]) + "."
                                for c in candidates:
                                    if c.startswith(a_prefix):
                                        best_ip = c
                                        matching_adapter_idx = idx
                                        break
                            if best_ip:
                                break

                    if not best_ip:
                        best_ip = candidates[0]

                    dev_name = msg.get("device", "")
                    if dev_name and dev_name != "Android":
                        self._connected_phone_name = dev_name
                        self.conn_device_name_var.set(dev_name)
                    elif self._connected_phone_name == "Waiting for phone...":
                        self.conn_device_name_var.set("Android Phone")
                    self.conn_device_ip_var.set(f"{best_ip}  •  CONNECTED")
                    if (self._party_view_state == "HOST" and self.party_mode_var.get()) or msg.get("party_join"):
                        if self._party_view_state == "HOST" or getattr(self, "party_extend_var", tk.BooleanVar(value=False)).get():
                            self._register_party_device(best_ip, dev_name or "Android Phone", msg.get("audio_port", AUDIO_PORT), force=True)
                    if msg.get("party_mode") or msg.get("party_host") or msg.get("is_party_host") or msg.get("is_extended_host") or msg.get("role") == "HOST":
                        self._discovered_party_hosts[best_ip] = {
                            "name": dev_name or "Phone Host",
                            "last_seen": time.monotonic(),
                            "port": msg.get("audio_port", AUDIO_PORT),
                        }

                    if matching_adapter_idx is not None and matching_adapter_idx != adapter_sel:
                        self.adapter_combo.current(matching_adapter_idx)
                        self._on_adapter_selected()

                    self.phone_ip_var.set(f"Phone: {best_ip}")

                    is_ip_focused = False
                    try:
                        is_ip_focused = (self.root.focus_get() == self.ip_entry)
                    except Exception:
                        pass

                    if not is_ip_focused:
                        current_ip = self.ip_var.get().strip()
                        should_update_ip = (not current_ip) or (current_ip not in candidates)
                        if not should_update_ip and active_adapter_ip:
                            act_prefix = ".".join(active_adapter_ip.split(".")[:3]) + "."
                            if not current_ip.startswith(act_prefix) and best_ip.startswith(act_prefix):
                                should_update_ip = True

                        if should_update_ip:
                            old_ip = current_ip
                            self.ip_var.set(best_ip)
                            if self._streaming and self._udp_sender and best_ip != current_ip:
                                try:
                                    target_port = int(self.ip_port_var.get().strip())
                                except ValueError:
                                    target_port = AUDIO_PORT
                                self._udp_sender.update_target(best_ip, target_port)
                                self._update_combined_status()
                            if old_ip != best_ip or not old_ip:
                                try:
                                    send_control_message(best_ip, self._get_current_config(), CONTROL_PORT)
                                except Exception:
                                    pass

                elif kind == "stats":
                    s = data
                    dev_name = s.get("device", "")
                    if dev_name and dev_name != "Android":
                        self._connected_phone_name = dev_name
                        self.conn_device_name_var.set(dev_name)
                    elif self._connected_phone_name == "Waiting for phone...":
                        self.conn_device_name_var.set("Android Phone")
                    cur_ip = self.ip_var.get().strip()
                    sender_ip = s.get("_sender_ip") or cur_ip
                    if cur_ip:
                        self.conn_device_ip_var.set(f"{cur_ip}  •  CONNECTED")
                    if sender_ip and self._party_view_state == "HOST" and self.party_mode_var.get():
                        self._register_party_device(sender_ip, dev_name or "Android Phone", s.get("audio_port", AUDIO_PORT))

                    phone_mic_active = bool(s.get("mic_streaming", False))
                    if time.monotonic() >= getattr(self, "_mic_control_cooldown_until", 0.0):
                        effective_mic_active = phone_mic_active or (self._rx_pps > 0)
                        if effective_mic_active != getattr(self, "_last_phone_mic_state", None):
                            self._last_phone_mic_state = effective_mic_active
                            if self._phone_receiver:
                                self._phone_receiver.set_muted(not effective_mic_active, manual=False)
                            if effective_mic_active:
                                self.mic_btn.config(
                                    text="■  STOP MIC RECEIVER (PHONE → PC :5006 ACTIVE)",
                                    bg="#003344", fg="#00FF00"
                                )
                                self.mic_state_var.set("Phone Mic: STREAMING")
                            else:
                                self.mic_btn.config(
                                    text="🎤  START MIC STREAM / RECEIVER (PHONE → PC :5006)",
                                    bg="#112222", fg="#00EEEE"
                                )
                                self.mic_state_var.set("Phone Mic: READY")
                            self._update_combined_status()

                    self.latency_var.set(
                        f"Phone Buffer: {s.get('buffer_ms', '--'):.1f} ms"
                        if isinstance(s.get("buffer_ms"), (int, float)) else "Phone Buffer: --"
                    )
                    lost = s.get("lost_packets", "--")
                    self.loss_var.set(f"Dropped: {lost}")
        except queue.Empty:
            pass

    def _status_set(self, text: str, color: str = FG) -> None:
        self.status_var.set(text)
        self.status_label.config(fg=color)

    # ------------------------------------------------------------------
    # Virtual Device & Driver Actions
    # ------------------------------------------------------------------

    def _install_driver(self) -> None:
        threading.Thread(target=self._do_install_driver, daemon=True).start()

    def _do_install_driver(self) -> None:
        ok, msg = download_and_install_vbcable()
        self._ui_queue.put(("driver_done", (ok, msg)))

    def _rename_device(self) -> None:
        ok, msg = set_virtual_device_friendly_name("PhoneSpeaker Virtual Audio")
        if ok:
            messagebox.showinfo("Virtual Device", msg)
            self._check_virtual_device_status()
        else:
            messagebox.showerror("Error", msg)

    def _show_firewall_dialog(self) -> None:
        from firewall import get_firewall_commands, apply_firewall_rules
        dlg = tk.Toplevel(self.root)
        dlg.title("Firewall Rules")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)

        tk.Label(
            dlg, text="Windows Firewall Configuration",
            bg=BG, fg=FG, font=FONT_MONO_LG
        ).pack(anchor="w", padx=12, pady=(12, 6))

        tk.Label(
            dlg,
            text="Inbound UDP ports 5004, 5005, and 5006 must be allowed\nso the phone and PC can stream audio and sync settings.",
            bg=BG, fg=FG, font=FONT_MONO, justify="left",
        ).pack(anchor="w", padx=12, pady=4)

        btn_f = ttk.Frame(dlg)
        btn_f.pack(fill="x", padx=12, pady=12)

        def _apply():
            ok, msg = apply_firewall_rules()
            if ok:
                messagebox.showinfo("Firewall Rules", f"Rules applied successfully!\n\n{msg}", parent=dlg)
                dlg.destroy()
            else:
                messagebox.showwarning("Firewall Rules", msg, parent=dlg)

        ttk.Button(btn_f, text="Apply Automatically (Admin Prompt)", command=_apply).pack(side="left")
        ttk.Button(btn_f, text="Close", command=dlg.destroy).pack(side="right")

    # ------------------------------------------------------------------
    # Config & Lifecycle
    # ------------------------------------------------------------------

    def _load_config_into_ui(self) -> None:
        phone_ip = self.cfg.get("phone_ip") or self.cfg.get("target_ip", "")
        if phone_ip:
            self.ip_var.set(phone_ip)
        self.ip_port_var.set(str(self.cfg.get("phone_port", self.cfg.get("target_port", AUDIO_PORT))))
        self.codec_var.set(int(self.cfg.get("codec", CODEC_OPUS)))
        self.bitrate_var.set(str(self.cfg.get("opus_bitrate_kbps", 192)))
        self.frame_ms_var.set(str(self.cfg.get("frame_ms", 5)))
        self.buffer_mode_var.set(self.cfg.get("buffer_mode", "AUTO"))
        self.custom_buffer_var.set(float(self.cfg.get("custom_buffer_ms", 30.0)))
        self.buffer_val_label.config(text=f"{self.custom_buffer_var.get():.1f} ms")
        self.volume_gain_var.set(float(self.cfg.get("volume_gain", 1.0)))
        self.gain_val_label.config(text=f"{int(self.volume_gain_var.get() * 100)}%")
        if hasattr(self, "conn_vol_pct_label"):
            self.conn_vol_pct_label.config(text=f"{int(self.volume_gain_var.get() * 100)}%")
        m = bool(self.cfg.get("muted", False))
        self.muted_var.set(m)
        self.mute_btn.config(bg="#990000" if m else "#333333", text="MUTED" if m else "MUTE")
        if hasattr(self, "conn_mute_icon_btn"):
            self.conn_mute_icon_btn.config(text="🔇" if m else "🔊")
        self.direction_var.set(self.cfg.get("direction", "PC_TO_PHONE"))
        self.audio_profile_var.set(self.cfg.get("audio_profile", "BALANCED"))
        self.tray_var.set(bool(self.cfg.get("minimize_to_tray", True)))
        self.party_mode_var.set(bool(self.cfg.get("party_mode", False)))
        self._update_codec_ui_state()
        self._update_expected_delay_label()

    def _save_config(self) -> None:
        self.cfg["phone_ip"] = self.ip_var.get().strip()
        try:
            self.cfg["phone_port"] = int(self.ip_port_var.get().strip())
        except ValueError:
            pass
        self.cfg["codec"] = self.codec_var.get()
        self.cfg["opus_bitrate_kbps"] = int(self.bitrate_var.get())
        self.cfg["bitrate"] = int(self.bitrate_var.get()) * 1000
        self.cfg["frame_ms"] = int(self.frame_ms_var.get())
        self.cfg["buffer_mode"] = self.buffer_mode_var.get()
        self.cfg["custom_buffer_ms"] = self.custom_buffer_var.get()
        self.cfg["volume_gain"] = self.volume_gain_var.get()
        self.cfg["muted"] = self.muted_var.get()
        self.cfg["direction"] = self.direction_var.get()
        self.cfg["audio_profile"] = self.audio_profile_var.get()
        self.cfg["minimize_to_tray"] = self.tray_var.get()
        self.cfg["audio_output_engine"] = self._phone_audio_engine
        self.cfg["enable_audio_effects"] = self._phone_audio_fx
        self.cfg["exclusive_audio_mode"] = self._phone_exclusive_mode
        self.cfg["stop_on_headset_disconnect"] = self._phone_headset_disc
        self.cfg["party_mode"] = self.party_mode_var.get()
        self.cfg["party_ips"] = list(self._party_devices.keys())
        self.cfg["config_rev"] = self._config_rev
        save(self.cfg)

    def _show_help_dialog(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("PhoneSpeaker v2.0 Studio — User Guide & Help")
        dlg.configure(bg=BG)
        dlg.geometry("680x580")
        dlg.resizable(True, True)

        header = tk.Frame(dlg, bg=BG)
        header.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(
            header, text="◀ PhoneSpeaker v2.0 Studio Guide ▶",
            bg=BG, fg=ACCENT, font=FONT_MONO_LG
        ).pack(side="left")
        ttk.Button(header, text="✕ Close", command=dlg.destroy).pack(side="right")

        text_frame = tk.Frame(dlg, bg=BG)
        text_frame.pack(fill="both", expand=True, padx=12, pady=6)

        scrollbar = ttk.Scrollbar(text_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        guide_text = tk.Text(
            text_frame, bg="#0A0A0A", fg=FG, insertbackground=FG,
            font=FONT_MONO_SM, wrap="word", yscrollcommand=scrollbar.set,
            padx=10, pady=10, bd=1, relief="solid"
        )
        guide_text.pack(fill="both", expand=True)
        scrollbar.config(command=guide_text.yview)

        help_content = """============================================================
PHONESPEAKER v2.0 STUDIO — COMPLETE USER GUIDE (WINDOWS & LINUX)
============================================================

✓ VERIFIED STATUS: SPEAKER & MIC STREAMING ARE WORKING!
------------------------------------------------------------
* Both PC (.exe / Linux binary) and Android now have TWO SEPARATE
  dedicated buttons on the main screen:
  1. [ ▶ START SPEAKER STREAM (PC -> PHONE :5004) ]
  2. [ 🎤 START / STOP MIC RECEIVER (PHONE -> PC :5006) ]
* Clicking the Mic button on PC toggles the UDP 5006 receiver AND
  remotely signals your Phone to start/stop its Microphone Server!
* How to verify Mic Streaming is working:
  - Check 'MIC RX PPS: ~200' and 'Phone Mic: STREAMING' in Live Status.
  - Speak into your phone and watch the green Level Meter move on PC!

1. PC -> PHONE WIRELESS SPEAKER (PORT 5004)
------------------------------------------------------------
* Connect both PC and Phone to the same Wi-Fi, Phone Hotspot, or USB Tether.
* Click [ ▶ START SPEAKER STREAM ] on PC and [ ▶ START SPEAKER PLAYER ] on Phone.
* On Linux: Automatically captures PipeWire / PulseAudio monitor loopback.
* On Windows: Automatically captures WASAPI loopback.

2. PHONE -> PC WIRELESS MICROPHONE (PORT 5006)
------------------------------------------------------------
* Tap [ 🎤 START MIC STREAM TO PC (PORT 5006) ] on the Phone's main tab
  (or click the Mic button on PC once the phone is discovered).
* Routing Phone Mic to Discord / Zoom / OBS:
  - ON WINDOWS: Install VB-CABLE in Tab 3, set 'PC Playback Dev' on Tab 1
    to 'CABLE Input', and set Discord/Zoom input to 'CABLE Output'.
  - ON LINUX: In Tab 3, click [ Install Virtual Audio Driver ] to create
    a native PipeWire/PulseAudio virtual mic ('PhoneSpeaker_Mic'). Set
    'PC Playback Dev' to '[PipeWire/Pulse] PhoneSpeaker_Mic_Sink', and
    choose 'PhoneSpeaker_Wireless_Mic' as your mic in Discord/Zoom/OBS!
============================================================
"""
        guide_text.insert("1.0", help_content)
        guide_text.config(state="disabled")

    def _on_close(self) -> None:
        if self.tray_var.get() and _TRAY_AVAILABLE:
            self.root.withdraw()
            if not self._tray_icon:
                self._setup_tray()
        else:
            self._quit_app()

    def _setup_tray(self) -> None:
        if not _TRAY_AVAILABLE or pystray is None:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Open", self._on_tray_open, default=True),
                pystray.MenuItem("Exit", self._on_tray_exit),
            )
            img = _make_tray_image(64)
            if img is None:
                return
            self._tray_icon = pystray.Icon("PhoneSpeaker", img, "PhoneSpeaker v2.0", menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception as e:
            logger.warning("Failed to start tray icon: %s", e)

    def _on_tray_open(self, icon=None, item=None) -> None:
        self.root.after(0, self._restore_from_tray)

    def _restore_from_tray(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def _on_tray_exit(self, icon=None, item=None) -> None:
        self.root.after(0, self._quit_app)

    def _quit_app(self) -> None:
        if self._streaming:
            self._stop_streaming()
        if self._phone_receiver:
            self._phone_receiver.stop()
            self._phone_receiver = None
        if self._control_listener:
            self._control_listener.stop()
            self._control_listener = None
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self._save_config()
        self.root.destroy()
        sys.exit(0)


def run_ui(cfg: Dict[str, Any]) -> None:
    root = tk.Tk()
    app = PhoneSpeakerApp(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    from config import load
    run_ui(load())
