# PhoneSpeaker v2.0 Studio

PhoneSpeaker turns an Android phone into a wireless speaker for a Windows or Linux PC, and can also send the phone's microphone audio to the PC. Audio travels over a local IP network using UDP; supported connections include Wi-Fi, a phone hotspot, USB tethering, and Bluetooth tethering (PAN). It is not a Bluetooth audio sink.

## What it does

- Streams PC audio to an Android phone, phone microphone audio to a PC, or both at once.
- Plays audio on the phone and a PC output device together, with an adjustable sync delay.
- Supports lossless PCM16 and Opus, with configurable bitrate, frame size, and phone-side jitter buffering.
- Synchronizes settings between the PC and phone and displays live audio and network telemetry.
- Provides a virtual microphone route for chat, meeting, and recording apps.

## Downloads and installation

Get the latest installers from the [GitHub Releases page](https://github.com/HSAdib/PhoneSpeaker/releases):

- **Android:** `PhoneSpeaker v2.apk` (Android 8.0 / API 26 or newer).
- **Windows:** `PhoneSpeaker v2.exe` (Windows 10 or 11, 64-bit; standalone).
- **Linux:** `PhoneSpeaker-v2-Linux-Installer.run`, or use the install script below. Ubuntu, Linux Mint, Arch, Debian, and Fedora are listed as supported; PipeWire and PulseAudio are supported.

For a quick Linux installation, run:

```bash
curl -sSL https://raw.githubusercontent.com/HSAdib/PhoneSpeaker/main/install_linux.sh | bash
```

On Windows, the included `install_apk.ps1` helper can install the Android app to a connected phone. See [README.md](README.md) for detailed installation instructions, source builds, and platform-specific commands.

## Quick start

1. Connect the PC and phone to the same network.
2. Install and open PhoneSpeaker on both devices.
3. On the phone, start the speaker player.
4. On the PC, select the discovered phone (or enter its IP address) and start the PC-to-phone speaker stream.
5. To use the phone as a microphone, start the phone mic stream and route the PC playback to a virtual audio device. Choose that device's corresponding input as the microphone in your communication app.

## Network and audio

The PC must allow inbound UDP traffic on these ports:

| Port | Purpose |
|---|---|
| 5004 | PC-to-phone speaker audio |
| 5005 | Control, discovery, telemetry, and settings |
| 5006 | Phone-to-PC microphone audio |

Use the app's **Add Firewall Rules** control on Windows, or allow these UDP ports in your Linux firewall. For microphone routing, the Windows app uses VB-CABLE; Linux uses virtual devices created with `pactl`.

For a low-latency starting point, try PCM16 with 5 ms frames on USB tethering or a clean 5 GHz Wi-Fi connection. Increase the phone's jitter buffer if you hear dropouts; Bluetooth PAN typically needs a larger buffer.

## Troubleshooting and limitations

- If the phone is not discovered, verify both devices are on the same network, use the phone's current IP, and check firewall rules and hotspot client isolation.
- If packets arrive but audio does not play, check the selected capture/output device, phone mute state, and audio-focus settings.
- For crackling, increase the buffer, try a more stable connection, and exempt PhoneSpeaker from Android battery optimization.
- Avoid capturing and playing back through the same device, which can create an echo loop.
- PhoneSpeaker has **no authentication or encryption**. Use it only on trusted networks; devices that can reach the ports may send audio or settings.
- macOS is not supported. Do not run v1 and v2 phone apps simultaneously because they share ports.

For full feature details, the wire protocol, project layout, build instructions, and extended troubleshooting, see the [main README](README.md).
