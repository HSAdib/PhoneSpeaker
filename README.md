# PhoneSpeaker 🔊

Low-latency real-time system audio streaming from a Windows 11 PC to an Android phone over Wi-Fi, Phone Hotspot, USB Tethering, or Bluetooth PAN.

PhoneSpeaker captures the Windows WASAPI loopback output (ideal for laptops with broken speakers or headphone jacks) and transmits low-latency audio via UDP to your Android device with zero-dropout packet loss concealment.

---

## Architecture & Shared Wire Protocol (v1.0)

- **Audio Data Plane (UDP Port 5004):**
  - PC unicasts 12-byte fixed header + variable audio payload (strictly < 1200 bytes to avoid MTU fragmentation).
  - **12-Byte Header:** `<IIBBH` (seq, monotonic timestamp ms, codec [0=PCM16, 1=Opus], channels [2=stereo], sample_rate/100 [480=48kHz]).
  - **PCM16 (Codec 0):** 5 ms frames (240 samples/ch) = 960 bytes payload + 12-byte header = **972 bytes**.
  - **Opus (Codec 1):** 5 ms / 10 ms encoded frames with In-Band FEC enabled at 128 / 160 / 192 kbps.
  - **Silence Keep-Alive:** When Windows stops playing audio, the PC automatically generates zeroed frames at the exact 5 ms cadence so the phone's jitter buffer clock never stalls.
- **Control & Telemetry Back-Channel (UDP Port 5005):**
  - **BEACON:** Phone broadcasts discovery JSON every 1000 ms to auto-populate the phone's IP in the PC app.
  - **STATS:** Phone sends unicast live telemetry JSON every 500 ms back to the PC (buffer level, target buffer, loss, late packets, pps).

---

## 1. PC Application (`phonespeaker-pc/`)

### Running the Pre-Compiled Standalone App
You can immediately launch the windowed executable from the root folder:
```
.\PhoneSpeaker.exe
```
*(Also available in `phonespeaker-pc\dist\PhoneSpeaker.exe`; no console window, bundled 64-bit `opus.dll`, and custom monochrome speaker icon)*.

### Windows Firewall Configuration
Windows classifies phone Wi-Fi hotspots as **Public networks** by default. To allow incoming discovery (UDP 5005) and audio datagrams (UDP 5004), click **🔥 Add Firewall Rules** in the UI, or run PowerShell as Administrator:
```powershell
netsh advfirewall firewall add rule name="PhoneSpeaker UDP 5004" protocol=UDP localport=5004 dir=in action=allow enable=yes profile=any
netsh advfirewall firewall add rule name="PhoneSpeaker UDP 5005" protocol=UDP localport=5005 dir=in action=allow enable=yes profile=any
```

### Running from Source / Rebuilding
```powershell
cd phonespeaker-pc
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python src\main.py
```
To run acceptance verification tests:
```powershell
.\.venv\Scripts\python tests\verify_pc.py
```
To rebuild the standalone executable:
```powershell
.\.venv\Scripts\python build_exe.py
```

---

## 2. Android Application (`PhoneSpeakerAndroid/`)

### Installing `PhoneSpeaker.apk`

The compiled APK is located in the root folder:
```
.\PhoneSpeaker.apk
```
*(Also available in `PhoneSpeakerAndroid\app\build\outputs\apk\debug\app-debug.apk`)*.

#### Method A: One-Click PowerShell Script (Recommended)
Simply run:
```powershell
.\install_apk.ps1
```
*(Automatically locates your Android SDK `adb.exe`, checks device connection, pairs/connects over Wireless ADB if needed, installs the APK, and launches the app!)*

#### Method B: Manual Wireless ADB
1. On your Android phone, enable **Developer options** -> enable **Wireless debugging**.
2. Tap **Wireless debugging** -> **Pair device with pairing code** (take note of the IP, port, and 6-digit code).
3. In PowerShell on your PC:
   ```powershell
   adb pair <phone_ip>:<pairing_port>
   # Enter the 6-digit code
   adb connect <phone_ip>:<connect_port>
   adb install -r "c:\Users\hasan\PhoneSpeaker\PhoneSpeaker.apk"
   ```

#### Method C: Local File Transfer
Send `PhoneSpeaker.apk` to your phone via Google Drive, Nearby Share / Quick Share, or USB and install it.

---

## 3. Best Settings per Connection Type

| Connection Mode | Recommended Codec | Frame Size | Phone Target Buffer | Notes |
|---|---|---|---|---|
| **Phone Hotspot (5 GHz)** | PCM16 (Lossless) | 5 ms | 20 – 30 ms | Lowest latency (<35 ms total). PC connects directly to phone AP. |
| **Wi-Fi Router (2.4/5 GHz)** | PCM16 or Opus 192k | 5 ms | 30 – 40 ms | Reliable on clean home Wi-Fi networks. |
| **USB Tethering (RNDIS/NCM)** | PCM16 (Lossless) | 5 ms | 15 – 25 ms | Zero radio interference, ultra-low latency. |
| **Bluetooth PAN Tethering** | Opus (128 kbps) | 10 ms | 60 – 80 ms | Bluetooth PAN has higher jitter. 10 ms frames reduce overhead. Auto-tuned in UI! |

---

## 4. Features & Controls

- **Mono/Stereo & Sample Rate Agnostic:** Automatically converts and resamples Windows playback devices (float32, int16, int24, int32, 44.1 kHz, 48 kHz, 96 kHz) to 48 kHz stereo.
- **Smart Network Adapter Filtering:** Automatically hides virtual adapters (`192.168.56.x` VirtualBox, VMware, Hyper-V, WSL) so you always see your real IP.
- **Adaptive Jitter Buffer & Gap Concealment:**
  - Auto-adjusts target latency (+5 ms on underrun, decays -2.5 ms during clean streaming).
  - PCM16 exponential fade-out ($0.7^n$) on dropped frames; Opus In-Band FEC and native PLC.
- **Background Streaming & System Tray:**
  - On PC: Closes to the system tray (`Open PhoneSpeaker`, `Start / Stop Stream`, `Exit`).
  - On Android: Runs inside an ongoing `mediaPlayback` Foreground Service with `PARTIAL_WAKE_LOCK` and `WIFI_MODE_FULL_LOW_LATENCY` so streaming continues with the screen turned off.
- **Audio Focus & Headphone Handling:** Automatically ducks/pauses during incoming phone calls and recovers playback seamlessly when headphones are plugged/unplugged (`onErrorAfterClose`).

