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
    PCDelayedAudioPlayer,
    PhoneAudioReceiver,
    list_output_devices,
    normalize_device_name,
)
from virtual_device import (
    detect_virtual_devices,
    open_windows_sound_settings,
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
_MONO_FAMILY = "DejaVu Sans Mono" if sys.platform.startswith("linux") else "Consolas"
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
        self.auto_pc_sync_var = tk.BooleanVar(value=True)
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
        self.output_targets_var = tk.StringVar(value=self.cfg.get("output_targets", "PHONE_ONLY"))
        self.pc_sync_delay_var = tk.IntVar(value=int(self.cfg.get("pc_sync_delay_ms", 25)))
        self.audio_profile_var = tk.StringVar(value=self.cfg.get("audio_profile", "BALANCED"))
        self.expected_delay_var = tk.StringVar(value="")
        self.delay_header_var = tk.StringVar(value="Delay: -- ms (Est. Total: -- ms) | Target: 30.0 ms | Loss: 0")
        self._manual_ip_override: Optional[str] = None
        self._last_phone_seen_time: float = 0.0

        # Audio threads & engines
        self._audio_capture: Optional[AudioCapture] = None
        self._udp_sender: Optional[UDPSender] = None
        self._pc_player: Optional[PCDelayedAudioPlayer] = None
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
        self.tab_audio = tk.Frame(self.notebook, bg=BG)
        self.tab_virtual = tk.Frame(self.notebook, bg=BG)

        self.notebook.add(self.tab_stream, text="  Stream / Roles  ")
        self.notebook.add(self.tab_audio, text="  Audio & Buffer  ")
        self.notebook.add(self.tab_virtual, text="  Virtual Device (Win+Ctrl+V)  ")

        self._build_tab_stream()
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

        # 3. Speaker Output Targets & Dual-Speaker Sync
        of = tk.LabelFrame(f, text=" Where to Play Sound (Speaker Routing) ", bg=BG, fg=FG, font=FONT_MONO_BOLD, bd=1, relief="solid", padx=6, pady=3)
        of.pack(fill="x", **pad)
        of.columnconfigure(1, weight=1)

        tk.Label(of, text="Output Target:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, width=15, anchor="e").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        tgt_row = tk.Frame(of, bg=BG)
        tgt_row.grid(row=0, column=1, sticky="w", padx=2, pady=2)
        for text, val in [
            ("Phone Only", "PHONE_ONLY"),
            ("Phone + PC Together (Auto-Synced)", "BOTH_SYNCED"),
            ("PC Speaker Only", "PC_ONLY"),
        ]:
            tk.Radiobutton(
                tgt_row, text=text, variable=self.output_targets_var, value=val,
                bg=BG, fg=FG, selectcolor="#222222", font=FONT_MONO_SM,
                activebackground=BG, activeforeground=ACCENT,
                command=self._on_output_target_changed
            ).pack(side="left", padx=(0, 8))

        tk.Label(of, text="Speaker Sync:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, width=15, anchor="e").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        sync_row = tk.Frame(of, bg=BG)
        sync_row.grid(row=1, column=1, sticky="ew", padx=4, pady=2)
        self.sync_delay_scale = tk.Scale(
            sync_row, from_=0, to=150, orient="horizontal", variable=self.pc_sync_delay_var,
            showvalue=False, bg=BG, fg=FG, troughcolor="#222222", highlightthickness=0, bd=0,
            command=self._on_pc_delay_changed
        )
        self.sync_delay_scale.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.sync_delay_scale.config(state="disabled")
        self.sync_delay_val_label = tk.Label(sync_row, text=f"{self.pc_sync_delay_var.get()} ms", bg=BG, fg=ACCENT, font=FONT_MONO_BOLD, width=11, anchor="e")
        self.sync_delay_val_label.pack(side="left")
        tk.Checkbutton(
            sync_row, text="Auto-Sync (100% Automatic)", variable=self.auto_pc_sync_var,
            bg=BG, fg="#00EEEE", selectcolor="#222222", activebackground=BG,
            font=FONT_MONO_SM, command=self._on_auto_pc_sync_toggled
        ).pack(side="right", padx=(4, 0))

        tk.Label(of, text="PC Speaker / Cable:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, width=15, anchor="e").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        self.pc_output_combo = ttk.Combobox(of, state="readonly", font=FONT_MONO_SM)
        self.pc_output_combo.grid(row=2, column=1, sticky="ew", padx=4, pady=2)
        self.pc_output_combo.bind("<<ComboboxSelected>>", self._on_pc_output_selected)

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
    # TAB 2: Audio & Buffer Settings (Two-Way Synced)
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

        vf = tk.LabelFrame(f, text=" Windows Virtual Audio Output Integration ", bg=BG, fg=FG, font=FONT_MONO, bd=1, relief="solid")
        vf.pack(fill="x", **pad)

        tk.Label(
            vf, text="To stream all PC audio without echo or feedback,\nuse PhoneSpeaker / AudioRelay Virtual Speakers as Windows Output:",
            bg=BG, fg=FG_MUTED, font=FONT_MONO_SM, justify="left"
        ).pack(anchor="w", padx=8, pady=4)

        self.virt_status_label = tk.Label(vf, text="Detecting virtual audio driver...", bg=BG, fg=ACCENT, font=FONT_MONO_BOLD)
        self.virt_status_label.pack(anchor="w", padx=8, pady=2)

        btn_row1 = tk.Frame(vf, bg=BG)
        btn_row1.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn_row1, text="🔊 Switch Windows Output (Win+Ctrl+V)", command=open_windows_sound_settings).pack(side="left", padx=2)
        ttk.Button(btn_row1, text="⚙ Sound Control Panel", command=open_classic_sound_control_panel).pack(side="left", padx=2)

        btn_row2 = tk.Frame(vf, bg=BG)
        btn_row2.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn_row2, text="📥 Install Virtual Audio Driver (VB-CABLE)", command=self._install_driver).pack(side="left", padx=2)
        ttk.Button(btn_row2, text="🏷 Rename to PhoneSpeaker Audio", command=self._rename_device).pack(side="left", padx=2)

        # Audio Capture Source Selector
        cf = tk.LabelFrame(f, text=" PC Capture Source (for PC → Phone Stream) ", bg=BG, fg=FG, font=FONT_MONO, bd=1, relief="solid")
        cf.pack(fill="x", **pad)

        tk.Label(cf, text="Source Device:", bg=BG, fg=FG_MUTED, font=FONT_MONO_SM).grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.audio_dev_combo = ttk.Combobox(cf, width=30, state="readonly", font=FONT_MONO_SM)
        self.audio_dev_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self.audio_dev_combo.bind("<<ComboboxSelected>>", self._on_capture_source_selected)
        ttk.Button(cf, text="↻ Refresh", command=self._refresh_all_devices).grid(row=0, column=2, padx=4, pady=4)
        cf.columnconfigure(1, weight=1)

        # Firewall & System Options
        sf = tk.LabelFrame(f, text=" System & Firewall ", bg=BG, fg=FG, font=FONT_MONO, bd=1, relief="solid")
        sf.pack(fill="x", **pad)

        ttk.Button(sf, text="🔥 Add Firewall Rules (5004, 5005, 5006)", command=self._show_firewall_dialog).pack(side="left", padx=8, pady=6)
        self.tray_var = tk.BooleanVar(value=self.cfg.get("minimize_to_tray", True))
        tk.Checkbutton(
            sf, text="Minimize to system tray", variable=self.tray_var,
            bg=BG, fg=FG, selectcolor="#333333", font=FONT_MONO
        ).pack(side="right", padx=8, pady=6)

        self._check_virtual_device_status()

    # ------------------------------------------------------------------
    # Virtual Device & Device Queries
    # ------------------------------------------------------------------

    def _check_virtual_device_status(self) -> None:
        devs = detect_virtual_devices()
        virt = [d for d in devs if d.get("is_virtual")]
        if virt:
            self.virt_status_label.config(text=f"DETECTED: {virt[0]['name']}", fg=ACCENT)
        else:
            self.virt_status_label.config(text="No virtual speaker detected. Click [ Install Driver ] below.", fg=WARN)

    def _refresh_all_devices(self) -> None:
        self._refresh_adapters()
        self._refresh_audio_devices()
        self._refresh_pc_outputs()

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
            self._sync_live_pc_player()

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
        entries = ["Auto (Windows Default Loopback)"] + [f"[Loopback] {d['name']}" for d in loopbacks] + [f"[Mic] {d['name']}" for d in inputs]
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

    def _on_pc_output_selected(self, event=None) -> None:
        sel = self.pc_output_combo.current()
        pc_out_idx = None
        if 0 <= sel < len(self._pc_output_devices):
            pc_out_idx = self._pc_output_devices[sel]["index"]
        if self._phone_receiver:
            self._phone_receiver.set_output_device(pc_out_idx)
        if self._pc_player:
            self._pc_player.set_output_device(pc_out_idx)

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

    def _on_manual_ip_applied(self, event=None) -> None:
        new_ip, new_port = self._sanitize_ip_and_port()
        if not new_ip:
            return
        self._manual_ip_override = new_ip
        self.phone_ip_var.set(f"Phone: {new_ip}")
        self.conn_device_ip_var.set(f"{new_ip}  •  TARGET SET")
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
            "output_targets": self.output_targets_var.get(),
            "pc_sync_delay_ms": self.pc_sync_delay_var.get(),
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
        }

    def _get_safe_pc_playback_device(self) -> Optional[object]:
        """
        Return a physical PC output device index that is distinct from the active capture loopback device.
        If AudioCapture is already capturing directly from the only physical speaker, return None so
        PCDelayedAudioPlayer does not inject delayed audio back into the active loopback (which causes phone echo).
        """
        if not self._pc_output_devices:
            return None

        cap_name = ""
        if self._audio_capture and getattr(self._audio_capture, "active_device_name", ""):
            cap_name = self._audio_capture.active_device_name
        else:
            sel = self.audio_dev_combo.current()
            if sel == 0:
                if self._audio_devices:
                    cap_name = self._audio_devices[0].get("name", "")
                else:
                    loopbacks = list_loopback_devices()
                    if loopbacks:
                        cap_name = loopbacks[0].get("name", "")
            elif sel >= 1 and (sel - 1) < len(self._audio_devices):
                cap_name = self._audio_devices[sel - 1].get("name", "")
        cap_norm = normalize_device_name(cap_name)

        out_sel = self.pc_output_combo.current()
        current_dev = self._pc_output_devices[out_sel] if (0 <= out_sel < len(self._pc_output_devices)) else None

        # 1. Check if the currently selected PC Playback Dev is a physical speaker distinct from cap_norm
        if current_dev and not current_dev.get("is_virtual", False):
            cur_norm = normalize_device_name(current_dev.get("name", ""))
            if not cap_norm or (cur_norm != cap_norm and cur_norm not in cap_norm and cap_norm not in cur_norm):
                return current_dev.get("index")

        # 2. Search for the first physical (non-virtual) speaker distinct from cap_norm
        for i, dev in enumerate(self._pc_output_devices):
            if dev.get("is_virtual", False):
                continue
            dev_norm = normalize_device_name(dev.get("name", ""))
            if not cap_norm or (dev_norm != cap_norm and dev_norm not in cap_norm and cap_norm not in dev_norm):
                self.pc_output_combo.current(i)
                logger.info(
                    "Selected physical PC Playback Dev '%s' (capture source is '%s')",
                    dev.get("name"), cap_name
                )
                return dev.get("index")

        # 3. If AudioCapture is already capturing the only physical speaker on the system,
        # Windows is already playing natively to the PC speaker! Return None to avoid loopback feedback echo.
        logger.info(
            "Capture source '%s' is already the active physical speaker; skipping duplicate PC playback to prevent echo",
            cap_name
        )
        return None

    def _sync_live_pc_player(self) -> None:
        """Start, update, or stop PCDelayedAudioPlayer dynamically when output_targets changes mid-stream."""
        if not self._streaming:
            return
        targets = self.output_targets_var.get()
        gain = self.volume_gain_var.get()
        muted = self.muted_var.get()
        delay = 0.0 if targets == "PC_ONLY" else float(self.pc_sync_delay_var.get())

        if targets in ("BOTH_SYNCED", "PC_ONLY"):
            pc_out_idx = self._get_safe_pc_playback_device()
            if pc_out_idx is None:
                # Physical speaker is already playing natively via Windows; ensure no feedback player is running
                if hasattr(self, "_queue_adapter") and self._queue_adapter is not None:
                    self._queue_adapter._mirror = None
                if self._pc_player is not None:
                    self._pc_player.stop()
                    self._pc_player = None
                return

            if self._pc_player is None:
                self._pc_player = PCDelayedAudioPlayer(device_index=pc_out_idx, delay_ms=delay)
                self._pc_player.set_gain_and_mute(gain, muted)
                self._pc_player.start()
                if hasattr(self, "_queue_adapter") and self._queue_adapter is not None:
                    self._queue_adapter._mirror = self._pc_player.write_frame
            else:
                self._pc_player.set_output_device(pc_out_idx)
                self._pc_player.set_delay_ms(delay)
                self._pc_player.set_gain_and_mute(gain, muted)
                if hasattr(self, "_queue_adapter") and self._queue_adapter is not None:
                    self._queue_adapter._mirror = self._pc_player.write_frame
        else:
            if hasattr(self, "_queue_adapter") and self._queue_adapter is not None:
                self._queue_adapter._mirror = None
            if self._pc_player is not None:
                self._pc_player.stop()
                self._pc_player = None

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
            targets = self.output_targets_var.get()
            gain = self.volume_gain_var.get()
            muted = self.muted_var.get()

            if self._audio_capture:
                self._audio_capture.set_frame_ms(f_ms)

            if self._udp_sender:
                self._udp_sender.set_codec_and_bitrate(c, br, f_ms)
                send_enabled = (targets != "PC_ONLY")
                self._udp_sender.set_routing_and_gain(send_enabled, gain, muted)

            self._sync_live_pc_player()

        # Transmit to phone if IP is known
        phone_ip = self.ip_var.get().strip()
        if phone_ip:
            send_control_message(phone_ip, cfg, CONTROL_PORT)

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

    def _on_auto_pc_sync_toggled(self) -> None:
        is_auto = self.auto_pc_sync_var.get()
        self.sync_delay_scale.config(state="disabled" if is_auto else "normal")
        if not is_auto:
            self.sync_delay_val_label.config(text=f"{self.pc_sync_delay_var.get()} ms")
        self._on_local_setting_changed()

    def _on_output_target_changed(self) -> None:
        targets = self.output_targets_var.get()
        if targets == "BOTH_SYNCED":
            self.auto_pc_sync_var.set(True)
            self.sync_delay_scale.config(state="disabled")
            self.audio_profile_var.set("QUALITY")
            self._apply_profile_presets("QUALITY")
            self.custom_buffer_var.set(60.0)
            if hasattr(self, "buffer_val_label"):
                self.buffer_val_label.config(text="60.0 ms")
        self._sync_live_pc_player()
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

    def _on_pc_delay_changed(self, val) -> None:
        if self._suppress_sync:
            return
        self.auto_pc_sync_var.set(False)
        delay = int(val)
        self.sync_delay_val_label.config(text=f"{delay} ms")
        if self._pc_player:
            self._pc_player.set_delay_ms(delay)
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
            send_enabled = (self.output_targets_var.get() != "PC_ONLY")
            self._udp_sender.set_routing_and_gain(send_enabled, gain, muted)
        if self._pc_player:
            self._pc_player.set_gain_and_mute(gain, muted)

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
            if "output_targets" in msg:
                tgt = msg["output_targets"]
                self.output_targets_var.set(tgt)
                if tgt == "BOTH_SYNCED":
                    self.auto_pc_sync_var.set(True)
                    if hasattr(self, "sync_delay_scale"):
                        self.sync_delay_scale.config(state="disabled")
            if "pc_sync_delay_ms" in msg:
                d = int(msg["pc_sync_delay_ms"])
                self.pc_sync_delay_var.set(d)
                self.sync_delay_val_label.config(text=f"{d} ms")
                if self._pc_player:
                    self._pc_player.set_delay_ms(d)
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
                targets = self.output_targets_var.get()
                gain = self.volume_gain_var.get()
                muted = self.muted_var.get()

                if self._audio_capture:
                    self._audio_capture.set_frame_ms(f_ms)

                if self._udp_sender:
                    self._udp_sender.set_codec_and_bitrate(c, br, f_ms)
                    send_enabled = (targets != "PC_ONLY")
                    self._udp_sender.set_routing_and_gain(send_enabled, gain, muted)

                self._sync_live_pc_player()

            self._save_config()
            logger.info("Applied incoming CONFIG_SYNC rev %d (sync_id=%s) from phone", incoming_rev, incoming_sync_id)
        finally:
            self._suppress_sync = False

    # ------------------------------------------------------------------
    # Streaming Start / Stop
    # ------------------------------------------------------------------

    def _toggle_streaming(self) -> None:
        if self._streaming:
            self._stop_streaming()
        else:
            self._start_streaming()

    def _start_streaming(self) -> None:
        self._refresh_audio_devices()
        self._refresh_pc_outputs()

        direction = self.direction_var.get()
        target_ip, target_port = self._sanitize_ip_and_port()
        if direction in ("PC_TO_PHONE", "FULL_DUPLEX") and not target_ip:
            messagebox.showwarning("PhoneSpeaker", "Please enter the phone's IP address.")
            return

        codec = self.codec_var.get()
        bitrate = int(self.bitrate_var.get()) * 1000
        frame_ms = int(self.frame_ms_var.get())
        output_targets = self.output_targets_var.get()

        self._save_config()

        # Selected local bind IP
        bind_ip = ""
        adapter_sel = self.adapter_combo.current()
        if 0 <= adapter_sel < len(self._adapter_data):
            bind_ip = self._adapter_data[adapter_sel].get("ip", "")

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
            self._udp_sender.configure(target_ip, target_port, codec, frame_ms, bind_ip=bind_ip)
            send_enabled = (output_targets != "PC_ONLY")
            self._udp_sender.set_routing_and_gain(send_enabled, self.volume_gain_var.get(), self.muted_var.get())
            self._udp_sender.start()

            # Audio capture device (started BEFORE pc_player so active_device_name is known for echo prevention)
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

            # Start PC delayed audio player / PC_ONLY playback now that active capture device is known
            self._sync_live_pc_player()

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
        self.start_btn.config(
            text="■  STOP SPEAKER STREAM", bg="#330000", fg=ERROR_COLOR,
            activebackground="#550000", activeforeground=ERROR_COLOR
        )
        self._update_combined_status()
        logger.info("Streaming started (direction=%s, target=%s:%d)", direction, target_ip, target_port)
        try:
            send_control_message(target_ip, self._get_current_config(), CONTROL_PORT)
        except Exception:
            pass

    def _stop_streaming(self) -> None:
        if self._audio_capture:
            self._audio_capture.stop()
            self._audio_capture = None
        self._queue_adapter = None
        if self._udp_sender:
            self._udp_sender.stop()
            self._udp_sender = None
        if self._pc_player:
            self._pc_player.stop()
            self._pc_player = None
        # Keep self._phone_receiver running so phone mic can connect at any time!
        if self._opus_encoder:
            self._opus_encoder.destroy()
            self._opus_encoder = None

        self._streaming = False
        self._peak_level = 0.0
        self.start_btn.config(
            text="▶  START SPEAKER STREAM (PC → PHONE :5004)", bg="#003300", fg=ACCENT,
            activebackground="#005500", activeforeground=ACCENT
        )
        self.pps_var.set("Speaker Packets: 0/s")
        self._update_combined_status()
        self._level_canvas.coords(self._level_bar, 0, 0, 0, 12)
        logger.info("Streaming stopped")

    def _toggle_mic_receiver(self) -> None:
        """Toggle mute on PC PhoneAudioReceiver (Port 5006) and notify the Phone via MIC_CONTROL."""
        phone_ip = self.ip_var.get().strip()
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

        is_muted = getattr(self._phone_receiver, "_muted", True)
        new_active = is_muted  # First click: is_muted is True -> new_active = True (STARTS mic!)
        self._phone_receiver.set_muted(not new_active, manual=not new_active)
        self._mic_control_cooldown_until = time.monotonic() + 1.5
        self._last_phone_mic_state = new_active

        if not new_active:
            self.rx_pps_var.set("Mic Packets: 0/s")
            self.mic_state_var.set("Phone Mic: OFF")
            self.mic_btn.config(
                text="🎤  START MIC STREAM / RECEIVER (PHONE → PC :5006)",
                bg="#112222", fg="#00EEEE"
            )
            if phone_ip:
                send_control_message(phone_ip, {"type": "MIC_CONTROL", "active": False}, CONTROL_PORT)
        else:
            self.mic_state_var.set("Phone Mic: LISTENING (:5006)")
            self.mic_btn.config(
                text="■  STOP MIC RECEIVER (PHONE → PC :5006 ACTIVE)",
                bg="#003344", fg="#00FF00"
            )
            if phone_ip:
                send_control_message(phone_ip, {"type": "MIC_CONTROL", "active": True}, CONTROL_PORT)
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
                    self.rx_pps_var.set(f"Mic Packets: {data}/s")
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
                    if cur_ip:
                        self.conn_device_ip_var.set(f"{cur_ip}  •  CONNECTED")

                    phone_mic_active = bool(s.get("mic_streaming", False))
                    if time.monotonic() >= getattr(self, "_mic_control_cooldown_until", 0.0):
                        if phone_mic_active != getattr(self, "_last_phone_mic_state", None):
                            self._last_phone_mic_state = phone_mic_active
                            if self._phone_receiver:
                                self._phone_receiver.set_muted(not phone_mic_active, manual=False)
                            if phone_mic_active:
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

                    if self.auto_pc_sync_var.get() and isinstance(s.get("buffer_ms"), (int, float)):
                        net_oneway = float(s.get("_net_oneway_ms", 3.0))
                        phone_buf = float(s.get("buffer_ms", 30.0))
                        live_phone_delay = max(0.0, min(250.0, net_oneway + phone_buf + 14.0 - 20.0))
                        if self._pc_player and self.output_targets_var.get() == "BOTH_SYNCED":
                            smoothed_val = self._pc_player.set_smooth_delay_ms(live_phone_delay)
                        else:
                            smoothed_val = live_phone_delay
                        self._suppress_sync = True
                        try:
                            rounded_delay = int(round(smoothed_val))
                            self.sync_delay_scale.config(state="normal")
                            self.pc_sync_delay_var.set(rounded_delay)
                            self.sync_delay_val_label.config(text=f"Auto ({rounded_delay}ms)")
                            self.sync_delay_scale.config(state="disabled")
                        finally:
                            self._suppress_sync = False

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
        self.output_targets_var.set(self.cfg.get("output_targets", "PHONE_ONLY"))
        d = int(self.cfg.get("pc_sync_delay_ms", 25))
        self.pc_sync_delay_var.set(d)
        self.sync_delay_val_label.config(text=f"{d} ms")
        self.tray_var.set(bool(self.cfg.get("minimize_to_tray", True)))
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
        self.cfg["output_targets"] = self.output_targets_var.get()
        self.cfg["pc_sync_delay_ms"] = self.pc_sync_delay_var.get()
        self.cfg["minimize_to_tray"] = self.tray_var.get()
        self.cfg["audio_output_engine"] = self._phone_audio_engine
        self.cfg["enable_audio_effects"] = self._phone_audio_fx
        self.cfg["exclusive_audio_mode"] = self._phone_exclusive_mode
        self.cfg["stop_on_headset_disconnect"] = self._phone_headset_disc
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
