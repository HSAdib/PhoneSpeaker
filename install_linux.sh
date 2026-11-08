#!/usr/bin/env bash
# install_linux.sh — One-line instant installer for PhoneSpeaker v2.0 Studio (Arch, Ubuntu, Mint, Debian, Fedora)
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
echo "  PhoneSpeaker v2.0 Studio — Linux Installer"
echo "=========================================================="

INSTALLER="/tmp/PhoneSpeaker-v2-Linux-Installer.run"
rm -f "$INSTALLER"

# Check if we have the installer locally or need to download from GitHub
if [ -f "PhoneSpeaker-v2-Linux-Installer.run" ]; then
    echo "Using local PhoneSpeaker-v2-Linux-Installer.run..."
    cp -f "PhoneSpeaker-v2-Linux-Installer.run" "$INSTALLER"
else
    echo "Downloading PhoneSpeaker v2.0 Linux installer from GitHub..."
    PRIMARY_URL="https://github.com/HSAdib/PhoneSpeaker/releases/latest/download/PhoneSpeaker-v2-Linux-Installer.run"
    FALLBACK_URL="https://github.com/HSAdib/PhoneSpeaker/releases/download/v2.0.0/PhoneSpeaker-v2-Linux-Installer.run"
    RAW_URL="https://raw.githubusercontent.com/HSAdib/PhoneSpeaker/main/PhoneSpeaker-v2-Linux-Installer.run"

    if command -v curl >/dev/null 2>&1; then
        curl -sSL -f -o "$INSTALLER" "$PRIMARY_URL" || curl -sSL -f -o "$INSTALLER" "$FALLBACK_URL" || curl -sSL -f -o "$INSTALLER" "$RAW_URL"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --show-progress -O "$INSTALLER" "$PRIMARY_URL" || wget -q --show-progress -O "$INSTALLER" "$FALLBACK_URL" || wget -q --show-progress -O "$INSTALLER" "$RAW_URL"
    else
        echo "Error: neither curl nor wget was found. Please install curl or wget."
        exit 1
    fi
fi

if [ ! -s "$INSTALLER" ]; then
    echo "ERROR: Failed to download PhoneSpeaker-v2-Linux-Installer.run"
    exit 1
fi

chmod +x "$INSTALLER"
echo "Executing installer..."
"$INSTALLER"
rm -f "$INSTALLER"
