/* -*- mode: js2; indent-tabs-mode: nil; js2-basic-offset: 4 -*- */
'use strict';

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';
import Soup from 'gi://Soup?version=3.0';

import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as Slider from 'resource:///org/gnome/shell/ui/slider.js';

Gio._promisify(Soup.Session.prototype, 'send_and_read_async', 'send_and_read_finish');

const API_BASE = 'http://127.0.0.1:5556';
const POLL_INTERVAL_MS = 2000;
const DEBOUNCE_DELAY_MS = 80;

/**
 * Debounced Slider Menu Item with full-width track and clean Adwaita header.
 * Clamps values between [minVal, maxVal] and avoids relayout when unchanged.
 */
const DebouncedSliderMenuItem = GObject.registerClass(
class DebouncedSliderMenuItem extends PopupMenu.PopupBaseMenuItem {
    _init(iconName, labelText, minVal, maxVal, initialVal, unit, onValueChanged) {
        super._init({
            activate: false,
            reactive: true,
            hover: false,
            style_class: 'popup-menu-item ds-slider-item',
        });

        this._min = minVal;
        this._max = maxVal;
        this._unit = unit || '%';
        this._onValueChanged = onValueChanged;
        this._isInternalUpdate = false;
        this._isDragging = false;
        this._debounceTimer = null;
        this._pendingVal = null;
        this._currentVal = Math.round(initialVal);

        const container = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: 'ds-slider-container',
        });

        // 1. Header row: [Icon + Title] on left, [Percentage %] on right
        const headerRow = new St.BoxLayout({
            style_class: 'ds-slider-header',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
            reactive: false,
        });

        const leftBox = new St.BoxLayout({
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'ds-slider-left-box',
        });

        if (iconName) {
            const icon = new St.Icon({
                icon_name: iconName,
                style_class: 'popup-menu-icon ds-slider-icon',
                y_align: Clutter.ActorAlign.CENTER,
            });
            leftBox.add_child(icon);
        }

        const label = new St.Label({
            text: labelText,
            style_class: 'popup-menu-label ds-slider-title',
            y_align: Clutter.ActorAlign.CENTER,
        });
        leftBox.add_child(label);
        headerRow.add_child(leftBox);

        this._valueLabel = new St.Label({
            text: `${this._currentVal}${this._unit}`,
            style_class: 'ds-slider-value',
            x_align: Clutter.ActorAlign.END,
            y_align: Clutter.ActorAlign.CENTER,
        });
        headerRow.add_child(this._valueLabel);
        container.add_child(headerRow);

        // 2. Full-width slider row below the header
        const norm = (initialVal - this._min) / (this._max - this._min);
        this._slider = new Slider.Slider(Math.max(0, Math.min(1, norm)));
        this._slider.x_expand = true;
        this._slider.x_align = Clutter.ActorAlign.FILL;
        this._slider.y_align = Clutter.ActorAlign.CENTER;
        this._slider.style_class = 'slider ds-slider';
        container.add_child(this._slider);

        this.add_child(container);

        this._slider.connect('notify::value', () => {
            const val = Math.round(this._min + this._slider.value * (this._max - this._min));
            if (val === this._currentVal) return;
            this._currentVal = val;
            this._valueLabel.set_text(`${val}${this._unit}`);

            if (!this._isInternalUpdate) {
                this._queueDispatch(val);
            }
        });

        this._slider.connect('drag-begin', () => {
            this._isDragging = true;
        });

        this._slider.connect('drag-end', () => {
            this._isDragging = false;
            this._flush();
        });
    }

    _queueDispatch(val) {
        this._pendingVal = val;
        if (this._debounceTimer) {
            GLib.source_remove(this._debounceTimer);
            this._debounceTimer = null;
        }
        this._debounceTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, DEBOUNCE_DELAY_MS, () => {
            this._debounceTimer = null;
            this._flush();
            return GLib.SOURCE_REMOVE;
        });
    }

    _flush() {
        if (this._debounceTimer) {
            GLib.source_remove(this._debounceTimer);
            this._debounceTimer = null;
        }
        if (this._pendingVal !== null) {
            const toSend = this._pendingVal;
            this._pendingVal = null;
            if (this._onValueChanged) {
                this._onValueChanged(toSend);
            }
        }
    }

    setValue(val) {
        // Prevent UI feedback loops while actively dragging or during debounce window
        if (this._isDragging || this._debounceTimer !== null) return;
        const clamped = Math.max(this._min, Math.min(this._max, Math.round(val)));
        if (clamped === this._currentVal) return;

        this._isInternalUpdate = true;
        this._currentVal = clamped;
        const norm = (clamped - this._min) / (this._max - this._min);
        this._slider.value = Math.max(0, Math.min(1, norm));
        this._valueLabel.set_text(`${clamped}${this._unit}`);
        this._isInternalUpdate = false;
    }

    getValue() {
        return this._currentVal;
    }

    destroy() {
        if (this._debounceTimer) {
            GLib.source_remove(this._debounceTimer);
            this._debounceTimer = null;
        }
        super.destroy();
    }
});

/**
 * Main Top Panel Button & Dropdown Menu for Display Sync.
 */
const DisplaySyncIndicator = GObject.registerClass(
class DisplaySyncIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'Display Sync', false);
        this._extension = extension;
        this._cancellable = new Gio.Cancellable();
        this._httpSession = new Soup.Session();
        this._subprocess = null;
        this._pollTimer = null;
        this._isInternalUpdate = false;
        this._lastError = null;

        // Top bar icon: single centered symbolic icon matching native GNOME panel icons
        this._icon = new St.Icon({
            icon_name: 'display-brightness-symbolic',
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);

        this._buildMenu();

        // Prevent menu from closing when modifying settings or interacting with menu items
        this.menu.itemActivated = () => {};
        if (this._hzSubMenu && this._hzSubMenu.menu) {
            this._hzSubMenu.menu.itemActivated = () => {};
        }

        // Top bar scroll-wheel shortcut: scroll over icon to adjust brightness +/- 5%
        this.connect('scroll-event', (_actor, event) => {
            const direction = event.get_scroll_direction();
            let delta = 0;
            if (direction === Clutter.ScrollDirection.UP) {
                delta = 5;
            } else if (direction === Clutter.ScrollDirection.DOWN) {
                delta = -5;
            } else if (direction === Clutter.ScrollDirection.SMOOTH) {
                const [, dy] = event.get_scroll_delta();
                delta = dy < 0 ? 5 : -5;
            }
            if (delta !== 0) {
                this._stepBrightness(delta);
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        // Optimization: poll status only when the dropdown menu is open
        this.menu.connect('open-state-changed', (_menu, isOpen) => {
            if (isOpen) {
                this._startPolling();
            } else {
                this._stopPolling();
            }
        });
    }

    _stepBrightness(delta) {
        if (!this._brightnessSlider) return;
        const cur = this._brightnessSlider.getValue();
        const next = Math.min(100, Math.max(10, cur + delta));
        if (cur !== next) {
            this._brightnessSlider.setValue(next);
            this._sendApiRequest(`/brightness?val=${next}`);
        }
    }

    _buildMenu() {
        // 1. Master Service Toggle Switch
        this._serviceSwitch = new PopupMenu.PopupSwitchMenuItem('Display Sync Service', false);
        this._serviceSwitch.connect('toggled', (_item, state) => {
            if (this._isInternalUpdate) return;
            if (state) {
                this.startService();
            } else {
                this.stopService();
            }
        });
        this.menu.addMenuItem(this._serviceSwitch);

        // 2. Pre-flight & Runtime Status Box
        const statusItem = new PopupMenu.PopupBaseMenuItem({ activate: false, reactive: false });
        const statusBox = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: 'ds-status-box',
        });

        this._statusLabel = new St.Label({
            text: 'Service: Stopped',
            style_class: 'ds-status-title',
        });
        this._modeLabel = new St.Label({
            text: 'Mode: Idle',
            style_class: 'ds-subtext',
        });
        this._phoneLabel = new St.Label({
            text: 'Phone: Disconnected',
            style_class: 'ds-subtext',
        });
        this._configLabel = new St.Label({
            text: 'Config: Checking...',
            style_class: 'ds-subtext',
        });

        statusBox.add_child(this._statusLabel);
        statusBox.add_child(this._modeLabel);
        statusBox.add_child(this._phoneLabel);
        statusBox.add_child(this._configLabel);
        statusItem.add_child(statusBox);
        this.menu.addMenuItem(statusItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // 3. Bulb Power Toggle
        this._powerSwitch = new PopupMenu.PopupSwitchMenuItem('Bulb Power', true);
        this._powerSwitch.connect('toggled', (_item, state) => {
            if (this._isInternalUpdate) return;
            this._sendApiRequest(`/power_toggle?val=${state ? '1' : '0'}`);
        });
        this.menu.addMenuItem(this._powerSwitch);

        // 4. Reconnect Bulb Button (Adwaita symbolic icon, clean text, no emojis)
        this._reconnectBtn = new PopupMenu.PopupImageMenuItem('Reconnect Bulb', 'view-refresh-symbolic');
        this._reconnectBtn.connect('activate', async () => {
            this._reconnectBtn.label.text = 'Connecting...';
            const res = await this._sendApiRequest('/reconnect');
            if (res && res.status === 'ok') {
                this._reconnectBtn.label.text = 'Connected';
            } else {
                this._reconnectBtn.label.text = 'Connection Failed';
            }
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2000, () => {
                this._reconnectBtn.label.text = 'Reconnect Bulb';
                return GLib.SOURCE_REMOVE;
            });
            this._pollStatus();
        });
        this.menu.addMenuItem(this._reconnectBtn);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // 5. Brightness Slider (10% - 100%, full-width track, debounced ~80ms)
        this._brightnessSlider = new DebouncedSliderMenuItem(
            'display-brightness-symbolic',
            'Brightness',
            10,
            100,
            70,
            '%',
            (val) => this._sendApiRequest(`/brightness?val=${val}`)
        );
        this.menu.addMenuItem(this._brightnessSlider);

        // 6. Responsiveness (Smoothing Alpha) Slider (30% - 95%, full-width track, debounced ~80ms)
        this._smoothingSlider = new DebouncedSliderMenuItem(
            'preferences-system-symbolic',
            'Responsiveness',
            30,
            95,
            60,
            '%',
            (val) => this._sendApiRequest(`/smoothing?val=${val}`)
        );
        this.menu.addMenuItem(this._smoothingSlider);

        // 7. Refresh Rate Submenu (Header reflects active rate, e.g. "Refresh Rate (18 Hz)")
        this._hzSubMenu = new PopupMenu.PopupSubMenuMenuItem('Refresh Rate');
        this._hzItems = new Map();
        const hzOptions = [10, 12, 14, 15, 18, 20];
        for (const hz of hzOptions) {
            const item = new PopupMenu.PopupMenuItem(`${hz} Hz`);
            item.connect('activate', () => {
                this._setRefreshHz(hz);
            });
            this._hzSubMenu.menu.addMenuItem(item);
            this._hzItems.set(hz, item);
        }
        this.menu.addMenuItem(this._hzSubMenu);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // 8. Ethernet Only (Isolate Hotspot) Toggle
        this._ethernetSwitch = new PopupMenu.PopupSwitchMenuItem('Ethernet Only (Isolate Hotspot)', false);
        this._ethernetSwitch.connect('toggled', (_item, state) => {
            if (this._isInternalUpdate) return;
            this._sendApiRequest(`/ethernet_toggle?val=${state ? '1' : '0'}`);
        });
        this.menu.addMenuItem(this._ethernetSwitch);

        // 9. Normal White Mode Toggle
        this._whiteSwitch = new PopupMenu.PopupSwitchMenuItem('Normal White Mode', false);
        this._whiteSwitch.connect('toggled', (_item, state) => {
            if (this._isInternalUpdate) return;
            this._sendApiRequest(`/white_mode?val=${state ? '1' : '0'}`);
        });
        this.menu.addMenuItem(this._whiteSwitch);

        // 10. Dynamic Bulb Shifts Toggle
        this._dynamicSwitch = new PopupMenu.PopupSwitchMenuItem('Dynamic Bulb Shifts', true);
        this._dynamicSwitch.connect('toggled', (_item, state) => {
            if (this._isInternalUpdate) return;
            this._sendApiRequest(`/dynamic?val=${state ? '1' : '0'}`);
        });
        this.menu.addMenuItem(this._dynamicSwitch);

        // 11. Phone Torch on Rumble Toggle
        this._torchSwitch = new PopupMenu.PopupSwitchMenuItem('Phone Torch on Rumble', true);
        this._torchSwitch.connect('toggled', (_item, state) => {
            if (this._isInternalUpdate) return;
            this._sendApiRequest(`/phone_flash?val=${state ? '1' : '0'}`);
        });
        this.menu.addMenuItem(this._torchSwitch);
    }

    _setRefreshHz(hz) {
        this._sendApiRequest(`/refresh_hz?val=${hz}`);
        this._hzSubMenu.label.text = `Refresh Rate (${hz} Hz)`;
        for (const [val, item] of this._hzItems) {
            item.setOrnament(val === hz ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
        }
    }

    _checkConfigPath() {
        const userCfg = GLib.build_filenamev([GLib.get_home_dir(), '.config', 'display-sync', 'tuya_config.json']);
        const extCfg = GLib.build_filenamev([this._extension.path, 'tuya_config.json']);
        const homeCfg = GLib.build_filenamev([GLib.get_home_dir(), 'tuya_config.json']);

        if (GLib.file_test(userCfg, GLib.FileTest.EXISTS)) {
            return { path: '~/.config/display-sync/tuya_config.json', ok: true };
        } else if (GLib.file_test(extCfg, GLib.FileTest.EXISTS)) {
            return { path: 'extension dir (bundled)', ok: true };
        } else if (GLib.file_test(homeCfg, GLib.FileTest.EXISTS)) {
            return { path: '~/tuya_config.json', ok: true };
        }
        return { path: 'Not found', ok: false };
    }

    startService() {
        if (this._subprocess) return;

        this._lastError = null;
        const cfgInfo = this._checkConfigPath();
        this._configLabel.set_text(`Config: ${cfgInfo.path}`);
        if (!cfgInfo.ok) {
            this._configLabel.set_style_class_name('ds-subtext-error');
        } else {
            this._configLabel.set_style_class_name('ds-subtext');
        }

        // Locate linux_sender.py inside the extension directory
        const scriptPath = GLib.build_filenamev([this._extension.path, 'linux_sender.py']);
        if (!GLib.file_test(scriptPath, GLib.FileTest.EXISTS)) {
            this._statusLabel.set_text('Service: Script missing');
            this._modeLabel.set_text(`linux_sender.py not found in ${this._extension.path}`);
            this._serviceSwitch.setToggleState(false);
            return;
        }

        try {
            this._subprocess = new Gio.Subprocess({
                argv: ['python3', scriptPath],
                flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            });
            this._subprocess.init(this._cancellable);

            this._isInternalUpdate = true;
            this._serviceSwitch.setToggleState(true);
            this._isInternalUpdate = false;

            this._statusLabel.set_text('Service: Starting...');
            this._modeLabel.set_text('Launching sender pipeline...');

            // Monitor stderr asynchronously for missing dependencies or crash output
            const stderrStream = new Gio.DataInputStream({
                base_stream: this._subprocess.get_stderr_pipe()
            });
            this._readStderr(stderrStream);

            // Watch process exit
            this._subprocess.wait_async(this._cancellable, (proc, res) => {
                let exitStatus = 0;
                try {
                    proc.wait_finish(res);
                    exitStatus = proc.get_exit_status();
                } catch (e) {
                    exitStatus = -1;
                }
                this._subprocess = null;
                this._onProcessExited(exitStatus);
            });

            if (this.menu.isOpen) {
                this._startPolling();
            }
        } catch (e) {
            this._subprocess = null;
            this._isInternalUpdate = true;
            this._serviceSwitch.setToggleState(false);
            this._isInternalUpdate = false;
            this._statusLabel.set_text('Service: Failed to Start');
            this._modeLabel.set_text(e.message || String(e));
        }
    }

    _readStderr(stream) {
        stream.read_line_async(GLib.PRIORITY_DEFAULT, this._cancellable, (s, res) => {
            try {
                const [line] = s.read_line_finish_utf8(res);
                if (line !== null) {
                    if (line.includes('Missing one or more Python dependencies') ||
                        line.includes('ModuleNotFoundError') ||
                        line.includes('ImportError') ||
                        line.includes('Traceback')) {
                        this._lastError = line;
                        this._modeLabel.set_text(line);
                        this._modeLabel.set_style_class_name('ds-subtext-error');
                    }
                    this._readStderr(s);
                }
            } catch (e) {}
        });
    }

    stopService() {
        this._stopPolling();
        if (!this._subprocess) {
            this._isInternalUpdate = true;
            this._serviceSwitch.setToggleState(false);
            this._isInternalUpdate = false;
            this._statusLabel.set_text('Service: Stopped');
            this._modeLabel.set_text('Sender daemon idle');
            return;
        }

        try {
            // Signal 15 = SIGTERM, triggers clean shutdown & cleanup_phone_and_exit()
            this._subprocess.send_signal(15);
        } catch (e) {
            try {
                this._subprocess.force_exit();
            } catch (_) {}
        }

        this._statusLabel.set_text('Service: Stopping...');
    }

    _onProcessExited(exitStatus) {
        this._isInternalUpdate = true;
        this._serviceSwitch.setToggleState(false);
        this._isInternalUpdate = false;
        this._stopPolling();

        if (this._lastError) {
            this._statusLabel.set_text('Service: Stopped with Error');
            this._modeLabel.set_text(this._lastError);
            this._modeLabel.set_style_class_name('ds-subtext-error');
        } else if (exitStatus !== 0) {
            this._statusLabel.set_text('Service: Stopped');
            this._modeLabel.set_text(`Process exited with code ${exitStatus}`);
            this._modeLabel.set_style_class_name('ds-subtext');
        } else {
            this._statusLabel.set_text('Service: Stopped');
            this._modeLabel.set_text('Clean exit');
            this._modeLabel.set_style_class_name('ds-subtext');
        }
    }

    _startPolling() {
        this._stopPolling();
        this._pollStatus();
        this._pollTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_INTERVAL_MS, () => {
            this._pollStatus();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopPolling() {
        if (this._pollTimer) {
            GLib.source_remove(this._pollTimer);
            this._pollTimer = null;
        }
    }

    async _sendApiRequest(path) {
        try {
            const uri = GLib.Uri.parse(`${API_BASE}${path}`, GLib.UriFlags.NONE);
            const msg = Soup.Message.new_from_uri('GET', uri);
            const bytes = await this._httpSession.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, this._cancellable);
            if (msg.get_status() === 200 && bytes) {
                const text = new TextDecoder().decode(bytes.get_data());
                return JSON.parse(text);
            }
        } catch (e) {}
        return null;
    }

    async _pollStatus() {
        const data = await this._sendApiRequest('/status');
        if (!data) {
            if (this._subprocess) {
                this._statusLabel.set_text('Service: Connecting...');
            } else {
                this._statusLabel.set_text('Service: Stopped');
            }
            return;
        }

        this._updateUiFromData(data);
    }

    _updateUiFromData(data) {
        this._isInternalUpdate = true;

        // 1. Status labels
        this._statusLabel.set_text('Service: Running');
        this._modeLabel.set_text(`Mode: ${data.mode || 'Active'}`);
        this._modeLabel.set_style_class_name('ds-subtext');

        const adbText = data.adb_status === 'Connected' ? 'Connected' : 'Disconnected';
        this._phoneLabel.set_text(`Phone: ${adbText}`);

        const cfgInfo = this._checkConfigPath();
        this._configLabel.set_text(`Config: ${cfgInfo.path}`);
        if (!cfgInfo.ok) {
            this._configLabel.set_style_class_name('ds-subtext-error');
        } else {
            this._configLabel.set_style_class_name('ds-subtext');
        }

        // 2. Service switch state
        this._serviceSwitch.setToggleState(true);

        // 3. Power switch
        if (data.power !== undefined) {
            this._powerSwitch.setToggleState(data.power);
        }

        // 4. Brightness slider
        if (data.brightness !== undefined) {
            this._brightnessSlider.setValue(data.brightness);
        }

        // 5. Smoothing slider
        if (data.smoothing_alpha !== undefined) {
            this._smoothingSlider.setValue(Math.round(data.smoothing_alpha * 100));
        }

        // 6. Refresh rate selection & Header text
        if (data.refresh_hz !== undefined) {
            const roundedHz = Math.round(data.refresh_hz);
            this._hzSubMenu.label.text = `Refresh Rate (${roundedHz} Hz)`;
            for (const [hz, item] of this._hzItems) {
                item.setOrnament(hz === roundedHz ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
            }
        }

        // 7. Toggles
        if (data.ethernet_only !== undefined) {
            this._ethernetSwitch.setToggleState(data.ethernet_only);
        }
        if (data.white_mode !== undefined) {
            this._whiteSwitch.setToggleState(data.white_mode);
        }
        if (data.dynamic !== undefined) {
            this._dynamicSwitch.setToggleState(data.dynamic);
        }
        if (data.phone_flash !== undefined) {
            this._torchSwitch.setToggleState(data.phone_flash);
        }

        this._isInternalUpdate = false;
    }

    destroy() {
        this._stopPolling();
        if (this._cancellable) {
            this._cancellable.cancel();
        }
        if (this._httpSession) {
            this._httpSession.abort();
        }
        if (this._brightnessSlider) {
            this._brightnessSlider.destroy();
        }
        if (this._smoothingSlider) {
            this._smoothingSlider.destroy();
        }
        super.destroy();
    }
});

/**
 * Extension Entry Point.
 */
export default class DisplaySyncExtension extends Extension {
    enable() {
        this._indicator = new DisplaySyncIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);

        // Auto-start Display Sync service at login
        this._indicator.startService();
    }

    disable() {
        if (this._indicator) {
            this._indicator.stopService();
            this._indicator.destroy();
            this._indicator = null;
        }
    }
}
