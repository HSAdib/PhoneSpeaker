# PhoneSpeaker v2.0 Studio

Use your **phone as a wireless speaker for your PC**, and your **phone as a wireless microphone**, over a plain local network. Low-latency UDP audio, a native Android player built on Oboe, and a desktop app for **Windows 11** and **Linux** (PipeWire / PulseAudio).

It started as a fix for a laptop with a dead speaker and a dead headphone jack, and grew into a small studio: two-way settings sync, dual-speaker output, a virtual microphone, and live telemetry on both sides.

> **Not a Bluetooth speaker.** Android phones can't act as a normal Bluetooth audio sink. "Bluetooth" here means **Bluetooth tethering (PAN)**: the PC joins the phone over Bluetooth and the audio travels as ordinary IP packets. Wi-Fi, hotspot and USB tethering work the same way.

---

## Contents

- [Install and build](#install-and-build)
  - [1. Android Installation](#1-android-installation)
  - [2. Windows Installation](#2-windows-installation)
  - [3. Linux Distros Installation](#3-linux-distros-installation)
- [Features](#features)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Using it](#using-it)
- [Suggested settings per connection](#suggested-settings-per-connection)
- [Wire protocol](#wire-protocol)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Limitations and security](#limitations-and-security)
- [Credits](#credits)

---

## Install and build

---

### 1. Android Installation

**Supported Android Versions:**
- **Lowest / Minimum Version:** **Android 8.0 (Oreo, API 26)**
  - *Why Android 8.0?* The low-latency native **AAudio** C-API (which powers the high-performance Oboe audio engine) and modern background foreground notification channels require API 26+. Older Android versions (7.x Nougat and below) are not supported.
- **Supported Range:** **Android 8.0 (API 26)** through **Android 14 & 15+**.
- **Supported Architectures:** Compatible with all **ARM64** (`arm64-v8a`), **ARMv7** (`armeabi-v7a`), **x86_64**, and **x86** devices (smartphones, tablets, Android TV, and emulators).

**Download APK:**
- 📦 **Direct Download:** [**`PhoneSpeaker v2.apk`**](https://github.com/HSAdib/PhoneSpeaker/releases/latest/download/PhoneSpeaker%20v2.apk)
- 🏷️ **GitHub Releases:** [View All Releases](https://github.com/HSAdib/PhoneSpeaker/releases)

**How to Install on Phone:**
- **Direct File Transfer (Simplest):** Send `PhoneSpeaker v2.apk` to your phone via Google Drive, Quick Share, Telegram, or USB cable. Tap the downloaded file and select *Install* (allow *Install unknown apps* for your browser/file manager if prompted).
- **Windows Helper Script:** Run the included PowerShell helper:
  ```powershell
  .\install_apk.ps1
  ```
  *(Automatically detects connected phones over USB or guides you through wireless ADB pairing)*.
- **Wireless ADB / Command Line:**
  ```powershell
  adb pair <phone_ip>:<pairing_port>
  adb connect <phone_ip>:<connect_port>
  adb install -r "PhoneSpeaker v2.apk"
  ```

> **Permissions:** PhoneSpeaker requests **Microphone** (only when streaming phone mic to PC), **Notifications** (to keep foreground streaming active), and a **Battery Optimization Exemption** (strongly recommended so Android doesn't throttle audio playback when the screen is locked).

**Build Android APK from Source:**
Requires Android Studio / SDK 34, NDK, CMake 3.22.1, and JDK 17 (CMake automatically clones libopus v1.5.2):
```powershell
cd PhoneSpeakerAndroid
.\gradlew.bat assembleDebug
```
The APK is generated at `PhoneSpeakerAndroid/app/build/outputs/apk/debug/app-debug.apk` and copied to the root as `PhoneSpeaker v2.apk`.

---

### 2. Windows Installation

**Supported OS:**
- **Windows 11** & **Windows 10** (64-bit).

**Download Windows Binary:**
- 📦 **Direct Download:** [**`PhoneSpeaker v2.exe`**](https://github.com/HSAdib/PhoneSpeaker/releases/latest/download/PhoneSpeaker%20v2.exe)
- 🏷️ **GitHub Releases:** [View All Releases](https://github.com/HSAdib/PhoneSpeaker/releases)

**How to Run:**
- **No installation required!** `PhoneSpeaker v2.exe` is a standalone, single-file executable with bundled 64-bit `opus.dll` and zero external dependencies.
- Simply double-click `PhoneSpeaker v2.exe` to run.
- **Windows Firewall:** Click **Add Firewall Rules** in the app's *Virtual Device* tab, or run in Administrator PowerShell:
  ```powershell
  foreach ($p in 5004,5005,5006) {
    netsh advfirewall firewall add rule name="PhoneSpeaker UDP $p" protocol=UDP localport=$p dir=in action=allow enable=yes profile=any
  }
  ```

**Build Windows EXE from Source:**
```powershell
cd phonespeaker-pc
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python build_exe.py
```
*(Automatically fetches `opus.dll`, builds `PhoneSpeaker v2.exe`, and packages the self-extracting Linux installer)*.

---

### 3. Linux Distros Installation

**Supported Distributions:**
- **Ubuntu** (20.04, 22.04, 24.04+)
- **Linux Mint** (20, 21, 22+)
- **Arch Linux** (including Manjaro, EndeavourOS)
- **Debian** (11 Bullseye, 12 Bookworm, Sid)
- **Fedora** (38, 39, 40+)

Compatible with both **PipeWire** (`pipewire-pulse`) and **PulseAudio**.

#### Method A: One-Line Instant Terminal Install (Easiest)

Open any terminal on your distro and run:

```bash
curl -sSL https://raw.githubusercontent.com/HSAdib/PhoneSpeaker/main/install_linux.sh | bash
```

> **What this one-liner does automatically:**
> 1. Detects your package manager (`apt`, `pacman`, or `dnf`) and ensures dependencies (`python3-tk`, `libopus0`, `pulseaudio-utils`, `portaudio`) are present.
> 2. Downloads the pre-built `PhoneSpeaker v2.0` bundle from GitHub Releases.
> 3. Installs the application to `~/.local/opt/phonespeaker`.
> 4. Adds **PhoneSpeaker v2.0 Studio** with icon to your desktop application menu.
> 5. Adds the `phonespeaker` terminal command and launches the app.

---

#### Method B: Using the Downloaded `.run` Installer

1. Download [**`PhoneSpeaker-v2-Linux-Installer.run`**](https://github.com/HSAdib/PhoneSpeaker/releases/latest/download/PhoneSpeaker-v2-Linux-Installer.run) from [GitHub Releases](https://github.com/HSAdib/PhoneSpeaker/releases).
2. Install per your distro:

##### • Ubuntu & Linux Mint:
```bash
cd ~/Downloads
chmod +x PhoneSpeaker-v2-Linux-Installer.run
./PhoneSpeaker-v2-Linux-Installer.run
```
*(Or GUI: Right-click the `.run` file $\rightarrow$ **Properties** $\rightarrow$ **Permissions** $\rightarrow$ Check **"Allow executing file as program"** $\rightarrow$ Double-click to run).*

##### • Arch Linux (Manjaro / EndeavourOS):
```bash
cd ~/Downloads
chmod +x PhoneSpeaker-v2-Linux-Installer.run
./PhoneSpeaker-v2-Linux-Installer.run
```

##### • Debian:
Ensure base audio tools are installed first:
```bash
sudo apt update && sudo apt install -y pulseaudio-utils libopus0 python3-tk
chmod +x PhoneSpeaker-v2-Linux-Installer.run
./PhoneSpeaker-v2-Linux-Installer.run
```

---

#### Launching on Linux:
- **Application Menu:** Open your desktop app launcher and click **PhoneSpeaker v2.0 Studio**.
- **Terminal:** Type:
  ```bash
  phonespeaker
  ```

#### Uninstalling on Linux:
To completely remove **PhoneSpeaker v2.0 Studio**, its desktop icon, configuration files, and terminal command on any Linux distribution, run:
```bash
pkill -f "phonespeaker-pc/src/main.py" 2>/dev/null || true
rm -rf ~/.local/opt/phonespeaker ~/.local/bin/phonespeaker ~/.local/share/applications/phonespeaker.desktop ~/.local/share/icons/phonespeaker.png ~/.config/PhoneSpeaker
sudo rm -f /usr/local/bin/phonespeaker 2>/dev/null || true
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```
(Or run `curl -sSL https://raw.githubusercontent.com/HSAdib/PhoneSpeaker/main/install_linux.sh | bash -s -- --uninstall`).

#### Linux Firewall (If Streaming is Blocked):
- **Ubuntu / Mint / Debian (`ufw`):**
  ```bash
  sudo ufw allow 5004/udp && sudo ufw allow 5005/udp && sudo ufw allow 5006/udp
  ```
- **Arch / Fedora (`firewalld`):**
  ```bash
  sudo firewall-cmd --add-port={5004,5005,5006}/udp --permanent && sudo firewall-cmd --reload
  ```

#### Build Linux App from Source:
```bash
python3 phonespeaker-pc/build_linux.py     # builds dist/PhoneSpeaker-v2 and the .run installer
python3 phonespeaker-pc/src/main.py        # run directly from source
```

### Tests (PC)

```bash
python phonespeaker-pc/tests/verify_pc.py
```

Runs nine checks: adapter filtering, PCM packet size, the Opus encoder, silence pacing, audio capture opening, sample-format conversion, a UDP localhost integration test, protocol v2 settings sync, and system-volume access. Capture tests need a working audio device, so run them on a machine with sound.

### Firewall

The PC must accept inbound UDP **5004, 5005 and 5006**. Use the **Add Firewall Rules** button in the app (**Virtual Device** tab), or run the commands yourself.

Windows (administrator PowerShell). Phone hotspots are usually classed as *Public* networks, so the rules use `profile=any`:

```powershell
foreach ($p in 5004,5005,5006) {
  netsh advfirewall firewall add rule name="PhoneSpeaker UDP $p" protocol=UDP localport=$p dir=in action=allow enable=yes profile=any
}
```

Linux (use whichever firewall you have):

```bash
sudo ufw allow 5004/udp && sudo ufw allow 5005/udp && sudo ufw allow 5006/udp
# or
sudo firewall-cmd --add-port=5004/udp --add-port=5005/udp --add-port=5006/udp --permanent && sudo firewall-cmd --reload
```

---

## Features

**Streaming**
- **PC → Phone speaker:** captures everything your PC plays and plays it on the phone.
- **Phone → PC microphone:** streams the phone mic to the PC. Route it into Discord, Zoom, Teams or OBS through a virtual device.
- **Full-duplex:** speaker and microphone at the same time.
- **Dual-speaker sync:** play on the phone and on a PC output device together (`Both Phone + PC Together (Auto-Synced)`), with 100% automatic delay calibration and optional manual fine-tuning (0 to 150 ms) for drift-free, echo-free dual listening.
- **PC-only routing:** mirror to a PC output device without sending to the phone (`PC Speaker Only (Mute Phone)`).

**Audio quality & Sound Modes**
- **4 Sound Modes:**
  - **Best Quality (QUALITY):** Opus 256 kbps, 10 ms frame, High 80 ms buffer (60 ms when Both Phone + PC Synced), Studio Complexity 10 & Constrained VBR, crystal-clear music and movies.
  - **Gaming Low-Delay (GAMING):** Opus 128 kbps, 5 ms frame, Low 20 ms buffer, ultra-low latency for competitive gaming and realtime video.
  - **Balanced Auto (BALANCED):** Opus 192 kbps, 5 ms frame, Auto Adaptive buffer (30 ms base), dynamically adjusts jitter buffer to match network conditions.
  - **Custom (CUSTOM):** Freely customize codec (Opus 10–320 kbps or Lossless PCM16 1536 kbps), frame size (5, 10, 20 ms), and buffer target (10–200 ms).
- **Lossless PCM16** (1536 kbps, 48 kHz stereo) or **Opus** with selectable bitrates up to **320 kbps** (10, 24, 32, 64, 96, 128, 160, 192, 256, 320 kbps) with Studio Complexity 10 & Constrained VBR, and 5, 10, or 20 ms frames.
- Any Windows or Linux playback format (float32, int16, int24, int32; 44.1, 48, 96 kHz) is converted to 48 kHz stereo, with stateful `soxr.ResampleStream` (HQ) eliminating all sample-boundary clicks and crackles.
- Silence keep-alive keeps the phone's clock running when nothing is playing.

**Playout and robustness (Android)**
- Native **Oboe** output (low-latency, exclusive mode when the device allows) with a standard shared mode for compatibility.
- Adaptive **jitter buffer** with packet reordering, late-packet dropping and gap concealment. Presets: Low 20 ms, Medium 40 ms, High 80 ms, Auto (+5 ms on underrun, -2.5 ms when clean), or a custom 10 to 200 ms target.
- Opus in-band FEC and packet-loss concealment.
- Foreground service with wake lock and Wi-Fi low-latency lock, so audio keeps playing with the screen off.
- Relative volume scaling (0% to 200% with soft-knee limiting, where phone hardware volume = 100% baseline), mute, optional Loudness Enhancer, and audio-focus behaviour: mix with others, pause when another app plays, or always play.

**Control and sync**
- **Two-way settings sync:** change codec, bitrate, buffer, gain, mute, routing or stream mode on either device and the other follows. A revision counter decides which change is newer.
- **Auto-discovery:** the phone broadcasts a beacon, and the PC fills in the phone's IP, preferring an address on the same subnet as your chosen adapter.
- **Live telemetry:** buffer level, target, underruns, lost and late packets, packets per second, and a live delay graph on both sides.
- **System volume sync:** optionally follows the Windows sound bar or Linux default sink volume.

**Desktop app**
- Black-and-white Tk interface with three tabs: Stream / Roles, Audio & Buffer, Virtual Device.
- Virtual-adapter filtering (VirtualBox `192.168.56.x`, VMware, Hyper-V, WSL and similar) so you always see your real LAN IP.
- One-click firewall rules, system-tray mode, built-in help dialog.
- High process priority and 1 ms timer resolution on Windows.

---

## How it works

```
 PC (Windows / Linux)                                   Phone (Android)
 ┌────────────────────────────┐   UDP 5004 audio        ┌──────────────────────────┐
 │ WASAPI loopback / parec    │ ──────────────────────▶ │ jitter buffer → Oboe     │
 │ resample → PCM16 / Opus    │                         │ (speaker)                │
 │                            │   UDP 5006 audio        │                          │
 │ decode → output device /   │ ◀────────────────────── │ AudioRecord (mic) →      │
 │ virtual mic                │                         │ UDP sender               │
 │                            │   UDP 5005 control JSON │                          │
 │ settings, discovery, stats │ ◀─────────────────────▶ │ beacon, stats, settings  │
 └────────────────────────────┘                         └──────────────────────────┘
```

Every connection type (Wi-Fi router, phone hotspot, USB tethering, Bluetooth PAN) is just an IP network, so the same three UDP ports serve all of them.

---

## Quick start

1. Put the PC and the phone on the **same network**: same Wi-Fi, the phone's hotspot, or a USB or Bluetooth tether.
2. Install the Android app and the desktop app (see [Install and build](#install-and-build)).
3. On the phone, open **PhoneSpeaker** and tap **Start speaker player (port 5004)**.
4. On the PC, open **PhoneSpeaker**. The phone should appear under **Connections** within a second or two. If it doesn't, type the phone's IP into **Phone IP / Port**.
5. Click **Start speaker stream (PC → Phone)**. Play something on the PC.
6. Watch the **Audio Level** bar on the PC and the **RX packets / sec** counter on the phone. Both should move.

For the microphone: tap **Start mic stream to PC (port 5006)** on the phone, or use the mic button on the PC. See [Phone as a PC microphone](#phone-as-a-pc-microphone).

---

## Using it

### Stream modes

| Mode | What it does | Ports |
|---|---|---|
| **PC → Phone** | Phone is the speaker | 5004 |
| **Phone → PC Mic** | Phone is the microphone | 5006 |
| **Full-Duplex** | Both at once | 5004 and 5006 |

Speaker and mic have separate start and stop buttons on both the PC and the phone. Starting the mic from the PC also signals the phone to start its microphone server.

### Output targets

| Target | Behaviour |
|---|---|
| **Phone Speaker Only** | Audio goes to the phone only |
| **Both Phone + PC Together (Auto-Synced)** | Phone and PC speaker play together; automatic calibration calculates phone-to-PC delay, or fine-tune with **PC Sync Delay** |
| **PC Speaker Only (Mute Phone)** | Audio is mirrored to a PC output device and muted on the phone |

If you capture a device and play back to the **same** device, you create an echo loop. Capture a virtual device and play to your real speakers or headphones instead.

### Phone as a PC microphone

The phone sends 48 kHz audio to the PC on port 5006. The PC plays it into an output device, and you point your chat app at the matching input.

**Windows**
1. In the **Virtual Device** tab, click **Install Virtual Audio Driver (VB-CABLE)**. This downloads the VB-CABLE driver pack from vb-audio.com and runs its installer with elevation.
2. In the **Stream** tab, set **PC Playback Dev** to **CABLE Input**.
3. In Discord, Zoom, OBS or your game, choose **CABLE Output** as the microphone.

**Linux**
1. In the **Virtual Device** tab, click **Install Virtual Audio Driver**. This uses `pactl` to create a virtual speaker (`PhoneSpeaker_Sink`), a mic bridge (`PhoneSpeaker_Mic_Sink`) and a virtual microphone (`PhoneSpeaker_Mic`, shown as *PhoneSpeaker_Wireless_Mic*).
2. Set **PC Playback Dev** to `[PipeWire/Pulse] PhoneSpeaker_Mic_Sink`.
3. Choose **PhoneSpeaker_Wireless_Mic** as the microphone in your app.

The virtual-sink devices are created per login session. If they disappear after a reboot, click the button again.

### Capturing only part of your audio

Set the capture source in the **Virtual Device** tab. On Windows, making a virtual device your Windows output (`Win + Ctrl + V` opens the output switcher) lets you stream everything without also playing it on the PC. The app's *Rename to PhoneSpeaker Audio* button gives the virtual device a friendlier name.

### Files and logs

| | Windows | Linux |
|---|---|---|
| Config | `%APPDATA%\PhoneSpeaker\config.json` | `~/.config/PhoneSpeaker/config.json` |
| Log | `%APPDATA%\PhoneSpeaker\phonespeaker.log` | `~/.config/PhoneSpeaker/phonespeaker.log` |

---

## Suggested settings per connection

Starting points, not guarantees. Tune the buffer on the phone from what the telemetry shows.

| Connection | Codec | Frame | Phone buffer | Notes |
|---|---|---|---|---|
| **Phone hotspot (5 GHz)** | PCM16 or Opus 320k | 5 ms | 20 to 30 ms | PC joins the phone's access point directly. Usually the best wireless latency. |
| **Wi-Fi router (5 GHz)** | Opus 192k / 320k or PCM16 | 5 ms | 30 to 40 ms | Fine on a clean home network. 2.4 GHz is much more prone to dropouts. |
| **USB tethering** | PCM16 | 5 ms | 15 to 25 ms | No radio jitter, so the lowest and steadiest delay. |
| **Bluetooth PAN** | Opus 128k | 10 ms | 60 to 80 ms | Highest jitter and least bandwidth of the four. |

Low delay and zero dropouts pull against each other, and the jitter buffer is the trade-off. The phone's own audio hardware adds roughly 10 to 40 ms depending on the model, and no setting removes that.

---

## Wire protocol

Little-endian, UDP.

| Port | Direction | Purpose |
|---|---|---|
| **5004** | PC → Phone | Speaker audio |
| **5005** | Both | Control JSON: `BEACON`, `STATS`, `CONFIG_SYNC` |
| **5006** | Phone → PC | Microphone audio |

**Audio packet** = 12-byte header + payload (kept under 1200 bytes to avoid fragmentation):

| Field | Type | Meaning |
|---|---|---|
| `seq` | uint32 | Increments per packet |
| `timestamp_ms` | uint32 | Sender monotonic clock, milliseconds |
| `codec` | uint8 | `0` = PCM16, `1` = Opus |
| `channels` | uint8 | `2` (stereo) |
| `sample_rate_div100` | uint16 | `480` = 48 kHz |

PCM16 uses 5 ms frames: 240 samples per channel, a 960-byte payload, 972 bytes on the wire. Opus sends one packet per datagram.

**Control messages (port 5005, JSON)**
- `BEACON`: the phone broadcasts its IP addresses and ports about once a second so the PC can find it.
- `STATS`: the phone reports buffer level, target, underruns, lost and late packets and packets per second about twice a second.
- `CONFIG_SYNC`: the full shared settings. Whenever a setting changes, the changing side bumps a revision counter and sends the whole object. The receiver applies it only if the revision is newer, without echoing it back.

```json
{
  "type": "CONFIG_SYNC",
  "rev": 12,
  "direction": "PC_TO_PHONE",
  "output_targets": "PHONE_ONLY",
  "pc_sync_delay_ms": 25,
  "codec": 1,
  "opus_bitrate_kbps": 128,
  "frame_ms": 5,
  "buffer_mode": "AUTO",
  "custom_buffer_ms": 30.0,
  "auto_buffer": true,
  "volume_gain": 1.0,
  "muted": false,
  "audio_output_engine": "OBOE_LOW_LATENCY",
  "enable_audio_effects": false,
  "exclusive_audio_mode": "MIX_WITH_OTHERS",
  "stop_on_headset_disconnect": false
}
```

`direction` is `PC_TO_PHONE`, `PHONE_TO_PC` or `FULL_DUPLEX`. `output_targets` is `PHONE_ONLY`, `PC_ONLY` or `BOTH_SYNCED`. `buffer_mode` is `LOW`, `MEDIUM`, `HIGH`, `AUTO` or `CUSTOM`. The authoritative definitions are in `phonespeaker-pc/src/protocol.py` and `PhoneSpeakerAndroid/app/src/main/cpp/protocol.h`.

---

## Project layout

```
.
├── install_linux.sh                 # Linux: system deps + build + install
├── install_apk.ps1                  # Windows: adb install helper for the APK
├── phonespeaker-pc/
│   ├── build_exe.py                 # Windows .exe (PyInstaller) + Linux .run bundle
│   ├── build_linux.py               # Linux binary + .run installer
│   ├── src/
│   │   ├── main.py                  # entry point
│   │   ├── ui.py                    # Tk interface, sync engine, tray, help
│   │   ├── audio_capture.py         # WASAPI loopback / parec capture, resampling
│   │   ├── audio_output.py          # PC playback, dual-speaker delay, phone-mic receiver
│   │   ├── network.py               # UDP sender, control listener, adapter filtering
│   │   ├── protocol.py              # header, ports, default config
│   │   ├── opus_codec.py            # ctypes libopus encoder/decoder
│   │   ├── virtual_device.py        # VB-CABLE (Windows), pactl virtual devices (Linux)
│   │   ├── win_volume.py            # system volume sync (CoreAudio / pactl)
│   │   ├── firewall.py              # netsh / ufw / firewalld / iptables
│   │   └── config.py                # per-OS config and logging
│   ├── tests/                       # verify_pc.py, capture_smoke_test.py
│   └── vendor/get_opus.py           # fetches opus.dll for Windows builds
└── PhoneSpeakerAndroid/
    └── app/src/main/
        ├── cpp/                     # Oboe engine, jitter buffer, Opus wrapper, JNI
        ├── java/com/hasan/phonespeaker/
        │   ├── MainActivity.kt      # Player / Server / Settings tabs
        │   ├── AudioStreamService.kt# foreground service, locks, audio focus
        │   ├── ControlChannel.kt    # beacon, stats, settings sync
        │   ├── MicStreamer.kt       # phone microphone to PC
        │   ├── LatencyGraphView.kt  # live delay graph
        │   └── AudioNative.kt       # JNI bindings
        └── res/                     # black-and-white layouts and theme
```

---

## Troubleshooting

**The phone can't connect or the PC shows no phone.**
Check the IP the PC is using. If you run VirtualBox, VMware or similar, a virtual adapter such as `192.168.56.1` can look like your real address. The app hides those, but if you type an IP by hand, use the one from `ipconfig` (Windows) or `ip addr` (Linux) for your real Wi-Fi or Ethernet adapter, and the phone's IP from the phone's app. A phone hotspot usually gives the phone an address like `10.x.x.x` or `192.168.43.x`, and that address can change between sessions.

**Discovery works but there's no sound.**
- Windows classes hotspots as *Public*. Make sure the firewall rules use `profile=any`.
- Look at the PC **Audio Level** bar while playing something. If it stays flat, the app is capturing a different output device than the one Windows is playing to. Pick the right one in **PC Capture Source**, or leave it on the Windows default.
- If the PC sends but the phone's **RX packets / sec** stays at 0, it's the network: wrong IP, firewall, or a hotspot with client isolation turned on.
- If the phone receives packets and the buffer looks healthy but you hear nothing, check that the phone isn't muted in the app and that **Exclusive Audio** isn't set to pause when another app plays.

**Crackling or dropouts.**
Raise the phone's target buffer or switch to **Auto**. Prefer 5 GHz Wi-Fi, the phone hotspot, or USB tethering. Disable battery optimization for PhoneSpeaker so Android doesn't throttle it.

**Echo or feedback.**
You're capturing and playing the same device. Capture a virtual device and play to a different one.

**Linux: no devices or no capture.**
Install `pulseaudio-utils` (or `pipewire-pulse`) so `pactl`, `parec` and `pacat` exist, and make sure PipeWire or PulseAudio is running.

**Running two versions.**
Don't run the v1 and v2 phone apps at the same time. Both listen on UDP 5004 and 5005.

---

## Limitations and security

- There is **no authentication or encryption**. Anyone on the same network who can reach the ports can send audio or settings to either device. Use it on networks you trust, not on public Wi-Fi.
- Latency depends on your network and your phone's audio hardware. Expect tens of milliseconds on a clean 5 GHz link or USB tether, and more over Bluetooth tethering.
- The desktop app is built for Windows 11 and Linux with PipeWire or PulseAudio. macOS isn't supported.
- Windows virtual-device installation uses the third-party VB-CABLE driver, downloaded from vb-audio.com.

---

## Credits

- [Oboe](https://github.com/google/oboe): low-latency Android audio
- [Opus](https://opus-codec.org/) (libopus): audio codec
- [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch): WASAPI loopback capture on Windows
- [soxr](https://github.com/dofuuz/python-soxr), [NumPy](https://numpy.org/), [psutil](https://github.com/giampaolo/psutil), [Pillow](https://python-pillow.org/), [pystray](https://github.com/moses-palmer/pystray)
- [VB-CABLE](https://vb-audio.com/Cable/) by VB-Audio: virtual audio device on Windows
