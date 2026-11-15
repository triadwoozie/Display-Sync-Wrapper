#!/bin/bash
set -e

EXT_UUID="display-sync@triadwoozie"
TARGET_DIR="${HOME}/.local/share/gnome-shell/extensions/${EXT_UUID}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing Display Sync GNOME Shell Extension..."
mkdir -p "$TARGET_DIR"
cp -r "$SCRIPT_DIR/extension/"* "$TARGET_DIR/"
chmod +x "$TARGET_DIR/linux_sender.py"

# Ensure ~/.config/display-sync exists and copy template/existing config if not present
mkdir -p "${HOME}/.config/display-sync"
if [ ! -f "${HOME}/.config/display-sync/tuya_config.json" ]; then
    if [ -f "$SCRIPT_DIR/tuya_config.json" ]; then
        cp "$SCRIPT_DIR/tuya_config.json" "${HOME}/.config/display-sync/tuya_config.json"
        chmod 600 "${HOME}/.config/display-sync/tuya_config.json"
    elif [ -f "$SCRIPT_DIR/tuya_config.example.json" ]; then
        cp "$SCRIPT_DIR/tuya_config.example.json" "${HOME}/.config/display-sync/tuya_config.json"
        chmod 600 "${HOME}/.config/display-sync/tuya_config.json"
    fi
fi

echo "[+] Extension installed to $TARGET_DIR"
echo "[+] Enabling extension via gnome-extensions..."
gnome-extensions enable "$EXT_UUID" 2>/dev/null || true
echo "[✓] Installation complete! Log out and back in (or reload GNOME Shell) if installing for the first time."
