#!/usr/bin/env python3
"""
build_linux.py — Build standalone Linux executable and .run installer for PhoneSpeaker v2.0.

Output:
  - dist/PhoneSpeaker-v2 (Standalone Linux executable, like .exe on Windows)
  - ../PhoneSpeaker-v2-Linux-Installer.run (One-click Linux installer with desktop icon)
"""

import ctypes.util
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv-linux"
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
        Path("/usr/lib/libopus.so.0"),
        Path("/usr/lib64/libopus.so.0"),
        Path("/usr/lib/x86_64-linux-gnu/libopus.so.0"),
        Path("/usr/lib/aarch64-linux-gnu/libopus.so.0"),
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
        print("Creating Linux virtual environment...", flush=True)
        run([sys.executable, "-m", "venv", str(VENV)])

    python = VENV / "bin" / "python"
    pip = VENV / "bin" / "pip"

    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "-q"])

    # Install Linux Python requirements (PyAudioWPatch is Windows-only; on Linux use numpy, psutil, Pillow, pystray, pyinstaller)
    linux_pkgs = ["numpy>=1.26.0", "psutil>=5.9.8", "Pillow>=10.3.0", "pystray>=0.19.5", "pyinstaller>=6.6.0", "qrcode>=7.4.2"]
    run([str(pip), "install"] + linux_pkgs)
    # Try optional pyaudio & soxr (non-fatal if portaudio headers aren't installed; parec/pacat work natively!)
    subprocess.run([str(pip), "install", "soxr", "pyaudio"], check=False)

    # Locate and copy libopus.so.0 into vendor/
    opus_so = VENDOR / "libopus.so.0"
    if not opus_so.exists():
        sys_opus = find_system_libopus()
        if sys_opus:
            shutil.copy2(sys_opus, opus_so)
            print(f"Bundling system libopus: {sys_opus} -> {opus_so}")

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

    # Build single-file Linux binary with PyInstaller
    pyinstaller_args = [
        str(python), "-m", "PyInstaller",
        "--noconsole",
        "--onefile",
        "--name", "PhoneSpeaker-v2",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build-linux"),
        "--specpath", str(ROOT),
        "--paths", str(ROOT / "src"),
        "--hidden-import", "PIL._tkinter_finder",
    ]
    if opus_so.exists():
        pyinstaller_args += ["--add-binary", f"{opus_so}:."]

    pyinstaller_args.append(str(SRC_MAIN))
    run(pyinstaller_args)

    binary = ROOT / "dist" / "PhoneSpeaker-v2"
    if not binary.exists():
        print("ERROR: Linux binary was not created.")
        sys.exit(1)

    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    root_bin = ROOT.parent / "PhoneSpeaker-v2"
    shutil.copy2(binary, root_bin)
    print(f"Standalone Linux binary ready: {root_bin}")

    # Build self-extracting .run installer
    payload_tar = ROOT / "dist" / "payload.tar.gz"
    with tarfile.open(payload_tar, "w:gz") as tar:
        tar.add(binary, arcname="PhoneSpeaker-v2")
        if icon_png.exists():
            tar.add(icon_png, arcname="app_icon.png")

    installer_header = b"""#!/usr/bin/env bash
set -e
echo "===================================================="
echo " Installing PhoneSpeaker v2.0 Studio for Linux..."
echo "===================================================="
INSTALL_DIR="$HOME/.local/opt/phonespeaker"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$APP_DIR"

ARCHIVE_LINE=$(awk '/^__ARCHIVE_BELOW__/ {print NR + 1; exit 0; }' "$0")
tail -n+$ARCHIVE_LINE "$0" | tar -xz -C "$INSTALL_DIR"

chmod +x "$INSTALL_DIR/PhoneSpeaker-v2"
ln -sf "$INSTALL_DIR/PhoneSpeaker-v2" "$BIN_DIR/phonespeaker"

cat > "$APP_DIR/phonespeaker.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PhoneSpeaker v2.0 Studio
Comment=Low-latency PC to Android Speaker & Wireless Mic
Exec=$INSTALL_DIR/PhoneSpeaker-v2
Icon=$INSTALL_DIR/app_icon.png
Terminal=false
Categories=AudioVideo;Audio;Network;
EOF

chmod +x "$APP_DIR/phonespeaker.desktop"
update-desktop-database "$APP_DIR" 2>/dev/null || true

echo "SUCCESS! Installed to $INSTALL_DIR/PhoneSpeaker-v2"
echo "Run 'phonespeaker' from terminal or launch 'PhoneSpeaker v2.0 Studio' from your App Menu!"
exit 0
__ARCHIVE_BELOW__
"""
    installer_run = ROOT.parent / "PhoneSpeaker-v2-Linux-Installer.run"
    with open(installer_run, "wb") as out_f:
        out_f.write(installer_header)
        out_f.write(payload_tar.read_bytes())

    installer_run.chmod(installer_run.stat().st_mode | stat.S_IEXEC)
    print(f"Self-extracting Linux Installer created: {installer_run}")


if __name__ == "__main__":
    main()

