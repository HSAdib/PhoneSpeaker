#!/usr/bin/env bash
# install_linux.sh — One-line instant installer for PhoneSpeaker v2.0 Studio (Ubuntu/Debian, Arch, Fedora)
set -e

echo "=========================================================="
echo "  PhoneSpeaker v2.0 Studio — Linux Installer"
echo "=========================================================="

# 1. Install system dependencies
if command -v pacman >/dev/null 2>&1; then
    echo "[Arch Linux detected] Ensuring dependencies via pacman..."
    sudo pacman -S --needed --noconfirm python tk opus libpulse portaudio wget
elif command -v apt-get >/dev/null 2>&1; then
    echo "[Debian/Ubuntu detected] Ensuring dependencies via apt..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-venv python3-tk libopus0 pulseaudio-utils portaudio19-dev wget
elif command -v dnf >/dev/null 2>&1; then
    echo "[Fedora detected] Ensuring dependencies via dnf..."
    sudo dnf install -y -q python3 python3-tkinter opus pulseaudio-utils portaudio-devel wget
fi

# 2. Check if we already have the installer locally or need to download it
TEMP_INSTALLER=0
if [ -f "PhoneSpeaker-v2-Linux-Installer.run" ]; then
    INSTALLER="./PhoneSpeaker-v2-Linux-Installer.run"
elif [ -f "phonespeaker-pc/build_linux.py" ]; then
    echo "Building installer from local source..."
    python3 phonespeaker-pc/build_linux.py
    INSTALLER="./PhoneSpeaker-v2-Linux-Installer.run"
else
    echo "Downloading PhoneSpeaker v2.0 Linux installer from GitHub..."
    INSTALLER="/tmp/PhoneSpeaker-v2-Linux-Installer.run"
    wget -q --show-progress -O "$INSTALLER" "https://github.com/HSAdib/PhoneSpeaker/releases/download/v2.0.0/PhoneSpeaker-v2-Linux-Installer.run"
    TEMP_INSTALLER=1
fi

# 3. Make executable and run
chmod +x "$INSTALLER"
"$INSTALLER"

# 4. Cleanup if downloaded to tmp
if [ "$TEMP_INSTALLER" -eq 1 ]; then
    rm -f "$INSTALLER"
fi
