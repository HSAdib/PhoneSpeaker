"""
build_exe.py — Build PhoneSpeaker.exe using PyInstaller.

Creates venv if needed, installs requirements, then runs PyInstaller.
Output: dist/PhoneSpeaker.exe
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENDOR = ROOT / "vendor"
SRC_MAIN = ROOT / "src" / "main.py"
OPUS_DLL = VENDOR / "opus.dll"

REQUIREMENTS = ROOT / "requirements.txt"


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"ERROR: command exited with code {result.returncode}", flush=True)
        sys.exit(result.returncode)
    return result


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def venv_pip() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "pip.exe"
    return VENV / "bin" / "pip"


def main() -> None:
    os.chdir(ROOT)

    # 1. Create venv
    if not VENV.exists():
        print("Creating virtual environment...", flush=True)
        run([sys.executable, "-m", "venv", str(VENV)])
    else:
        print("Virtual environment already exists.", flush=True)

    python = venv_python()
    pip = venv_pip()

    # 2. Upgrade pip
    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "-q"])

    # 3. Install requirements
    print("Installing requirements...", flush=True)
    run([str(pip), "install", "-r", str(REQUIREMENTS), "-q"])

    # 4. Acquire opus.dll if missing
    if not OPUS_DLL.exists():
        print("Acquiring opus.dll...", flush=True)
        run([str(python), str(VENDOR / "get_opus.py")])
    else:
        print(f"opus.dll already present: {OPUS_DLL}", flush=True)

    # 5. Build EXE
    print("Building PhoneSpeaker.exe with PyInstaller...", flush=True)

    pyinstaller_args = [
        str(python), "-m", "PyInstaller",
        "--noconsole",
        "--onefile",
        "--name", "PhoneSpeaker v2",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT),
        "--paths", str(ROOT / "src"),
    ]

    if OPUS_DLL.exists():
        pyinstaller_args += ["--add-binary", f"{OPUS_DLL};."]

    icon_path = VENDOR / "app_icon.ico"
    if not icon_path.exists():
        gen_script = (
            "import sys; from pathlib import Path; "
            f"sys.path.insert(0, r'{ROOT / 'src'}'); "
            "from ui import _make_tray_image; "
            f"Path(r'{VENDOR}').mkdir(parents=True, exist_ok=True); "
            f"img = _make_tray_image(256); "
            f"img.save(r'{icon_path}', format='ICO', sizes=[(256, 256), (64, 64), (32, 32), (16, 16)]); "
            "print('Icon generated successfully')"
        )
        try:
            run([str(python), "-c", gen_script])
            print(f"Generated icon: {icon_path}", flush=True)
        except Exception as exc:
            print(f"Could not generate app_icon.ico: {exc}", flush=True)

    if icon_path.exists():
        pyinstaller_args += ["--icon", str(icon_path)]

    pyinstaller_args.append(str(SRC_MAIN))

    run(pyinstaller_args)

    exe = ROOT / "dist" / "PhoneSpeaker v2.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\nSUCCESS: {exe} ({size_mb:.1f} MB)", flush=True)

        root_exe = ROOT.parent / "PhoneSpeaker v2.exe"
        try:
            shutil.copy2(exe, root_exe)
            print(f"Copied to root: {root_exe}", flush=True)
        except Exception as exc:
            print(f"Warning: Could not copy to {root_exe}: {exc}", flush=True)
    else:
        print("\nWARNING: PhoneSpeaker v2.exe not found in dist/", flush=True)

    build_linux_installer_bundle()
    build_macos_installer_bundle()


def build_linux_installer_bundle() -> None:
    """
    Create a self-extracting Linux installer (PhoneSpeaker-v2-Linux-Installer.run)
    and place it directly in C:\\Users\\hasan\\PhoneSpeaker\\ alongside PhoneSpeaker v2.exe and PhoneSpeaker v2.apk.
    """
    import io
    import tarfile

    print("\nPackaging Linux self-extracting installer (PhoneSpeaker-v2-Linux-Installer.run)...", flush=True)
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
        for f in (ROOT / "src").glob("*.py"):
            tar.add(f, arcname=f"phonespeaker-pc/src/{f.name}")
        if (ROOT / "build_linux.py").exists():
            tar.add(ROOT / "build_linux.py", arcname="phonespeaker-pc/build_linux.py")
        if (ROOT / "requirements.txt").exists():
            tar.add(ROOT / "requirements.txt", arcname="phonespeaker-pc/requirements.txt")
        icon_png = VENDOR / "app_icon.png"
        if not icon_png.exists():
            try:
                sys.path.insert(0, str(ROOT / "src"))
                from ui import _make_tray_image
                img = _make_tray_image(256)
                img.save(str(icon_png), format="PNG")
            except Exception:
                pass
        if icon_png.exists():
            tar.add(icon_png, arcname="phonespeaker-pc/vendor/app_icon.png")

    header_script = """#!/usr/bin/env bash
set -e

if [ "$1" = "--uninstall" ] || [ "$1" = "-u" ] || [ "$1" = "uninstall" ]; then
    echo "=========================================================="
    echo "  PhoneSpeaker v2.0 Studio — Uninstalling..."
    echo "=========================================================="
    pkill -f "phonespeaker-pc/src/main.py" 2>/dev/null || true
    pkill -f "PhoneSpeaker-v2" 2>/dev/null || true
    rm -rf "$HOME/.local/opt/phonespeaker" "$HOME/.local/bin/phonespeaker" \
           "$HOME/.local/share/applications/phonespeaker.desktop" \
           "$HOME/.local/share/icons/phonespeaker.png" "$HOME/.config/PhoneSpeaker"
    sudo rm -f /usr/local/bin/phonespeaker 2>/dev/null || true
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    echo "PhoneSpeaker v2.0 Studio has been successfully uninstalled."
    exit 0
fi

echo "=========================================================="
echo " PhoneSpeaker v2.0 Studio — Linux One-Click Installer"
echo "=========================================================="

# 1. Ensure system dependencies (Arch / Debian / Ubuntu / Fedora)
if command -v pacman >/dev/null 2>&1; then
    echo "[Arch Linux] Installing dependencies via pacman..."
    sudo pacman -S --needed --noconfirm python tk opus libpulse portaudio python-numpy python-psutil python-pillow
elif command -v apt-get >/dev/null 2>&1; then
    echo "[Debian/Ubuntu] Installing dependencies via apt..."
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-tk python3-pip python3-numpy python3-psutil python3-pil libopus0 pulseaudio-utils portaudio19-dev
elif command -v dnf >/dev/null 2>&1; then
    echo "[Fedora] Installing dependencies via dnf..."
    sudo dnf install -y python3 python3-tkinter python3-pip python3-numpy python3-psutil python3-pillow opus pulseaudio-utils portaudio-devel
fi

INSTALL_DIR="$HOME/.local/opt/phonespeaker"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons"
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$APP_DIR" "$ICON_DIR" "$HOME/.config/PhoneSpeaker"

ARCHIVE_LINE=$(awk '/^__ARCHIVE_BELOW__/ {print NR + 1; exit 0; }' "$0")
tail -n+$ARCHIVE_LINE "$0" | tar -xz -C "$INSTALL_DIR"

cd "$INSTALL_DIR/phonespeaker-pc"
rm -rf .venv-linux
python3 -m venv --system-site-packages .venv-linux
./.venv-linux/bin/pip install --upgrade pip -q 2>/dev/null || true
./.venv-linux/bin/pip install numpy psutil Pillow qrcode -q 2>/dev/null || true
./.venv-linux/bin/pip install soxr pyaudio pystray -q 2>/dev/null || true

cp -f "$INSTALL_DIR/phonespeaker-pc/vendor/app_icon.png" "$ICON_DIR/phonespeaker.png" 2>/dev/null || true

cat > "$INSTALL_DIR/PhoneSpeaker-v2" <<'EOF'
#!/usr/bin/env bash
mkdir -p "$HOME/.config/PhoneSpeaker"
exec "$HOME/.local/opt/phonespeaker/phonespeaker-pc/.venv-linux/bin/python" "$HOME/.local/opt/phonespeaker/phonespeaker-pc/src/main.py" "$@" >>"$HOME/.config/PhoneSpeaker/startup.log" 2>&1
EOF
chmod +x "$INSTALL_DIR/PhoneSpeaker-v2"
ln -sf "$INSTALL_DIR/PhoneSpeaker-v2" "$BIN_DIR/phonespeaker"
sudo ln -sf "$INSTALL_DIR/PhoneSpeaker-v2" /usr/local/bin/phonespeaker 2>/dev/null || true

cat > "$APP_DIR/phonespeaker.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PhoneSpeaker v2.0 Studio
Comment=Low-latency Wireless PC Speaker & Mic Studio
Exec=$INSTALL_DIR/PhoneSpeaker-v2
Icon=$ICON_DIR/phonespeaker.png
Terminal=false
StartupNotify=true
Categories=AudioVideo;Audio;Network;
EOF
chmod +x "$APP_DIR/phonespeaker.desktop"
update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true

echo "=========================================================="
echo " SUCCESS! PhoneSpeaker v2.0 Studio installed!"
echo " Command: phonespeaker"
echo " Launching PhoneSpeaker v2.0 Studio now..."
echo "=========================================================="
nohup "$INSTALL_DIR/PhoneSpeaker-v2" >/dev/null 2>&1 &
exit 0
__ARCHIVE_BELOW__
""".replace("\r\n", "\n").encode("utf-8")

    installer_path = ROOT.parent / "PhoneSpeaker-v2-Linux-Installer.run"
    with open(installer_path, "wb") as f:
        f.write(header_script)
        f.write(tar_buffer.getvalue())
    size_kb = installer_path.stat().st_size / 1024.0
    print(f"SUCCESS: Linux Installer stored at {installer_path} ({size_kb:.1f} KB)", flush=True)


def build_macos_installer_bundle() -> None:
    """
    Create a self-extracting macOS installer (PhoneSpeaker-v2-macOS-Installer.command)
    and place it directly in C:\\Users\\hasan\\PhoneSpeaker\\ alongside PhoneSpeaker v2.exe and PhoneSpeaker-v2-Linux-Installer.run.
    """
    import io
    import tarfile

    print("\nPackaging macOS self-extracting installer (PhoneSpeaker-v2-macOS-Installer.command)...", flush=True)
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
        for f in (ROOT / "src").glob("*.py"):
            tar.add(f, arcname=f"phonespeaker-pc/src/{f.name}")
        if (ROOT / "build_macos.py").exists():
            tar.add(ROOT / "build_macos.py", arcname="phonespeaker-pc/build_macos.py")
        if (ROOT / "build_linux.py").exists():
            tar.add(ROOT / "build_linux.py", arcname="phonespeaker-pc/build_linux.py")
        if (ROOT / "requirements.txt").exists():
            tar.add(ROOT / "requirements.txt", arcname="phonespeaker-pc/requirements.txt")
        icon_png = VENDOR / "app_icon.png"
        if not icon_png.exists():
            try:
                sys.path.insert(0, str(ROOT / "src"))
                from ui import _make_tray_image
                img = _make_tray_image(256)
                img.save(str(icon_png), format="PNG")
            except Exception:
                pass
        if icon_png.exists():
            tar.add(icon_png, arcname="phonespeaker-pc/vendor/app_icon.png")

    header_script = """#!/usr/bin/env bash
set -e

# Change directory to script directory
cd "$(dirname "$0")"

if [ "$1" = "--uninstall" ] || [ "$1" = "-u" ] || [ "$1" = "uninstall" ]; then
    echo "=========================================================="
    echo "  PhoneSpeaker v2.0 Studio — Uninstalling for macOS..."
    echo "=========================================================="
    pkill -f "phonespeaker-pc/src/main.py" 2>/dev/null || true
    rm -rf "$HOME/Applications/PhoneSpeaker v2.app" \
           "/Applications/PhoneSpeaker v2.app" \
           "$HOME/Library/Application Support/PhoneSpeaker" \
           "$HOME/.local/bin/phonespeaker"
    sudo rm -f /usr/local/bin/phonespeaker 2>/dev/null || true
    echo "PhoneSpeaker v2.0 Studio has been successfully uninstalled."
    exit 0
fi

echo "=========================================================="
echo " PhoneSpeaker v2.0 Studio — macOS One-Click Installer"
echo "=========================================================="

# 1. Check for Homebrew
if ! command -v brew >/dev/null 2>&1; then
    echo "Notice: Homebrew is recommended for system audio dependencies (opus, portaudio, blackhole)."
    echo "If you have Homebrew installed, ensure 'brew' is available in your PATH."
fi

# 2. Install audio dependencies via Homebrew if available
if command -v brew >/dev/null 2>&1; then
    echo "Installing audio drivers and codecs via Homebrew..."
    brew install opus portaudio python-tk 2>/dev/null || true
    # Optionally install BlackHole 2ch for virtual loopback capture
    brew install --cask blackhole-2ch 2>/dev/null || true
fi

INSTALL_DIR="$HOME/Library/Application Support/PhoneSpeaker/app"
APP_DIR="$HOME/Applications/PhoneSpeaker v2.app"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$HOME/Applications" "$HOME/Library/Application Support/PhoneSpeaker"

ARCHIVE_LINE=$(awk '/^__ARCHIVE_BELOW__/ {print NR + 1; exit 0; }' "$0")
tail -n+$ARCHIVE_LINE "$0" | tar -xz -C "$INSTALL_DIR"

cd "$INSTALL_DIR/phonespeaker-pc"
rm -rf .venv-macos
python3 -m venv .venv-macos
./.venv-macos/bin/pip install --upgrade pip -q 2>/dev/null || true
./.venv-macos/bin/pip install numpy psutil Pillow qrcode -q 2>/dev/null || true
./.venv-macos/bin/pip install soxr pyaudio pystray -q 2>/dev/null || true

# Construct standard macOS .app bundle
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

if [ -f "$INSTALL_DIR/phonespeaker-pc/vendor/app_icon.png" ]; then
    cp -f "$INSTALL_DIR/phonespeaker-pc/vendor/app_icon.png" "$APP_DIR/Contents/Resources/app_icon.png"
fi

cat > "$APP_DIR/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>phonespeaker</string>
    <key>CFBundleIconFile</key>
    <string>app_icon.png</string>
    <key>CFBundleIdentifier</key>
    <string>com.hasan.phonespeaker.v2</string>
    <key>CFBundleName</key>
    <string>PhoneSpeaker v2</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>2.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>PhoneSpeaker requires microphone access for PC microphone streaming.</string>
</dict>
</plist>
EOF

cat > "$APP_DIR/Contents/MacOS/phonespeaker" <<EOF
#!/usr/bin/env bash
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
mkdir -p "\$HOME/Library/Application Support/PhoneSpeaker"
exec "$INSTALL_DIR/phonespeaker-pc/.venv-macos/bin/python" "$INSTALL_DIR/phonespeaker-pc/src/main.py" "\$@" >>"\$HOME/Library/Application Support/PhoneSpeaker/startup.log" 2>&1
EOF
chmod +x "$APP_DIR/Contents/MacOS/phonespeaker"

ln -sf "$APP_DIR/Contents/MacOS/phonespeaker" "$BIN_DIR/phonespeaker"
sudo ln -sf "$APP_DIR/Contents/MacOS/phonespeaker" /usr/local/bin/phonespeaker 2>/dev/null || true

echo "=========================================================="
echo " SUCCESS! PhoneSpeaker v2.0 Studio installed for macOS!"
echo " Location: $APP_DIR"
echo " CLI Command: phonespeaker"
echo " Launching PhoneSpeaker v2 now..."
echo "=========================================================="
open -a "$APP_DIR"
exit 0
__ARCHIVE_BELOW__
""".replace("\r\n", "\n").encode("utf-8")

    installer_path = ROOT.parent / "PhoneSpeaker-v2-macOS-Installer.command"
    with open(installer_path, "wb") as f:
        f.write(header_script)
        f.write(tar_buffer.getvalue())
    size_kb = installer_path.stat().st_size / 1024.0
    print(f"SUCCESS: macOS Installer stored at {installer_path} ({size_kb:.1f} KB)", flush=True)


if __name__ == "__main__":
    main()
