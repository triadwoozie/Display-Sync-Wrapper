#!/usr/bin/env python3
"""
Mutter ScreenCast (PipeWire) Display Sync, Controller Rumble Interceptor & Tuya Bulb Controller
-------------------------------------------------------------------------------------------------
- Captures desktop color downscaled to 16x9 at the source (Mutter GPU compositor pass).
- Phone Sync (ADB USB tcp:5555): sub-5ms low-latency ambient color + camera torch flash on rumble.
- Tuya Bulb Sync: Exponential smoothing (soft transitions), rate-limited at 2 Hz, 30-100% brightness.
- Web Brightness Slider: Live dark-mode slider at http://localhost:5556.
- Fault-tolerant: Phone or Tuya bulb not being connected/configured never blocks or halts the pipeline.
- Clean shutdown: On Ctrl-C or exit, closes app, sleeps phone, and shuts down pipeline.
"""

import sys
import os
import time
import json
import hmac
import socket
import signal
import atexit
import hashlib
import colorsys
import argparse
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests
import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gio, GLib, Gst

try:
    import evdev
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

try:
    import tinytuya
    HAS_TINYTUYA = True
except ImportError:
    HAS_TINYTUYA = False

HOST = "127.0.0.1"
PORT = 5555
FPS_TARGET = 30

WIDTH = 16
HEIGHT = 9

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tuya_config.json")
EMPTY_BODY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

TUYA_REGIONS = {
    "in": "https://openapi.tuyain.com",
    "us": "https://openapi.tuyaus.com",
    "us-east": "https://openapi-ueaz.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "eu-west": "https://openapi-weaz.tuyaeu.com",
    "cn": "https://openapi.tuyacn.com"
}

current_vibration = 0
vibration_lock = threading.Lock()
is_cleaning_up = False


# ==============================================================================
# Phone (ADB) Utilities
# ==============================================================================

def unlock_and_launch_phone():
    """Wakes the phone, dismisses keyguard, and launches the app in fullscreen mode."""
    try:
        # 1. Wake screen if asleep (KEYCODE_WAKEUP = 224)
        subprocess.run(["adb", "shell", "input", "keyevent", "224"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 2. Dismiss keyguard cleanly via KEYCODE_MENU (82)
        subprocess.run(["adb", "shell", "wm", "dismiss-keyguard"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["adb", "shell", "input", "keyevent", "82"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        # 3. Launch the app in foreground with fullscreen intent extra
        subprocess.run([
            "adb", "shell", "am", "start",
            "-W",
            "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER",
            "-n", "com.flashlight.torch/.MainActivity",
            "--ez", "hide_controls", "true"
        ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 4. Broadcast HIDE_UI to ensure fullscreen
        subprocess.run(["adb", "shell", "am", "broadcast", "-a", "com.flashlight.torch.HIDE_UI"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[+] Phone wake & launch intent sent.")
    except Exception as e:
        print(f"[!] Notice: ADB unlock/launch skipped (phone not connected): {e}")


def dismiss_initial_ui():
    """Ensures controls are hidden in the app upon connection."""
    try:
        subprocess.run(["adb", "shell", "am", "broadcast", "-a", "com.flashlight.torch.HIDE_UI"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def cleanup_phone_and_exit(sync_instance=None):
    """Turns off phone screen, closes app, stops pipeline, and exits cleanly."""
    global is_cleaning_up
    if is_cleaning_up:
        return
    is_cleaning_up = True

    print("\n[*] Shutting down: closing app and turning off phone screen...")
    try:
        subprocess.run(["adb", "shell", "am", "force-stop", "com.flashlight.torch"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["adb", "shell", "input", "keyevent", "223"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    if sync_instance:
        sync_instance.stop()


def setup_adb_forward(port: int):
    """Sets up ADB forward once to route traffic over USB debug link."""
    try:
        subprocess.run(["adb", "forward", f"tcp:{port}", f"tcp:{port}"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ==============================================================================
# Controller Force-Feedback (Rumble) Interceptor
# ==============================================================================

def monitor_controller_vibration(device_path=None, simulate=False):
    """Listens for gamepad rumble events (EV_FF) with hotplug support and pulse decay."""
    global current_vibration

    if simulate:
        print("[*] Simulation mode: pulsing vibration every 4s to test torch.")
        while True:
            time.sleep(3.0)
            with vibration_lock:
                current_vibration = 255
            time.sleep(1.0)
            with vibration_lock:
                current_vibration = 0
        return

    if not HAS_EVDEV:
        return

    import select
    last_vibe_time = 0

    while True:
        target_devices = []
        try:
            paths = evdev.list_devices() if not device_path else [device_path]
            for p in paths:
                try:
                    dev = evdev.InputDevice(p)
                    if evdev.ecodes.EV_FF in dev.capabilities():
                        target_devices.append(dev)
                except Exception:
                    pass
        except Exception:
            pass

        if not target_devices:
            time.sleep(2.0)
            continue

        print(f"[+] Controller rumble monitor active on: {[d.name for d in target_devices]}")

        while True:
            try:
                r, _, _ = select.select(target_devices, [], [], 0.1)
                now = time.time()
                if r:
                    for dev in r:
                        for ev in dev.read():
                            if ev.type == evdev.ecodes.EV_FF:
                                if ev.value > 0:
                                    last_vibe_time = now
                                    with vibration_lock:
                                        current_vibration = 255
                                else:
                                    with vibration_lock:
                                        current_vibration = 0

                # Auto-decay rumble pulse after 350ms if no continuous events
                if current_vibration > 0 and (now - last_vibe_time > 0.35):
                    with vibration_lock:
                        current_vibration = 0

            except (OSError, IOError):
                # Device unplugged or state reset
                time.sleep(1.0)
                break


# ==============================================================================
# ==============================================================================
# Tuya Local (Wi-Fi Persistent Socket) & Cloud API Smart Bulb Synchronizer
# ==============================================================================

class TuyaLocalDevice:
    """
    Controls Tuya bulb directly over local Wi-Fi router using tinytuya.BulbDevice
    with a persistent TCP socket (~10-15 Hz, sub-10ms latency).
    """
    def __init__(self, device_id: str, local_ip: str, local_key: str, version: float = 3.5):
        self.device_id = device_id.strip()
        self.local_ip = local_ip.strip()
        self.local_key = local_key.strip()
        self.version = float(version)
        self.bulb = None
        self.is_connected = False
        self.lock = threading.Lock()
        self.last_attempt_time = 0

    def connect(self) -> bool:
        if not HAS_TINYTUYA or not self.local_ip or not self.local_key:
            return False
        with self.lock:
            now = time.time()
            if now - self.last_attempt_time < 3.0:
                return False
            self.last_attempt_time = now

            try:
                # Persistent socket direct connection to local bulb on port 6668
                self.bulb = tinytuya.BulbDevice(
                    dev_id=self.device_id,
                    address=self.local_ip,
                    local_key=self.local_key,
                    version=self.version,
                    persist=True
                )
                self.bulb.set_version(self.version)
                self.bulb.set_socketPersistent(True)
                # Ensure bulb is powered on in colour mode
                self.bulb.set_multiple_values({'20': True, '21': 'colour'}, nowait=True)
                self.is_connected = True
                return True
            except Exception:
                self.is_connected = False
                self.bulb = None
                return False

    def send_hsv(self, h: int, s: int, v: int) -> bool:
        """Sends HSV color directly over local Wi-Fi TCP socket (non-blocking nowait=True)."""
        if not self.is_connected or not self.bulb:
            if not self.connect():
                return False

        hex_val = f"{int(h):04x}{int(s):04x}{int(v):04x}"
        with self.lock:
            try:
                self.bulb.set_value('24', hex_val, nowait=True)
                return True
            except Exception:
                self.is_connected = False
                try:
                    if self.bulb:
                        self.bulb.close()
                except Exception:
                    pass
                self.bulb = None
                return False

    def close(self):
        with self.lock:
            if self.bulb:
                try:
                    self.bulb.close()
                except Exception:
                    pass
                self.bulb = None
            self.is_connected = False


class TuyaCloudClient:
    """Fallback Tuya Cloud OpenAPI client (rate-limited ~2 Hz)."""
    def __init__(self, access_id: str, access_secret: str, device_id: str, region: str = "in"):
        self.access_id = access_id.strip()
        self.access_secret = access_secret.strip()
        self.device_id = device_id.strip()

        region_clean = region.strip().lower()
        self.base_url = TUYA_REGIONS.get(region_clean, region.strip())
        if not self.base_url.startswith("http"):
            self.base_url = f"https://{self.base_url}"

        self.token = None
        self.token_expire_time = 0
        self.token_lock = threading.Lock()
        self.use_legacy_code = False
        self.initialized = False

    def _calc_sign(self, string_to_sign: str) -> str:
        return hmac.new(
            self.access_secret.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha256
        ).hexdigest().upper()

    def get_token(self, force_refresh: bool = False) -> str:
        with self.token_lock:
            now = time.time()
            if self.token and not force_refresh and (now < self.token_expire_time - 120):
                return self.token

            path = "/v1.0/token?grant_type=1"
            t = str(int(now * 1000))
            string_to_sign = f"{self.access_id}{t}GET\n{EMPTY_BODY_HASH}\n\n{path}"
            sign = self._calc_sign(string_to_sign)

            headers = {
                "client_id": self.access_id,
                "t": t,
                "sign_method": "HMAC-SHA256",
                "sign": sign
            }

            try:
                resp = requests.get(f"{self.base_url}{path}", headers=headers, timeout=5)
                data = resp.json()
                if data.get("success"):
                    self.token = data["result"]["access_token"]
                    expires_in = data["result"].get("expire_time", 7200)
                    self.token_expire_time = now + expires_in
                    return self.token
                else:
                    raise RuntimeError(f"Tuya Auth Error: {data.get('msg')} (code: {data.get('code')})")
            except Exception as e:
                raise RuntimeError(f"Tuya Cloud connection error ({self.base_url}): {e}")

    def send_color(self, h: int, s: int, v: int) -> bool:
        """Sends HSV color to bulb via cloud OpenAPI."""
        try:
            token = self.get_token()
        except Exception:
            return False

        now = time.time()
        t = str(int(now * 1000))

        commands = []
        if not self.initialized:
            commands.append({"code": "switch_led", "value": True})
            commands.append({"code": "work_mode", "value": "colour"})
            self.initialized = True

        if not self.use_legacy_code:
            commands.append({
                "code": "colour_data_v2",
                "value": {"h": int(h), "s": int(s), "v": int(v)}
            })
        else:
            hex_val = f"{int(h):04x}{int(s):04x}{int(v):04x}"
            commands.append({
                "code": "colour_data",
                "value": hex_val
            })

        payload = {"commands": commands}
        body_str = json.dumps(payload, separators=(',', ':'))
        body_hash = hashlib.sha256(body_str.encode('utf-8')).hexdigest()

        path = f"/v1.0/iot-03/devices/{self.device_id}/commands"
        string_to_sign = f"{self.access_id}{token}{t}POST\n{body_hash}\n\n{path}"
        sign = self._calc_sign(string_to_sign)

        headers = {
            "client_id": self.access_id,
            "access_token": token,
            "t": t,
            "sign_method": "HMAC-SHA256",
            "sign": sign,
            "Content-Type": "application/json"
        }

        try:
            resp = requests.post(f"{self.base_url}{path}", headers=headers, data=body_str, timeout=3)
            data = resp.json()
            if data.get("success"):
                return True
            if data.get("code") in (1010, 1011, 1012):
                self.get_token(force_refresh=True)
                return False
            if not self.use_legacy_code and data.get("code") in (1106, 2001, 2008):
                self.use_legacy_code = True
                return self.send_color(h, s, v)
            return False
        except Exception:
            return False


class TuyaBulbSync:
    """
    Tuya Smart Bulb Controller:
    - Direct Wi-Fi local socket via tinytuya.BulbDevice (persistent TCP, ~12 Hz)
    - Full color range expansion: saturation weighting so colors pop and never wash out to white
    - Dynamic brightness shifts: bulb scales dynamically with screen luminance (clamped 30%-100%)
    - Smooth gradient easing: shortest circular path Hue and linear Saturation/Value interpolation
    - Fallback: Tuya Cloud OpenAPI if local network is unavailable
    - Web slider server: real-time brightness and dynamic mode control at http://localhost:5556
    """
    def __init__(self, config_path: str = CONFIG_FILE, cli_brightness: int = None,
                 cli_id: str = None, cli_secret: str = None, cli_device: str = None,
                 cli_region: str = None, cli_ip: str = None, cli_key: str = None,
                 cli_dynamic: bool = None):
        self.config_path = config_path
        self.config = self.load_config()

        changed = False
        if cli_id:
            self.config["access_id"] = cli_id
            changed = True
        if cli_secret:
            self.config["access_secret"] = cli_secret
            changed = True
        if cli_device:
            self.config["device_id"] = cli_device
            changed = True
        if cli_region:
            self.config["region"] = cli_region
            changed = True
        if cli_ip:
            self.config["local_ip"] = cli_ip
            changed = True
        if cli_key:
            self.config["local_key"] = cli_key
            changed = True
        if cli_dynamic is not None:
            self.config["dynamic_brightness"] = cli_dynamic
            changed = True
        if cli_brightness is not None:
            self.config["brightness"] = max(30, min(100, int(cli_brightness)))
            changed = True

        if changed:
            self.save_config()

        self.device_id = self.config.get("device_id", "")
        self.local_ip = self.config.get("local_ip", "")
        self.local_key = self.config.get("local_key", "")
        self.protocol_version = float(self.config.get("protocol_version", 3.5))

        self.enabled = bool(self.config.get("enabled", True) and self.device_id)
        self.brightness = max(30, min(100, int(self.config.get("brightness", 70))))
        self.min_brightness = max(30, int(self.config.get("min_brightness", 30)))
        self.max_brightness = min(100, int(self.config.get("max_brightness", 100)))
        self.dynamic_brightness = bool(self.config.get("dynamic_brightness", True))
        self.saturation_boost = float(self.config.get("saturation_boost", 1.8))
        self.alpha = float(self.config.get("smoothing_alpha", 0.20))
        self.local_hz = float(self.config.get("local_refresh_hz", 12.0))
        self.cloud_refresh = float(self.config.get("refresh_interval_sec", 0.50))
        self.web_port = int(self.config.get("web_slider_port", 5556))

        # Target color from screen frame
        self.target_h = 0.0      # [0.0 .. 1.0]
        self.target_s = 0.0      # [0.0 .. 1.0]
        self.target_lum = 0.5    # [0.0 .. 1.0]

        # Current smoothed color state
        self.curr_h = 0.0        # [0.0 .. 360.0]
        self.curr_s = 0.0        # [0.0 .. 1000.0]
        self.curr_v = float(self.brightness * 10)  # [300.0 .. 1000.0]
        self.last_sent_hsv = (-1, -1, -1)
        self.current_display_b = self.brightness

        self.lock = threading.Lock()
        self.running = False

        self.local_device = None
        self.cloud_client = None
        self.mode = "Inactive"

        if self.enabled:
            # Auto-retrieve local key from Cloud if missing but cloud credentials exist
            if HAS_TINYTUYA and not self.local_key and self.config.get("access_id") and self.config.get("access_secret"):
                self._fetch_local_key_from_cloud()

            # Initialize local device
            if HAS_TINYTUYA and self.local_ip and self.local_key:
                self.local_device = TuyaLocalDevice(
                    device_id=self.device_id,
                    local_ip=self.local_ip,
                    local_key=self.local_key,
                    version=self.protocol_version
                )

            # Initialize cloud client as fallback
            if self.config.get("access_id") and self.config.get("access_secret"):
                self.cloud_client = TuyaCloudClient(
                    access_id=self.config["access_id"],
                    access_secret=self.config["access_secret"],
                    device_id=self.device_id,
                    region=self.config.get("region", "in")
                )

    def _fetch_local_key_from_cloud(self):
        """Fetches local_key via Tuya OpenAPI if credentials are present and saves it."""
        try:
            cloud = tinytuya.Cloud(
                apiRegion=self.config.get("region", "in"),
                apiKey=self.config.get("access_id"),
                apiSecret=self.config.get("access_secret"),
                apiDeviceID=self.device_id
            )
            devices = cloud.getdevices()
            for dev in devices:
                if dev.get("id") == self.device_id and dev.get("key"):
                    self.local_key = dev["key"]
                    self.config["local_key"] = dev["key"]
                    self.save_config()
                    print("[+] Retrieved local key for Tuya device from cloud.")
                    break
        except Exception:
            pass

    def load_config(self) -> dict:
        default = {
            "enabled": True,
            "device_id": "",
            "local_key": "",
            "local_ip": "",
            "protocol_version": 3.5,
            "access_id": "",
            "access_secret": "",
            "region": "in",
            "brightness": 70,
            "min_brightness": 30,
            "max_brightness": 100,
            "dynamic_brightness": True,
            "saturation_boost": 1.8,
            "smoothing_alpha": 0.20,
            "local_refresh_hz": 12,
            "refresh_interval_sec": 0.50,
            "web_slider_port": 5556
        }
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    default.update(json.load(f))
            except Exception:
                pass
        else:
            self.save_config(default)
        return default

    def save_config(self, conf: dict = None):
        if conf is None:
            conf = self.config
        try:
            with open(self.config_path, "w") as f:
                json.dump(conf, f, indent=2)
        except Exception:
            pass

    def set_brightness(self, value: int):
        clamped = max(self.min_brightness, min(self.max_brightness, int(value)))
        with self.lock:
            self.brightness = clamped
            self.config["brightness"] = clamped
        self.save_config()

    def set_dynamic_brightness(self, enable: bool):
        with self.lock:
            self.dynamic_brightness = bool(enable)
            self.config["dynamic_brightness"] = self.dynamic_brightness
        self.save_config()

    def update_frame(self, arr: np.ndarray):
        """
        Extracts saturation-weighted dominant color and screen luminance for dynamic brightness.
        Separates color extraction from phone's flat average so the bulb utilizes its full gamut.
        """
        if not self.enabled:
            return

        try:
            rgb_norm = arr.astype(np.float32) / 255.0
            r = rgb_norm[..., 0]
            g = rgb_norm[..., 1]
            b = rgb_norm[..., 2]

            cmax = np.maximum(np.maximum(r, g), b)
            cmin = np.minimum(np.minimum(r, g), b)
            delta = cmax - cmin

            sat = np.zeros_like(cmax)
            mask = cmax > 0.001
            sat[mask] = delta[mask] / cmax[mask]

            hue = np.zeros_like(cmax)
            mask_r = (cmax == r) & (delta > 0.0001)
            mask_g = (cmax == g) & (delta > 0.0001)
            mask_b = (cmax == b) & (delta > 0.0001)

            hue[mask_r] = ((g[mask_r] - b[mask_r]) / delta[mask_r]) % 6.0
            hue[mask_g] = ((b[mask_g] - r[mask_g]) / delta[mask_g]) + 2.0
            hue[mask_b] = ((r[mask_b] - g[mask_b]) / delta[mask_b]) + 4.0
            hue = hue / 6.0

            val = cmax

            # Perceived screen luminance for dynamic brightness
            luminance = float(np.mean(0.2126 * r + 0.7152 * g + 0.0722 * b))

            # Weight by sat^2 * val + 0.01 so vibrant colors dominate over neutral white/gray UI
            weights = (sat ** 2) * val + 0.01
            w_sum = weights.sum()
            if w_sum > 0:
                weights /= w_sum
            else:
                weights = np.full_like(weights, 1.0 / weights.size)

            # Circular weighted mean for Hue
            angles = hue * 2.0 * np.pi
            x = np.sum(weights * np.cos(angles))
            y = np.sum(weights * np.sin(angles))
            weighted_h = float((np.arctan2(y, x) / (2.0 * np.pi)) % 1.0)

            # Weighted saturation with expansion curve
            weighted_s = float(np.sum(weights * sat))
            if weighted_s < 0.025:
                # True grayscale/white screen
                boosted_s = 0.0
            else:
                boosted_s = min(1.0, (weighted_s ** 0.65) * self.saturation_boost)

            with self.lock:
                self.target_h = weighted_h
                self.target_s = boosted_s
                self.target_lum = luminance
        except Exception:
            pass

    def _gradient_sync_loop(self):
        """
        Runs smooth gradient interpolation towards target HSV and streams to the bulb
        over persistent TCP socket (or rate-limited Cloud fallback).
        """
        time.sleep(0.5)
        while self.running:
            is_local = (self.local_device is not None and self.local_device.is_connected)
            interval = (1.0 / self.local_hz) if is_local else self.cloud_refresh
            time.sleep(interval)

            if not self.enabled:
                continue

            with self.lock:
                th = self.target_h
                ts = self.target_s
                tlum = self.target_lum
                bright_pct = self.brightness
                alpha = self.alpha
                dyn = self.dynamic_brightness
                min_b = self.min_brightness
                max_b = self.max_brightness

            # 1. Compute dynamic brightness target
            if dyn:
                eff_brightness = min_b + (bright_pct - min_b) * tlum
                target_v = max(float(min_b), min(float(max_b), eff_brightness))
            else:
                target_v = float(bright_pct)

            # 2. Smooth gradient interpolation (circular shortest-path for Hue)
            target_h_deg = th * 360.0
            diff_h = (target_h_deg - self.curr_h + 180.0) % 360.0 - 180.0
            self.curr_h = (self.curr_h + alpha * diff_h) % 360.0

            # Linear smoothing for Saturation & Value
            target_s_tuya = ts * 1000.0
            self.curr_s += alpha * (target_s_tuya - self.curr_s)

            target_v_tuya = target_v * 10.0
            self.curr_v += alpha * (target_v_tuya - self.curr_v)

            # 3. Format Tuya HSV (V clamped to min 30% = 300)
            h_tuya = int(self.curr_h) % 360
            s_tuya = int(max(0, min(1000, self.curr_s)))
            v_tuya = int(max(min_b * 10, min(1000, self.curr_v)))
            self.current_display_b = int(v_tuya / 10)

            # 4. Deadband check: send if changed
            last_h, last_s, last_v = self.last_sent_hsv
            threshold = 2 if is_local else 6
            if abs(h_tuya - last_h) >= threshold or abs(s_tuya - last_s) >= (threshold * 3) or abs(v_tuya - last_v) >= (threshold * 3):
                sent = False
                if self.local_device:
                    sent = self.local_device.send_hsv(h_tuya, s_tuya, v_tuya)
                    if sent:
                        self.mode = "Local Wi-Fi"

                if not sent and self.cloud_client:
                    # Cloud fallback
                    sent = self.cloud_client.send_color(h_tuya, s_tuya, v_tuya)
                    if sent:
                        self.mode = "Cloud OpenAPI"

                if sent:
                    self.last_sent_hsv = (h_tuya, s_tuya, v_tuya)

    def start(self):
        if not self.enabled:
            return

        self.running = True

        # Try initial local connect
        if self.local_device:
            if self.local_device.connect():
                self.mode = "Local Wi-Fi"
                print(f"[+] Tuya Bulb Local Sync Active: {self.local_ip}:6668 (Persistent TCP, ~{int(self.local_hz)}Hz)")
            else:
                print(f"[!] Local connection to {self.local_ip} pending, falling back to Cloud if needed.")

        if self.mode != "Local Wi-Fi" and self.cloud_client:
            self.mode = "Cloud OpenAPI"
            print(f"[+] Tuya Bulb Cloud Sync Active: Device {self.device_id[:6]}... (Region: {self.config.get('region')})")

        dyn_str = "Dynamic Brightness" if self.dynamic_brightness else "Static Brightness"
        print(f"[+] Tuya Brightness: {self.brightness}% ({dyn_str}, Min {self.min_brightness}%)")

        threading.Thread(target=self._gradient_sync_loop, daemon=True).start()
        self._start_web_server()

    def stop(self):
        self.running = False
        if self.local_device:
            self.local_device.close()

    def mode_short(self) -> str:
        return "Local" if self.mode == "Local Wi-Fi" else "Cloud"

    def current_brightness_display(self) -> int:
        return self.current_display_b

    def _start_web_server(self):
        sync_self = self

        class SliderHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    with sync_self.lock:
                        b = sync_self.brightness
                        dyn = sync_self.dynamic_brightness
                        mode_str = sync_self.mode
                        h_norm = (sync_self.curr_h % 360.0) / 360.0
                        s_norm = max(0.0, min(1.0, sync_self.curr_s / 1000.0))
                        v_norm = max(0.0, min(1.0, sync_self.curr_v / 1000.0))
                        r_f, g_f, b_f = colorsys.hsv_to_rgb(h_norm, s_norm, v_norm)
                        r_d, g_d, b_d = int(r_f * 255), int(g_f * 255), int(b_f * 255)

                    dyn_checked = "checked" if dyn else ""
                    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Tuya Bulb Ambient Sync</title>
    <style>
        body {{
            background: #111116;
            color: #f0f0f5;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
        }}
        .card {{
            background: #1b1b24;
            padding: 30px;
            border-radius: 20px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.6);
            width: 330px;
            text-align: center;
            border: 1px solid rgba(255,255,255,0.08);
        }}
        h2 {{ margin: 0 0 10px; font-size: 20px; }}
        .badge {{
            display: inline-block;
            background: rgba(76, 175, 80, 0.2);
            color: #4CAF50;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: bold;
            margin-bottom: 15px;
        }}
        .swatch {{
            width: 70px;
            height: 70px;
            border-radius: 50%;
            margin: 10px auto 15px;
            border: 3px solid rgba(255,255,255,0.2);
            transition: background 0.15s ease;
            background: rgb({r_d},{g_d},{b_d});
        }}
        .val {{
            font-size: 32px;
            font-weight: bold;
            color: #ffb300;
            margin: 10px 0;
            font-family: monospace;
        }}
        input[type=range] {{
            width: 100%;
            margin: 15px 0 10px;
            accent-color: #ffb300;
            height: 8px;
            border-radius: 4px;
            outline: none;
        }}
        .toggle-box {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255,255,255,0.05);
            padding: 10px 14px;
            border-radius: 12px;
            margin: 15px 0;
            font-size: 13px;
        }}
        .sub {{
            color: rgba(255,255,255,0.45);
            font-size: 12px;
            line-height: 1.4;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h2>💡 Tuya Bulb Sync</h2>
        <div class="badge" id="mode-badge">{mode_str}</div>
        <div class="swatch" id="swatch"></div>
        <div class="sub">Brightness Control</div>
        <div class="val" id="val">{b}%</div>
        <input type="range" min="30" max="100" value="{b}" id="slider" oninput="update(this.value)">
        <div class="toggle-box">
            <span>Dynamic Brightness</span>
            <input type="checkbox" id="dyn-toggle" {dyn_checked} onchange="toggleDyn(this.checked)">
        </div>
        <div class="sub">Clamped: Minimum 30% prevents bulb cutoff</div>
    </div>
    <script>
        function update(v) {{
            document.getElementById('val').innerText = v + '%';
            fetch('/brightness?val=' + v);
        }}
        function toggleDyn(checked) {{
            fetch('/dynamic?val=' + (checked ? '1' : '0'));
        }}
        setInterval(async () => {{
            try {{
                let r = await fetch('/status');
                let d = await r.json();
                document.getElementById('swatch').style.background = 'rgb(' + d.r + ',' + d.g + ',' + d.b + ')';
                document.getElementById('mode-badge').innerText = d.mode;
            }} catch(e) {{}}
        }}, 400);
    </script>
</body>
</html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html.encode('utf-8'))

                elif parsed.path == "/brightness":
                    qs = parse_qs(parsed.query)
                    if "val" in qs:
                        try:
                            sync_self.set_brightness(int(qs["val"][0]))
                        except Exception:
                            pass
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')

                elif parsed.path == "/dynamic":
                    qs = parse_qs(parsed.query)
                    if "val" in qs:
                        sync_self.set_dynamic_brightness(qs["val"][0] == "1")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')

                elif parsed.path == "/status":
                    with sync_self.lock:
                        b = sync_self.brightness
                        dyn = sync_self.dynamic_brightness
                        mode_str = sync_self.mode
                        h_norm = (sync_self.curr_h % 360.0) / 360.0
                        s_norm = max(0.0, min(1.0, sync_self.curr_s / 1000.0))
                        v_norm = max(0.0, min(1.0, sync_self.curr_v / 1000.0))
                        r_f, g_f, b_f = colorsys.hsv_to_rgb(h_norm, s_norm, v_norm)
                        r_d, g_d, b_d = int(r_f * 255), int(g_f * 255), int(b_f * 255)

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "brightness": b,
                        "dynamic": dyn,
                        "mode": mode_str,
                        "r": r_d, "g": g_d, "b": b_d
                    }).encode('utf-8'))
                else:
                    self.send_response(404)
                    self.end_headers()

        def run_server():
            try:
                server = HTTPServer(("0.0.0.0", self.web_port), SliderHandler)
                print(f"[+] Tuya Brightness Web Slider running at: http://localhost:{self.web_port}")
                server.serve_forever()
            except Exception as e:
                print(f"[!] Warning: Tuya Web Slider server failed on port {self.web_port}: {e}")

        threading.Thread(target=run_server, daemon=True).start()


# ==============================================================================
# Screen Capture & Multi-Device Dispatcher (Mutter / PipeWire)
# ==============================================================================

class MutterScreenSync:
    def __init__(self, port=PORT, fps=FPS_TARGET, auto_dismiss_ui=True, skip_phone=False,
                 tuya_brightness=None, tuya_id=None, tuya_secret=None, tuya_device=None,
                 tuya_region=None, tuya_ip=None, tuya_key=None, tuya_dynamic=None):
        self.port = port
        self.fps = fps
        self.auto_dismiss_ui = auto_dismiss_ui
        self.skip_phone = skip_phone
        self.sock = None
        self.pipeline = None
        self.loop = GLib.MainLoop()
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sess_proxy = None
        self.last_rgb = (-1, -1, -1)
        self.phone_worker_running = True

        # Tuya Smart Bulb Sync
        self.tuya = None
        try:
            self.tuya = TuyaBulbSync(
                cli_brightness=tuya_brightness,
                cli_id=tuya_id,
                cli_secret=tuya_secret,
                cli_device=tuya_device,
                cli_region=tuya_region,
                cli_ip=tuya_ip,
                cli_key=tuya_key,
                cli_dynamic=tuya_dynamic
            )
            if self.tuya.enabled:
                self.tuya.start()
        except Exception as e:
            print(f"[!] Warning: Tuya init failed: {e}")

    def _phone_connect_worker(self):
        """Asynchronously connects to the phone in the background without blocking screen capture or Tuya sync."""
        time.sleep(0.5)
        while self.phone_worker_running:
            if self.sock is not None:
                time.sleep(1.0)
                continue

            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.connect((HOST, self.port))
                self.sock = s
                print("\n[+] Connected to Android phone app!")
                if self.auto_dismiss_ui:
                    dismiss_initial_ui()
            except (ConnectionRefusedError, OSError):
                time.sleep(2.0)

    def on_new_sample(self, appsink):
        sample = appsink.emit('pull-sample')
        if not sample:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        success, map_info = buf.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK

        try:
            # 16x9 x 3 bytes RGB = 432 bytes total
            arr = np.frombuffer(map_info.data, dtype=np.uint8).reshape((HEIGHT, WIDTH, 3))

            # Phone Ambient Color (Strictly UNCHANGED)
            avg_rgb = arr.mean(axis=(0, 1)).astype(np.uint8)
            r, g, b = int(avg_rgb[0]), int(avg_rgb[1]), int(avg_rgb[2])

            with vibration_lock:
                vibe = current_vibration

            payload = bytes([r, g, b, vibe])

            # 1. Stream to Phone over ADB USB
            if self.sock:
                try:
                    self.sock.sendall(payload)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None

            # 2. Feed frame to Tuya bulb controller (Saturation expansion + dynamic brightness + gradient easing)
            if self.tuya and self.tuya.enabled:
                self.tuya.update_frame(arr)

            # 3. Terminal status display
            if (r, g, b) != self.last_rgb:
                self.last_rgb = (r, g, b)
                bulb_str = f" | Bulb ({self.tuya.mode_short()}): {self.tuya.current_brightness_display()}%" if (self.tuya and self.tuya.enabled) else ""
                phone_str = " | Phone: Active" if self.sock else " | Phone: Connecting..."
                print(f"\r[*] Screen Sync -> RGB: ({r:3d}, {g:3d}, {b:3d}) | Vibe: {vibe:3d}{phone_str}{bulb_str}   ", end="", flush=True)

        finally:
            buf.unmap(map_info)

        return Gst.FlowReturn.OK

    def start_pipeline(self, node_id):
        pipe_str = (
            f'pipewiresrc path={node_id} keepalive-time=1000 ! '
            f'video/x-raw,max-framerate={self.fps}/1 ! '
            f'videoscale ! video/x-raw,width={WIDTH},height={HEIGHT} ! '
            f'videoconvert ! video/x-raw,format=RGB ! '
            f'appsink name=sink emit-signals=true max-buffers=1 drop=true'
        )
        self.pipeline = Gst.parse_launch(pipe_str)
        sink = self.pipeline.get_by_name('sink')
        sink.connect('new-sample', self.on_new_sample)
        self.pipeline.set_state(Gst.State.PLAYING)
        print(f"[+] PipeWire pipeline running (Downscaled to {WIDTH}x{HEIGHT} at source).")

    def stop(self):
        self.phone_worker_running = False
        try:
            if self.loop.is_running():
                self.loop.quit()
        except Exception:
            pass
        if self.pipeline:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        if self.sess_proxy:
            try:
                self.sess_proxy.call_sync('Stop', None, Gio.DBusCallFlags.NONE, -1, None)
            except Exception:
                pass
        if self.tuya:
            try:
                self.tuya.stop()
            except Exception:
                pass
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def run(self):
        Gst.init(None)

        # Start non-blocking phone connection worker
        if not self.skip_phone:
            setup_adb_forward(self.port)
            threading.Thread(target=self._phone_connect_worker, daemon=True).start()

        # Connect to Mutter ScreenCast D-Bus
        proxy = Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            'org.gnome.Mutter.ScreenCast',
            '/org/gnome/Mutter/ScreenCast',
            'org.gnome.Mutter.ScreenCast',
            None
        )
        sess_res = proxy.call_sync('CreateSession', GLib.Variant('(a{sv})', ({},)), Gio.DBusCallFlags.NONE, -1, None)
        sess_path = sess_res.unpack()[0]
        self.sess_proxy = Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            'org.gnome.Mutter.ScreenCast',
            sess_path,
            'org.gnome.Mutter.ScreenCast.Session',
            None
        )

        stream_res = self.sess_proxy.call_sync('RecordMonitor', GLib.Variant('(sa{sv})', ('', {})), Gio.DBusCallFlags.NONE, -1, None)
        stream_path = stream_res.unpack()[0]

        def on_stream_signal(conn, sender, path, iface, signal, params, udata):
            if signal == 'PipeWireStreamAdded':
                node_id = params.unpack()[0]
                self.start_pipeline(node_id)

        self.bus.signal_subscribe(
            'org.gnome.Mutter.ScreenCast',
            'org.gnome.Mutter.ScreenCast.Stream',
            'PipeWireStreamAdded',
            stream_path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_stream_signal,
            None
        )

        self.sess_proxy.call_sync('Start', None, Gio.DBusCallFlags.NONE, -1, None)
        print("[+] Mutter ScreenCast session started.")

        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            if not self.skip_phone:
                cleanup_phone_and_exit(self)


def main():
    parser = argparse.ArgumentParser(description="Mutter ScreenCast (PipeWire) Downscaled Screen Sync with Tuya Bulb & Controller Rumble")
    parser.add_argument("--port", type=int, default=PORT, help="ADB TCP forward port (default 5555)")
    parser.add_argument("--fps", type=int, default=FPS_TARGET, help="Target FPS for screen sync (default 30)")
    parser.add_argument("--simulate-vibration", action="store_true", help="Simulate vibration every 4s to test torch")
    parser.add_argument("--controller", type=str, default=None, help="Explicit input device node (e.g. /dev/input/event17)")
    parser.add_argument("--no-unlock", action="store_true", help="Skip waking/unlocking phone on launch")
    parser.add_argument("--no-dismiss-ui", action="store_true", help="Do not auto-dismiss phone UI")
    parser.add_argument("--no-phone", action="store_true", help="Skip phone connection and sync only Tuya bulb")
    parser.add_argument("--tuya-brightness", type=int, default=None, help="Tuya bulb brightness percentage (30-100)")
    parser.add_argument("--tuya-id", type=str, default=None, help="Tuya Cloud Access ID")
    parser.add_argument("--tuya-secret", type=str, default=None, help="Tuya Cloud Access Secret")
    parser.add_argument("--tuya-device", type=str, default=None, help="Tuya Bulb Device ID")
    parser.add_argument("--tuya-region", type=str, default=None, help="Tuya Cloud Region (in, us, eu, cn)")
    parser.add_argument("--tuya-ip", type=str, default=None, help="Tuya bulb local IP on Wi-Fi (e.g. 192.168.1.74)")
    parser.add_argument("--tuya-key", type=str, default=None, help="Tuya bulb local 16-character encryption key")
    parser.add_argument("--tuya-dynamic", action="store_true", default=None, help="Enable dynamic brightness shifts")
    parser.add_argument("--no-tuya-dynamic", dest="tuya_dynamic", action="store_false", help="Disable dynamic brightness shifts")
    args = parser.parse_args()

    # 1. Unlock phone and launch app if not skipped
    if not args.no_phone and not args.no_unlock:
        unlock_and_launch_phone()

    sync = MutterScreenSync(
        port=args.port,
        fps=args.fps,
        auto_dismiss_ui=not args.no_dismiss_ui,
        skip_phone=args.no_phone,
        tuya_brightness=args.tuya_brightness,
        tuya_id=args.tuya_id,
        tuya_secret=args.tuya_secret,
        tuya_device=args.tuya_device,
        tuya_region=args.tuya_region,
        tuya_ip=args.tuya_ip,
        tuya_key=args.tuya_key,
        tuya_dynamic=args.tuya_dynamic
    )

    # 2. Signal handlers for clean exit on Ctrl-C or SIGTERM
    def sig_handler(*_):
        if not sync.skip_phone:
            cleanup_phone_and_exit(sync)
        else:
            sync.stop()
        if sync.loop and sync.loop.is_running():
            sync.loop.quit()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    try:
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, sig_handler)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, sig_handler)
    except Exception:
        pass
    if not sync.skip_phone:
        atexit.register(cleanup_phone_and_exit, sync)

    # 3. Start rumble monitoring
    vibe_thread = threading.Thread(
        target=monitor_controller_vibration,
        args=(args.controller, args.simulate_vibration),
        daemon=True
    )
    vibe_thread.start()

    # 4. Start sync loop
    sync.run()


if __name__ == "__main__":
    main()
