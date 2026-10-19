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
# Tuya Cloud API & Smart Bulb Synchronizer
# ==============================================================================

class TuyaCloudClient:
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
        """Sends HSV color to bulb. h: [0..360], s: [0..1000], v: [300..1000]."""
        try:
            token = self.get_token()
        except Exception as e:
            return False

        now = time.time()
        t = str(int(now * 1000))

        if not self.use_legacy_code:
            payload = {
                "commands": [
                    {
                        "code": "colour_data_v2",
                        "value": {"h": int(h), "s": int(s), "v": int(v)}
                    }
                ]
            }
        else:
            hex_val = f"{int(h):04x}{int(s):04x}{int(v):04x}"
            payload = {
                "commands": [
                    {
                        "code": "colour_data",
                        "value": hex_val
                    }
                ]
            }

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
    """Handles color smoothing (EMA filter), rate limiting (2 Hz), and web brightness slider."""
    def __init__(self, config_path: str = CONFIG_FILE, cli_brightness: int = None,
                 cli_id: str = None, cli_secret: str = None, cli_device: str = None, cli_region: str = None):
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
        if cli_brightness is not None:
            self.config["brightness"] = max(30, min(100, int(cli_brightness)))
            changed = True

        if changed:
            self.save_config()

        self.enabled = bool(self.config.get("enabled", True) and self.config.get("access_id") and self.config.get("device_id"))
        self.brightness = max(30, min(100, int(self.config.get("brightness", 70))))
        self.alpha = float(self.config.get("smoothing_alpha", 0.20))
        self.refresh_interval = float(self.config.get("refresh_interval_sec", 0.50))
        self.web_port = int(self.config.get("web_slider_port", 5556))

        self.target_rgb = [0.0, 0.0, 0.0]
        self.current_rgb = [0.0, 0.0, 0.0]
        self.last_sent_hsv = (-1, -1, -1)
        self.lock = threading.Lock()
        self.running = False
        self.client = None

        if self.enabled:
            self.client = TuyaCloudClient(
                access_id=self.config["access_id"],
                access_secret=self.config["access_secret"],
                device_id=self.config["device_id"],
                region=self.config.get("region", "in")
            )

    def load_config(self) -> dict:
        default = {
            "enabled": True,
            "access_id": "",
            "access_secret": "",
            "device_id": "",
            "region": "in",
            "brightness": 70,
            "min_brightness": 30,
            "max_brightness": 100,
            "smoothing_alpha": 0.20,
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
        clamped = max(30, min(100, int(value)))
        with self.lock:
            self.brightness = clamped
            self.config["brightness"] = clamped
        self.save_config()

    def update_screen_color(self, r: int, g: int, b: int):
        if not self.enabled:
            return
        with self.lock:
            self.target_rgb = [float(r), float(g), float(b)]

    def _smoothing_loop(self):
        """Softens rapid color jumps using exponential smoothing (EMA)."""
        dt = 0.04  # 25 updates per second
        while self.running:
            with self.lock:
                for i in range(3):
                    self.current_rgb[i] += self.alpha * (self.target_rgb[i] - self.current_rgb[i])
            time.sleep(dt)

    def _dispatcher_loop(self):
        """Dispatches rate-limited updates to Tuya Cloud without blocking screen sync."""
        time.sleep(1.0)
        while self.running:
            time.sleep(self.refresh_interval)
            if not self.enabled or not self.client:
                continue

            with self.lock:
                r, g, b = self.current_rgb
                bright_pct = self.brightness

            r_norm, g_norm, b_norm = r / 255.0, g / 255.0, b / 255.0
            h, s, v = colorsys.rgb_to_hsv(r_norm, g_norm, b_norm)

            h_deg = int(h * 360)
            s_tuya = int(s * 1000)
            # Minimum brightness clamped to 30% (300) to prevent bulb shutoff
            v_tuya = int(max(300, min(1000, bright_pct * 10)))

            if v < 0.05:
                v_tuya = 300

            last_h, last_s, last_v = self.last_sent_hsv
            if abs(h_deg - last_h) >= 4 or abs(s_tuya - last_s) >= 20 or abs(v_tuya - last_v) >= 20:
                success = self.client.send_color(h_deg, s_tuya, v_tuya)
                if success:
                    self.last_sent_hsv = (h_deg, s_tuya, v_tuya)

    def start(self):
        if not self.enabled:
            print("[!] Tuya bulb sync is disabled or credentials missing in tuya_config.json")
            return

        print(f"[+] Tuya Bulb Sync Active: Device {self.client.device_id[:6]}... (Region: {self.config.get('region')})")
        print(f"[+] Tuya Brightness: {self.brightness}% (Clamped 30% - 100%)")
        self.running = True

        threading.Thread(target=self._smoothing_loop, daemon=True).start()
        threading.Thread(target=self._dispatcher_loop, daemon=True).start()
        self._start_web_server()

    def stop(self):
        self.running = False

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
                        r, g, b_val = [int(c) for c in sync_self.current_rgb]

                    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Tuya Bulb Ambient Slider</title>
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
            width: 320px;
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
            margin-bottom: 20px;
        }}
        .swatch {{
            width: 70px;
            height: 70px;
            border-radius: 50%;
            margin: 15px auto;
            border: 3px solid rgba(255,255,255,0.2);
            transition: background 0.3s ease;
            background: rgb({r},{g},{b_val});
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
            margin: 20px 0 10px;
            accent-color: #ffb300;
            height: 8px;
            border-radius: 4px;
            outline: none;
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
        <div class="badge">ACTIVE & SMOOTHED</div>
        <div class="swatch" id="swatch"></div>
        <div class="sub">Brightness Control</div>
        <div class="val" id="val">{b}%</div>
        <input type="range" min="30" max="100" value="{b}" id="slider" oninput="update(this.value)">
        <div class="sub">Clamped: Minimum 30% prevents bulb cutoff</div>
    </div>
    <script>
        function update(v) {{
            document.getElementById('val').innerText = v + '%';
            fetch('/brightness?val=' + v);
        }}
        setInterval(async () => {{
            try {{
                let r = await fetch('/status');
                let d = await r.json();
                document.getElementById('swatch').style.background = 'rgb(' + d.r + ',' + d.g + ',' + d.b + ')';
            }} catch(e) {{}}
        }}, 800);
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

                elif parsed.path == "/status":
                    with sync_self.lock:
                        b = sync_self.brightness
                        r, g, b_val = [int(c) for c in sync_self.current_rgb]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"brightness": b, "r": r, "g": g, "b": b_val}).encode('utf-8'))
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
                 tuya_brightness=None, tuya_id=None, tuya_secret=None, tuya_device=None, tuya_region=None):
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
                cli_region=tuya_region
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

            # 2. Feed to Tuya bulb smoothing worker
            if self.tuya and self.tuya.enabled:
                self.tuya.update_screen_color(r, g, b)

            # 3. Terminal status display
            if (r, g, b) != self.last_rgb:
                self.last_rgb = (r, g, b)
                bulb_str = f" | Bulb: {self.tuya.brightness}%" if (self.tuya and self.tuya.enabled) else ""
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
        tuya_region=args.tuya_region
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
