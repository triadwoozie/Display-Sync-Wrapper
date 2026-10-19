#!/bin/bash
# ==============================================================================
# InputLeap Client & Display Sync Wrapper Linux Sender (Startup Runner)
# ==============================================================================

# 1. Start InputLeap client in background if not already running
pgrep -x input-leapc >/dev/null || input-leapc --name compooter 10.42.0.183 &

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================"
echo "    Display Sync Wrapper - Linux Sender"
echo "======================================================"

# 2. Check for adb
if ! command -v adb &> /dev/null; then
    echo "[!] Warning: 'adb' is not installed or not in PATH."
    echo "    Install Android platform-tools via your package manager."
fi

# 3. Check for connected Android device
if command -v adb &> /dev/null; then
    DEVICES=$(adb devices | grep -v "List" | grep "device" || true)
    if [ -z "$DEVICES" ]; then
        echo "[!] Notice: No authorized Android device detected via 'adb devices'."
        echo "    Ensure your phone is plugged in with USB Debugging enabled."
    else
        echo "[+] Android device detected over ADB."
    fi
fi

# 4. Check Tuya Cloud Configuration
if [ -f "$SCRIPT_DIR/tuya_config.json" ]; then
    echo "[+] Tuya Cloud configuration detected (tuya_config.json)."
else
    echo "[!] Notice: tuya_config.json not found (Tuya sync will be skipped or run in phone/rumble-only mode)."
fi

# 5. Check Python dependencies
python3 -c "import numpy, gi, evdev, requests, tinytuya" 2>/dev/null || {
    echo "[!] Missing one or more Python dependencies."
    echo "    Install system packages or pip packages:"
    echo "    - Fedora/Nobara: sudo dnf install python3-numpy python3-gobject python3-evdev python3-requests gstreamer1-plugins-good pipewire-gstreamer"
    echo "    - Debian/Ubuntu: sudo apt install python3-numpy python3-gi python3-evdev python3-requests gstreamer1.0-pipewire gstreamer1.0-plugins-good"
    echo "    - Or via pip: pip install -r requirements.txt"
}

# 6. Launch linux_sender.py with any user-provided arguments
echo "[*] Launching Mutter ScreenCast (PipeWire) Display Sync Sender..."
exec python3 "$SCRIPT_DIR/linux_sender.py" "$@"
