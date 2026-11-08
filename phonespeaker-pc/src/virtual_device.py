"""
virtual_device.py — Windows virtual audio output device integration,
switching, and driver installer for PhoneSpeaker v2.0.
"""

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_IS_LINUX = sys.platform.startswith("linux")
VBCABLE_URL = "https://download.vb-audio.com/Download_DIR/VBCABLE_Driver_Pack43.zip"

_VIRTUAL_KEYWORDS = [
    "virtual speaker",
    "virtual audio",
    "cable input",
    "vb-audio",
    "virtual cable",
    "phonespeaker",
]


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

        created = []
        if "PhoneSpeaker_Sink" not in existing:
            subprocess.run([
                "pactl", "load-module", "module-null-sink",
                "sink_name=PhoneSpeaker_Sink",
                "sink_properties=device.description=PhoneSpeaker_Virtual_Speaker"
            ], check=True, timeout=3)
            created.append("PhoneSpeaker_Sink (Virtual Speaker)")

        if "PhoneSpeaker_Mic_Sink" not in existing:
            subprocess.run([
                "pactl", "load-module", "module-null-sink",
                "sink_name=PhoneSpeaker_Mic_Sink",
                "sink_properties=device.description=PhoneSpeaker_Mic_Bridge"
            ], check=True, timeout=3)
            created.append("PhoneSpeaker_Mic_Sink")

        if "PhoneSpeaker_Mic" not in existing:
            subprocess.run([
                "pactl", "load-module", "module-remap-source",
                "master=PhoneSpeaker_Mic_Sink.monitor",
                "source_name=PhoneSpeaker_Mic",
                "source_properties=device.description=PhoneSpeaker_Wireless_Mic"
            ], check=True, timeout=3)
            created.append("PhoneSpeaker_Mic (Virtual Microphone)")

        msg = (
            "Linux PipeWire/PulseAudio virtual devices active!\n\n"
            "• Virtual Speaker: 'PhoneSpeaker_Sink'\n"
            "• Wireless Mic for Discord/Zoom: 'PhoneSpeaker_Mic'\n"
            "  (Select '[PipeWire/Pulse] PhoneSpeaker_Mic_Sink' in PC Playback Dev to route Phone Mic into it!)"
        )
        return True, msg
    except Exception as exc:
        return False, f"Failed to create PipeWire/PulseAudio virtual devices: {exc}"


def detect_virtual_devices() -> List[Dict]:
    """
    Scan WASAPI audio devices for virtual playback devices (e.g. Virtual Speakers, CABLE Input).
    Returns list of dicts: {index, name, is_virtual, is_default}
    """
    results = []
    if _IS_LINUX and shutil.which("pactl"):
        try:
            res = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        name = parts[1].strip()
                        if "phonespeaker" in name.lower() or "null" in name.lower():
                            results.append({
                                "index": f"pulse:{name}",
                                "name": f"[PipeWire/Pulse] {name}",
                                "is_virtual": True,
                                "is_default": False,
                            })
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
                    if info.get("maxOutputChannels", 0) > 0 and not info.get("isLoopbackDevice", False):
                        name = info.get("name", "")
                        name_lower = name.lower()
                        is_virt = any(kw in name_lower for kw in _VIRTUAL_KEYWORDS)
                        results.append({
                            "index": i,
                            "name": name,
                            "is_virtual": is_virt,
                            "is_default": (i == default_out_idx),
                        })
                except Exception:
                    continue
        finally:
            pa.terminate()
    except Exception as exc:
        logger.error("Error detecting virtual devices: %s", exc)

    return results


def find_preferred_virtual_device() -> Optional[Dict]:
    """Return the first detected virtual speaker device, if any."""
    devs = detect_virtual_devices()
    for d in devs:
        if d.get("is_virtual"):
            return d
    return None


def open_windows_sound_settings() -> bool:
    """
    Open OS sound settings or trigger the Win+Ctrl+V flyout on Windows / pavucontrol on Linux.
    """
    if _IS_LINUX:
        for cmd in [["pavucontrol"], ["gnome-control-center", "sound"], ["kcmshell6", "kcm_pulseaudio"]]:
            if shutil.which(cmd[0]):
                try:
                    subprocess.Popen(cmd)
                    return True
                except Exception:
                    continue
        return False

    try:
        # Try triggering Win+Ctrl+V flyout (Windows 11 audio endpoint switcher)
        VK_LWIN = 0x5B
        VK_CONTROL = 0x11
        VK_V = 0x56
        KEYEVENTF_KEYUP = 0x0002

        user32 = ctypes.windll.user32
        user32.keybd_event(VK_LWIN, 0, 0, 0)
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        pass

    try:
        os.system("start ms-settings:sound")
        return True
    except Exception as exc:
        logger.error("Could not open sound settings: %s", exc)
        return False


def open_classic_sound_control_panel() -> bool:
    """Open classic mmsys.cpl sound control panel."""
    if _IS_LINUX:
        return open_windows_sound_settings()
    try:
        subprocess.Popen(["rundll32.exe", "shell32.dll,Control_RunDLL", "mmsys.cpl,,playback"])
        return True
    except Exception as exc:
        logger.error("Could not open mmsys.cpl: %s", exc)
        return False


def set_virtual_device_friendly_name(friendly_name: str = "PhoneSpeaker Virtual Audio") -> Tuple[bool, str]:
    """
    Write and execute an elevated PowerShell script that renames 'Virtual Speakers for AudioRelay'
    (or VB-CABLE) in HKLM MMDevices & Enum to 'PhoneSpeaker Virtual Audio' and restarts Audiosrv.
    """
    if _IS_LINUX:
        return setup_linux_virtual_audio()

    import tempfile

    ps_content = f'''$ErrorActionPreference = "SilentlyContinue"
$targetName = "{friendly_name}"
$shortName = "PhoneSpeaker"
$found = $false

# 1. Update HKLM CurrentControlSet Enum FriendlyName & DeviceDesc for AudioRelay / Virtual Audio drivers
Get-ChildItem -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Enum" -Recurse | ForEach-Object {{
    $p = $_.PSPath
    $props = Get-ItemProperty -Path $p
    if ($props.FriendlyName -match "AudioRelay|Virtual Speakers|VB-Audio" -or $props.DeviceDesc -match "AudioRelay|Virtual Speakers|VB-Audio") {{
        Set-ItemProperty -Path $p -Name "FriendlyName" -Value $targetName -Force
        Set-ItemProperty -Path $p -Name "DeviceDesc" -Value $targetName -Force
        $found = $true
    }}
}}

# 2. Take ownership / grant Admin write access on MMDevices Render & Capture Properties and update PKEYs
$mmPaths = @(
    "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\Audio\\Render",
    "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\Audio\\Capture"
)

foreach ($base in $mmPaths) {{
    Get-ChildItem -Path $base | ForEach-Object {{
        $propPath = Join-Path $_.PSPath "Properties"
        if (Test-Path $propPath) {{
            # Grant Administrators FullControl on Properties subkey
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

            $vals = Get-ItemProperty -Path $propPath
            $allText = ($vals.PSObject.Properties | ForEach-Object {{ $_.Value }}) -join " "
            if ($allText -match "AudioRelay|Virtual Speakers|CABLE|VB-Audio|PhoneSpeaker") {{
                Set-ItemProperty -Path $propPath -Name "{{a45c254e-df1c-4efd-8020-67d146a850e0}},2" -Value $targetName -Force
                Set-ItemProperty -Path $propPath -Name "{{b3f8fa53-0004-438e-9003-51a46e139b6c}},2" -Value $targetName -Force
                Set-ItemProperty -Path $propPath -Name "{{b3f8fa53-0004-438e-9003-51a46e139b6c}},6" -Value $shortName -Force
                $found = $true
            }}
        }}
    }}
}}

# 3. Restart Windows Audio service so Win+Ctrl+V refreshes immediately
Restart-Service -Name "Audiosrv" -Force
'''
    try:
        script_path = Path(tempfile.gettempdir()) / "phonespeaker_rename_device.ps1"
        script_path.write_text(ps_content, encoding="utf-8")
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{script_path}\"'"
        ]
        ret = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if ret.returncode == 0:
            return True, f"Renamed virtual device to '{friendly_name}' and restarted Windows Audio service.\nPress Win + Ctrl + V to see 'PhoneSpeaker ({friendly_name})'!"
        return False, "Administrator prompt was cancelled or failed."
    except Exception as exc:
        logger.error("Error renaming virtual device: %s", exc)
        return False, str(exc)


def download_and_install_vbcable(target_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """
    On Linux, sets up PipeWire/PulseAudio virtual audio devices.
    On Windows, downloads official VB-CABLE redistributable, unzips, and launches VBCABLE_Setup_x64.exe elevated.
    """
    if _IS_LINUX:
        return setup_linux_virtual_audio()

    import urllib.request

    if target_dir is None:
        target_dir = Path(__file__).resolve().parent.parent / "vendor" / "vbcable"

    target_dir.mkdir(parents=True, exist_ok=True)
    zip_dest = target_dir / "vbcable.zip"
    installer_exe = target_dir / "VBCABLE_Setup_x64.exe"

    try:
        if not installer_exe.exists():
            logger.info("Downloading VB-CABLE driver package...")
            urllib.request.urlretrieve(VBCABLE_URL, zip_dest)
            with zipfile.ZipFile(zip_dest, "r") as z:
                z.extractall(target_dir)
            if zip_dest.exists():
                zip_dest.unlink()

        if installer_exe.exists():
            # Run elevated installer
            ps_cmd = f"Start-Process '{installer_exe}' -Verb RunAs -Wait"
            subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps_cmd], timeout=120)
            return True, "VB-Audio Virtual Cable installer launched. Follow the on-screen prompts to complete driver installation."
        else:
            return False, "Could not find VBCABLE_Setup_x64.exe after extraction."
    except Exception as exc:
        logger.error("Failed to install VB-CABLE: %s", exc)
        return False, f"Installation failed: {exc}"

