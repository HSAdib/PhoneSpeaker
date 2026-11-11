#!/usr/bin/env python3
"""
build_macos.py — Build standalone macOS Application bundle (.app) and DMG for PhoneSpeaker v2.0.

Output:
  - dist/PhoneSpeaker v2.app (macOS Application bundle)
  - ../PhoneSpeaker-v2-macOS.dmg (Drag-and-Drop macOS Disk Image installer)
"""

import ctypes.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv-macos"
VENDOR = ROOT / "vendor"
SRC_MAIN = ROOT / "src" / "main.py"


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"ERROR: command exited with code {result.returncode}", flush=True)
        sys.exit(result.returncode)
    return result


def find_system_libopus() -> Path | None:
    candidates = [
        Path("/opt/homebrew/lib/libopus.0.dylib"),
        Path("/opt/homebrew/lib/libopus.dylib"),
        Path("/usr/local/lib/libopus.0.dylib"),
        Path("/usr/local/lib/libopus.dylib"),
    ]
    for c in candidates:
        if c.exists():
            return c
    found = ctypes.util.find_library("opus")
    if found and Path(found).exists():
        return Path(found)
    return None


def main() -> None:
    os.chdir(ROOT)
    VENDOR.mkdir(parents=True, exist_ok=True)

    if not VENV.exists():
        print("Creating macOS virtual environment...", flush=True)
        run([sys.executable, "-m", "venv", str(VENV)])

    python = VENV / "bin" / "python"
    pip = VENV / "bin" / "pip"

    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "-q"])

    # Install macOS Python requirements
    mac_pkgs = [
        "numpy>=1.26.0",
        "psutil>=5.9.8",
        "Pillow>=10.3.0",
        "pystray>=0.19.5",
        "pyinstaller>=6.6.0",
        "qrcode>=7.4.2",
    ]
    run([str(pip), "install"] + mac_pkgs)
    # Optional audio libraries (soxr, pyaudio)
    subprocess.run([str(pip), "install", "soxr", "pyaudio"], check=False)

    # Locate and copy libopus.dylib into vendor/
    opus_dylib = VENDOR / "libopus.dylib"
    if not opus_dylib.exists():
        sys_opus = find_system_libopus()
        if sys_opus:
            shutil.copy2(sys_opus, opus_dylib)
            print(f"Bundling system libopus: {sys_opus} -> {opus_dylib}")

    # Generate PNG icon
    icon_png = VENDOR / "app_icon.png"
    if not icon_png.exists():
        gen_script = (
            "import sys; "
            f"sys.path.insert(0, r'{ROOT / 'src'}'); "
            "from ui import _make_tray_image; "
            f"img = _make_tray_image(256); img.save(r'{icon_png}', format='PNG')"
        )
        subprocess.run([str(python), "-c", gen_script], check=False)

    # Build macOS app bundle with PyInstaller
    pyinstaller_args = [
        str(python), "-m", "PyInstaller",
        "--windowed",
        "--noconsole",
        "--name", "PhoneSpeaker v2",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build-macos"),
        "--specpath", str(ROOT),
        "--paths", str(ROOT / "src"),
        "--hidden-import", "PIL._tkinter_finder",
    ]
    if opus_dylib.exists():
        pyinstaller_args += ["--add-binary", f"{opus_dylib}:."]
    if icon_png.exists():
        pyinstaller_args += ["--icon", str(icon_png)]

    pyinstaller_args.append(str(SRC_MAIN))
    run(pyinstaller_args)

    app_path = ROOT / "dist" / "PhoneSpeaker v2.app"
    if not app_path.exists():
        print("ERROR: PhoneSpeaker v2.app was not created.")
        sys.exit(1)

    print(f"macOS Application bundle ready: {app_path}")

    # Build DMG if hdiutil is available
    hdiutil = shutil.which("hdiutil")
    dmg_path = ROOT.parent / "PhoneSpeaker-v2-macOS.dmg"
    if hdiutil:
        print("Creating macOS DMG image installer...", flush=True)
        dmg_temp = ROOT / "dist" / "PhoneSpeaker-v2-macOS.dmg"
        if dmg_temp.exists():
            dmg_temp.unlink()
        run([
            "hdiutil", "create",
            "-volname", "PhoneSpeaker v2",
            "-srcfolder", str(app_path),
            "-ov",
            "-format", "UDZO",
            str(dmg_temp)
        ])
        if dmg_temp.exists():
            shutil.copy2(dmg_temp, dmg_path)
            size_mb = dmg_path.stat().st_size / (1024 * 1024)
            print(f"SUCCESS: macOS DMG installer created at {dmg_path} ({size_mb:.1f} MB)")
    else:
        print("Note: 'hdiutil' not available on this platform. App bundle is at dist/PhoneSpeaker v2.app")


if __name__ == "__main__":
    main()

