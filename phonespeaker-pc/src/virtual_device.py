"""
virtual_device.py — Cross-platform virtual audio device detection,
OS sound settings launchers, fast registry friendly-name tool, and
VB-CABLE / PipeWire virtual driver installer for PhoneSpeaker v2.0.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_IS_LINUX = sys.platform.startswith("linux")
_IS_WIN = sys.platform.startswith("win")
_IS_MAC = (sys.platform == "darwin")
VBCABLE_URL = "https://download.vb-audio.com/Download_DIR/VBCABLE_Driver_Pack43.zip"

_VIRTUAL_KEYWORDS = [
    "virtual speaker",
    "virtual audio",
    "cable input",
    "cable output",
    "vb-audio",
    "virtual cable",
    "phonespeaker",
    "audiorelay",
    "voicemeeter",
    "blackhole",
    "soundflower",
    "loopback",
]


def setup_macos_virtual_audio() -> Tuple[bool, str]:
    """
    Install BlackHole 2ch on macOS via Homebrew (if brew is present)
    or download and open the official BlackHole pkg installer.
    """
    if shutil.which("brew"):
        try:
            res = subprocess.run(["brew", "install", "--cask", "blackhole-2ch"], capture_output=True, text=True, timeout=120)
            if res.returncode == 0:
                return True, "Installed BlackHole 2ch virtual audio driver via Homebrew!\nClick [ ↻ Refresh ] in PhoneSpeaker."
        except Exception:
            pass

    pkg_url = "https://existential.audio/downloads/BlackHole2ch-0.6.0.pkg"
    pkg_path = Path("/tmp/BlackHole2ch.pkg")
    try:
        req = urllib.request.Request(pkg_url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) PhoneSpeaker/2.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            pkg_path.write_bytes(resp.read())
        subprocess.Popen(["open", str(pkg_path)])
        return True, "Downloaded BlackHole 2ch installer!\nPlease complete the installer window, then click [ ↻ Refresh ] in PhoneSpeaker."
    except Exception as exc:
        return False, f"Could not download BlackHole 2ch: {exc}\nYou can install manually with: brew install --cask blackhole-2ch"


def setup_linux_virtual_audio() -> Tuple[bool, str]:
    """
    Create PipeWire / PulseAudio virtual devices on Linux via pactl:
      1. PhoneSpeaker_Sink (Virtual Speaker for isolated PC -> Phone streaming)
      2. PhoneSpeaker_Mic_Sink + PhoneSpeaker_Mic (Virtual Microphone for Phone -> PC mic in Discord/Zoom)
    """
    if not shutil.which("pactl"):
        return False, "pactl not found. Please install 'pulseaudio-utils' or 'pipewire-pulse'."

    try:
        existing = subprocess.run(
            ["pactl", "list", "short", "modules"], capture_output=True, text=True, timeout=3
        ).stdout

        if "PhoneSpeaker_Sink" not in existing:
            subprocess.run([
                "pactl", "load-module", "module-null-sink",
                "sink_name=PhoneSpeaker_Sink",
                "sink_properties=device.description=PhoneSpeaker_Virtual_Speaker"
            ], check=True, timeout=3)

        if "PhoneSpeaker_Mic_Sink" not in existing:
            subprocess.run([
                "pactl", "load-module", "module-null-sink",
                "sink_name=PhoneSpeaker_Mic_Sink",
                "sink_properties=device.description=PhoneSpeaker_Mic_Bridge"
            ], check=True, timeout=3)

        if "PhoneSpeaker_Mic" not in existing:
            subprocess.run([
                "pactl", "load-module", "module-remap-source",
                "master=PhoneSpeaker_Mic_Sink.monitor",
                "source_name=PhoneSpeaker_Mic",
                "source_properties=device.description=PhoneSpeaker_Wireless_Mic"
            ], check=True, timeout=3)

        msg = (
            "Linux PipeWire/PulseAudio virtual devices are active!\n\n"
            "• Virtual Speaker: 'PhoneSpeaker_Sink'\n"
            "• Wireless Mic for Discord/Zoom/OBS: 'PhoneSpeaker_Mic'\n"
            "• Phone Mic Output automatically routed to '[PipeWire/Pulse] PhoneSpeaker_Mic_Sink'."
        )
        return True, msg
    except Exception as exc:
        return False, f"Failed to create PipeWire/PulseAudio virtual devices: {exc}"


def remove_linux_virtual_audio() -> Tuple[bool, str]:
    """Unload PhoneSpeaker virtual sinks/sources on Linux."""
    if not _IS_LINUX or not shutil.which("pactl"):
        return False, "pactl not available."
    try:
        res = subprocess.run(
            ["pactl", "list", "short", "modules"], capture_output=True, text=True, timeout=3
        )
        removed = 0
        for line in res.stdout.splitlines():
            if "PhoneSpeaker_" in line:
                mod_id = line.split("\t")[0].strip()
                if mod_id.isdigit():
                    subprocess.run(["pactl", "unload-module", mod_id], check=False, timeout=2)
                    removed += 1
        return True, f"Removed {removed} PhoneSpeaker virtual audio module(s)."
    except Exception as exc:
        return False, f"Error removing virtual modules: {exc}"


def detect_virtual_devices() -> List[Dict]:
    """
    Scan audio devices for virtual playback and recording endpoints (e.g. CABLE Input, CABLE Output,
    PhoneSpeaker Virtual Audio, Voicemeeter, PipeWire/PulseAudio virtual sinks).
    Returns list of dicts: {index, name, is_virtual, is_default, is_input, is_output}
    """
    results: List[Dict] = []
    seen_names = set()

    if _IS_LINUX and shutil.which("pactl"):
        try:
            res = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        name = parts[1].strip()
                        is_virt = any(k in name.lower() for k in ("phonespeaker", "null", "virtual", "cable"))
                        results.append({
                            "index": f"pulse:{name}",
                            "name": f"[PipeWire/Pulse] {name}",
                            "is_virtual": is_virt,
                            "is_default": False,
                            "is_output": True,
                            "is_input": False,
                        })
                        seen_names.add(name.lower())
        except Exception:
            pass

    try:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            import pyaudio

        pa = pyaudio.PyAudio()
        try:
            default_out_idx = None
            try:
                def_info = pa.get_default_output_device_info()
                default_out_idx = def_info.get("index")
            except Exception:
                pass

            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                    if info.get("isLoopbackDevice", False):
                        continue
                    max_out = int(info.get("maxOutputChannels", 0))
                    max_in = int(info.get("maxInputChannels", 0))
                    if max_out <= 0 and max_in <= 0:
                        continue
                    name = info.get("name", "").strip()
                    name_lower = name.lower()
                    if not name or name_lower in seen_names:
                        continue
                    is_virt = any(kw in name_lower for kw in _VIRTUAL_KEYWORDS)
                    if is_virt:
                        seen_names.add(name_lower)
                    results.append({
                        "index": i,
                        "name": name,
                        "is_virtual": is_virt,
                        "is_default": (i == default_out_idx),
                        "is_output": max_out > 0,
                        "is_input": max_in > 0,
                    })
                except Exception:
                    continue
        finally:
            pa.terminate()
    except Exception as exc:
        logger.error("Error detecting virtual devices: %s", exc)

    return results


def find_preferred_virtual_device() -> Optional[Dict]:
    """Return the first detected virtual output device (e.g. CABLE Input), if any."""
    devs = detect_virtual_devices()
    for d in devs:
        if d.get("is_virtual") and d.get("is_output", True):
            return d
    return None


def open_windows_sound_settings() -> bool:
    """Open OS Sound Settings cleanly without flashing a console window."""
    if _IS_MAC:
        try:
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.Sound-Settings.extension"])
            return True
        except Exception:
            try:
                subprocess.Popen(["open", "-a", "System Settings"])
                return True
            except Exception:
                return False

    if _IS_LINUX:
        for cmd in [["pavucontrol"], ["gnome-control-center", "sound"], ["kcmshell6", "kcm_pulseaudio"]]:
            if shutil.which(cmd[0]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
                except Exception:
                    continue
        return False

    try:
        os.startfile("ms-settings:sound")
        return True
    except Exception as exc:
        logger.error("Could not open ms-settings:sound: %s", exc)
        return False


def open_app_volume_settings() -> bool:
    """Open Windows Per-App Volume & Device Preferences (ms-settings:apps-volume) or pavucontrol."""
    if _IS_MAC or _IS_LINUX:
        return open_windows_sound_settings()
    try:
        os.startfile("ms-settings:apps-volume")
        return True
    except Exception as exc:
        logger.error("Could not open ms-settings:apps-volume: %s", exc)
        return False


def open_classic_sound_control_panel() -> bool:
    """Open classic sound control panel (Audio MIDI Setup on Mac, mmsys.cpl on Windows)."""
    if _IS_MAC:
        try:
            subprocess.Popen(["open", "-a", "Audio MIDI Setup"])
            return True
        except Exception:
            return False

    if _IS_LINUX:
        return open_windows_sound_settings()
    try:
        subprocess.Popen(
            ["control.exe", "mmsys.cpl,,0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        logger.error("Could not open mmsys.cpl: %s", exc)
        return False


def set_virtual_device_friendly_name(friendly_name: str = "PhoneSpeaker Virtual Audio") -> Tuple[bool, str]:
    """
    Fast (<1s) elevated PowerShell script that renames VB-CABLE / AudioRelay virtual endpoints
    in MMDevices & SWD\\MMDEVAPI to 'PhoneSpeaker Virtual Audio' and restarts Audiosrv.
    """
    if _IS_MAC:
        return True, "macOS CoreAudio uses device name 'BlackHole 2ch' / 'Multi-Output Device'."

    if _IS_LINUX:
        return setup_linux_virtual_audio()

    ps_content = f'''$ErrorActionPreference = "SilentlyContinue"
$targetName = "{friendly_name}"
$shortName = "PhoneSpeaker"

# 1. Fast targeted scan of SWD\\MMDEVAPI and ROOT\\MEDIA only (completes in <0.5s)
$enumRoots = @(
    "HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\SWD\\MMDEVAPI",
    "HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\ROOT\\MEDIA"
)
foreach ($er in $enumRoots) {{
    if (Test-Path $er) {{
        Get-ChildItem -Path $er -Recurse | ForEach-Object {{
            $p = $_.PSPath
            $props = Get-ItemProperty -Path $p
            if ($props.FriendlyName -match "AudioRelay|Virtual Speakers|VB-Audio|CABLE" -or $props.DeviceDesc -match "AudioRelay|Virtual Speakers|VB-Audio|CABLE") {{
                Set-ItemProperty -Path $p -Name "FriendlyName" -Value $targetName -Force
                Set-ItemProperty -Path $p -Name "DeviceDesc" -Value $targetName -Force
            }}
        }}
    }}
}}

# 2. Update MMDevices Render & Capture Properties
$mmPaths = @(
    "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\Audio\\Render",
    "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\Audio\\Capture"
)

foreach ($base in $mmPaths) {{
    if (-not (Test-Path $base)) {{ continue }}
    Get-ChildItem -Path $base | ForEach-Object {{
        $propPath = Join-Path $_.PSPath "Properties"
        if (Test-Path $propPath) {{
            $vals = Get-ItemProperty -Path $propPath
            $allText = ($vals.PSObject.Properties | ForEach-Object {{ $_.Value }}) -join " "
            if ($allText -match "AudioRelay|Virtual Speakers|CABLE|VB-Audio|PhoneSpeaker") {{
                $regKeyPath = $propPath -replace "^Microsoft\\.PowerShell\\.Core\\\\Registry::HKEY_LOCAL_MACHINE\\\\", ""
                $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($regKeyPath, [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree, [System.Security.AccessControl.RegistryRights]::TakeOwnership)
                if ($key) {{
                    $acl = $key.GetAccessControl()
                    $adminSid = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544")
                    $acl.SetOwner($adminSid)
                    $key.SetAccessControl($acl)
                    $key.Close()
                }}
                $key2 = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($regKeyPath, [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree, [System.Security.AccessControl.RegistryRights]::ChangePermissions)
                if ($key2) {{
                    $acl2 = $key2.GetAccessControl()
                    $rule = New-Object System.Security.AccessControl.RegistryAccessRule("Administrators", "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
                    $acl2.SetAccessRule($rule)
                    $key2.SetAccessControl($acl2)
                    $key2.Close()
                }}
                Set-ItemProperty -Path $propPath -Name "{{a45c254e-df1c-4efd-8020-67d146a850e0}},2" -Value $targetName -Force
                Set-ItemProperty -Path $propPath -Name "{{b3f8fa53-0004-438e-9003-51a46e139b6c}},2" -Value $targetName -Force
                Set-ItemProperty -Path $propPath -Name "{{b3f8fa53-0004-438e-9003-51a46e139b6c}},6" -Value $shortName -Force
            }}
        }}
    }}
}}

Restart-Service -Name "Audiosrv" -Force
'''
    try:
        script_path = Path(tempfile.gettempdir()) / "phonespeaker_rename_device.ps1"
        script_path.write_text(ps_content, encoding="utf-8")
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-WindowStyle", "Hidden",
            "-Command",
            f"Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{script_path}\"'"
        ]
        ret = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if ret.returncode == 0:
            return True, f"Renamed virtual audio device to '{friendly_name}' and restarted Windows Audio service!"
        return False, "Administrator prompt was cancelled or failed."
    except Exception as exc:
        logger.error("Error renaming virtual device: %s", exc)
        return False, f"Failed to rename virtual device: {exc}"


def download_and_install_vbcable(target_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """
    On macOS, installs BlackHole 2ch via Homebrew or downloads pkg.
    On Linux, creates PipeWire/PulseAudio virtual audio devices via pactl.
    On Windows, downloads official VB-CABLE package into %TEMP%\\PhoneSpeaker_VBCable,
    extracts it, and runs VBCABLE_Setup_x64.exe elevated with the correct WorkingDirectory.
    """
    if _IS_MAC:
        return setup_macos_virtual_audio()

    if _IS_LINUX:
        return setup_linux_virtual_audio()

    if target_dir is None:
        target_dir = Path(tempfile.gettempdir()) / "PhoneSpeaker_VBCable"

    target_dir.mkdir(parents=True, exist_ok=True)
    zip_dest = target_dir / "vbcable.zip"
    installer_exe = target_dir / "VBCABLE_Setup_x64.exe"
    if not installer_exe.exists() and (target_dir / "VBCABLE_Setup.exe").exists():
        installer_exe = target_dir / "VBCABLE_Setup.exe"

    try:
        if not installer_exe.exists():
            logger.info("Downloading VB-CABLE driver package to %s...", zip_dest)
            req = urllib.request.Request(
                VBCABLE_URL,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PhoneSpeaker/2.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                zip_dest.write_bytes(resp.read())

            with zipfile.ZipFile(zip_dest, "r") as z:
                z.extractall(target_dir)
            if zip_dest.exists():
                zip_dest.unlink()

        if (target_dir / "VBCABLE_Setup_x64.exe").exists():
            installer_exe = target_dir / "VBCABLE_Setup_x64.exe"
        elif (target_dir / "VBCABLE_Setup.exe").exists():
            installer_exe = target_dir / "VBCABLE_Setup.exe"

        if installer_exe.exists():
            ps_cmd = (
                f"Start-Process -FilePath '{installer_exe}' "
                f"-WorkingDirectory '{target_dir}' -Verb RunAs -Wait"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                timeout=180,
            )
            return True, (
                "VB-Audio Virtual Cable installer finished!\n\n"
                "• In the installer window, click 'Install Driver' (if you haven't already).\n"
                "• Click [ ↻ Refresh ] in PhoneSpeaker to use 'CABLE Input' & 'CABLE Output'!"
            )
        return False, "Could not find VBCABLE_Setup_x64.exe after extraction."
    except Exception as exc:
        logger.error("Failed to install VB-CABLE: %s", exc)
        return False, f"Virtual driver installation failed: {exc}"
