#!/usr/bin/env bash
# ==============================================================================
# Display Sync Wrapper - Linux Sender Runner Script
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================"
echo "    Display Sync Wrapper - Linux Sender"
echo "======================================================"

# 1. Check for adb
if ! command -v adb &> /dev/null; then
    echo "[!] Warning: 'adb' is not installed or not in PATH."
    echo "    Install Android platform-tools via your package manager."
fi

# 2. Check for connected Android device
if command -v adb &> /dev/null; then
    DEVICES=$(adb devices | grep -v "List" | grep "device" || true)
    if [ -z "$DEVICES" ]; then
        echo "[!] Notice: No authorized Android device detected via 'adb devices'."
        echo "    Ensure your phone is plugged in with USB Debugging enabled."
    else
        echo "[+] Android device detected over ADB."
    fi
fi

# 3. Check Python dependencies
python3 -c "import numpy, gi, evdev" 2>/dev/null || {
    echo "[!] Missing one or more Python dependencies."
    echo "    Install system packages or pip packages:"
    echo "    - Fedora/Nobara: sudo dnf install python3-numpy python3-gobject python3-evdev gstreamer1-plugins-good pipewire-gstreamer"
    echo "    - Debian/Ubuntu: sudo apt install python3-numpy python3-gi python3-evdev gstreamer1.0-pipewire gstreamer1.0-plugins-good"
    echo "    - Or via pip: pip install -r requirements.txt"
}

# 4. Launch linux_sender.py with any user-provided arguments
echo "[*] Launching Mutter ScreenCast (PipeWire) Display Sync Sender..."
exec python3 "$SCRIPT_DIR/linux_sender.py" "$@"
