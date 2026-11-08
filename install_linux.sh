#!/usr/bin/env bash
# install_linux.sh — Auto-installs system dependencies, builds PhoneSpeaker-v2, and installs desktop launcher.
set -e

echo "=========================================================="
echo " PhoneSpeaker v2.0 Studio — Linux Setup & Builder"
echo "=========================================================="

# 1. Install system dependencies (Arch pacman / Debian apt / Fedora dnf)
if command -v pacman >/dev/null 2>&1; then
    echo "[Arch Linux detected] Checking system packages..."
    sudo pacman -S --needed --noconfirm python tk opus libpulse portaudio
elif command -v apt-get >/dev/null 2>&1; then
    echo "[Debian/Ubuntu detected] Checking system packages..."
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-tk libopus0 pulseaudio-utils portaudio19-dev
elif command -v dnf >/dev/null 2>&1; then
    echo "[Fedora detected] Checking system packages..."
    sudo dnf install -y python3 python3-tkinter opus pulseaudio-utils portaudio-devel
fi

# 2. Build standalone binary and .run installer
python3 phonespeaker-pc/build_linux.py

# 3. Run the generated .run installer
./PhoneSpeaker-v2-Linux-Installer.run

