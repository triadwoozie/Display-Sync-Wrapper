#!/usr/bin/env python3
"""
Mutter ScreenCast (PipeWire) Display Sync, Controller Rumble Interceptor & Tuya Bulb Controller
-------------------------------------------------------------------------------------------------
- Captures desktop color downscaled to 16x9 at the source (Mutter GPU compositor pass).
- Phone Sync (ADB USB tcp:5555): sub-5ms low-latency ambient color + camera torch flash on rumble.
- Tuya Bulb Sync: Persistent local TCP socket with TCP_USER_TIMEOUT/keepalive, non-blocking
  drain & EOF detection, MCU-safe pacing, and non-blocking Cloud fallback.
- Localhost JSON Control API: Lightweight control API at http://127.0.0.1:5556.
- Fault-tolerant: Phone or Tuya bulb not being connected/configured never blocks or halts the pipeline.
- Clean shutdown: On Ctrl-C or exit, closes app, sleeps phone, and shuts down pipeline.
"""

import sys
import os
import time
import json
import hmac
import select
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
FPS_TARGET = 60

WIDTH = 16
HEIGHT = 9

def resolve_config_path(explicit_path: str = None) -> str:
    """Resolves tuya_config.json prioritizing ~/.config/display-sync so extension updates do not overwrite credentials."""
    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path
    user_cfg = os.path.expanduser("~/.config/display-sync/tuya_config.json")
    if os.path.isfile(user_cfg):
        return user_cfg
    script_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tuya_config.json")
    if os.path.isfile(script_cfg):
        return script_cfg
    home_cfg = os.path.expanduser("~/tuya_config.json")
    if os.path.isfile(home_cfg):
        return home_cfg
    return user_cfg

CONFIG_FILE = resolve_config_path()
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

_last_adb_check = 0.0
_cached_adb_status = False

def check_adb_device() -> bool:
    """Checks if an authorized Android device is connected via ADB (cached for 2s to optimize polling)."""
    global _last_adb_check, _cached_adb_status
    now = time.time()
    if now - _last_adb_check < 2.0:
        return _cached_adb_status
    _last_adb_check = now
    try:
        res = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=1.5)
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip() and not line.startswith("List of")]
        _cached_adb_status = any("\tdevice" in line for line in lines)
    except Exception:
        _cached_adb_status = False
    return _cached_adb_status

def unlock_and_launch_phone():
    """Wakes the phone, dismisses keyguard, and launches the app in fullscreen mode."""
    try:
        # 1. Wake screen if asleep (KEYCODE_WAKEUP = 224)
        subprocess.run(["adb", "shell", "input", "keyevent", "224"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
        # 2. Dismiss keyguard cleanly via KEYCODE_MENU (82)
        subprocess.run(["adb", "shell", "wm", "dismiss-keyguard"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
        subprocess.run(["adb", "shell", "input", "keyevent", "82"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
        time.sleep(0.3)
        # 3. Launch the app in foreground with fullscreen intent extra
        subprocess.run([
            "adb", "shell", "am", "start",
            "-W",
            "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER",
            "-n", "com.flashlight.torch/.MainActivity",
            "--ez", "hide_controls", "true"
        ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0)
        # 4. Broadcast HIDE_UI to ensure fullscreen
        subprocess.run(["adb", "shell", "am", "broadcast", "-a", "com.flashlight.torch.HIDE_UI"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
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
        subprocess.run(["adb", "shell", "am", "force-stop", "com.flashlight.torch"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
        subprocess.run(["adb", "shell", "input", "keyevent", "223"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
    except Exception:
        pass

    if sync_instance:
        try:
            sync_instance.stop()
        except Exception:
            pass


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
# Tuya Local (Wi-Fi Persistent Socket) & Cloud API Smart Bulb Synchronizer
# ==============================================================================

def get_ip_address(ifname: str) -> str:
    """Returns IPv4 address of a network interface using ioctl."""
    import fcntl, struct
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            return socket.inet_ntoa(fcntl.ioctl(
                s.fileno(),
                0x8915,  # SIOCGIFADDR
                struct.pack('256s', ifname[:15].encode('utf-8'))
            )[20:24])
        finally:
            s.close()
    except Exception:
        return None

def get_ethernet_ip() -> str:
    """Finds the IP address of an active UP Ethernet/USB tether interface (excluding hotspot wlp2s0)."""
    try:
        for iface in sorted(os.listdir('/sys/class/net')):
            if iface.startswith(('en', 'eth')):
                state_path = f'/sys/class/net/{iface}/operstate'
                try:
                    with open(state_path, 'r') as f:
                        if f.read().strip().lower() not in ('up', 'unknown'):
                            continue
                except Exception:
                    pass
                ip = get_ip_address(iface)
                if ip:
                    return ip
    except Exception:
        pass
    return None

def configure_tcp_socket(sock: socket.socket, rw_timeout: float = 0.5):
    """Applies low-latency TCP options, keepalive, and Linux TCP_USER_TIMEOUT to fail fast on drops."""
    sock.settimeout(rw_timeout)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'TCP_KEEPIDLE'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
        if hasattr(socket, 'TCP_KEEPINTVL'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)
        if hasattr(socket, 'TCP_KEEPCNT'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 2)
        # TCP_USER_TIMEOUT = 18 on Linux: drop connection in 1500ms if bulb stops ACKing
        sock.setsockopt(socket.IPPROTO_TCP, 18, 1500)
    except Exception:
        pass

if HAS_TINYTUYA:
    class BoundBulbDevice(tinytuya.BulbDevice):
        """BulbDevice subclass that binds sockets exclusively to an explicit interface IP (e.g. Ethernet)."""
        def __init__(self, *args, bind_ip=None, connect_timeout=1.2, rw_timeout=0.5, **kwargs):
            self.bind_ip = bind_ip
            self.connect_timeout = connect_timeout
            self.rw_timeout = rw_timeout
            super().__init__(*args, **kwargs)

        def _get_socket(self, renew):
            if renew and self.socket is not None:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None
            if self.socket is None:
                if not self.address:
                    return tinytuya.ERR_OFFLINE
                family = socket.AF_INET6 if ':' in self.address else socket.AF_INET
                sock = socket.socket(family, socket.SOCK_STREAM)
                configure_tcp_socket(sock, rw_timeout=self.connect_timeout)
                if self.bind_ip:
                    try:
                        sock.bind((self.bind_ip, 0))
                    except Exception:
                        pass
                try:
                    sock.connect((self.address, self.port))
                    self.socket = sock
                    if self.version >= 3.4:
                        if self._negotiate_session_key():
                            # Switch to tighter runtime timeout after handshake succeeds
                            configure_tcp_socket(self.socket, rw_timeout=self.rw_timeout)
                            return True
                        else:
                            if self.socket:
                                self.socket.close()
                                self.socket = None
                            return tinytuya.ERR_KEY_OR_VER
                    configure_tcp_socket(self.socket, rw_timeout=self.rw_timeout)
                    return True
                except Exception:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    self.socket = None
                    return tinytuya.ERR_CONNECT
            return True
else:
    class BoundBulbDevice:
        pass

class TuyaLocalDevice:
    """
    Controls Tuya bulb directly over local network using BoundBulbDevice
    with a persistent TCP socket.
    - Detects remote EOF (b'') in non-blocking drain without wiping socket timeout.
    - Enforces TCP_USER_TIMEOUT & SO_KEEPALIVE so Wi-Fi hiccups recover in <1.5s.
    - Falls back cleanly from bind_ip to default route if bound interface cannot reach the bulb.
    """
    def __init__(self, device_id: str, local_ip: str, local_key: str, version: float = 3.5, bind_ip: str = None):
        self.device_id = device_id.strip()
        self.local_ip = local_ip.strip()
        self.local_key = local_key.strip()
        self.version = float(version)
        self.bind_ip = bind_ip
        self.rw_timeout = 0.5
        self.bulb = None
        self.is_connected = False
        self.lock = threading.Lock()
        self.last_attempt_time = 0.0
        self.last_send_time = 0.0
        self.bind_fail_count = 0

    def set_bind_ip(self, bind_ip: str):
        with self.lock:
            if self.bind_ip != bind_ip:
                self.bind_ip = bind_ip
                self.bind_fail_count = 0
                self._close_socket()

    def _close_socket(self):
        self.is_connected = False
        if self.bulb:
            try:
                self.bulb.close()
            except Exception:
                pass
            self.bulb = None

    def disconnect(self):
        with self.lock:
            self._close_socket()

    def connect(self) -> bool:
        if not HAS_TINYTUYA or not self.local_ip or not self.local_key:
            return False
        with self.lock:
            now = time.time()
            if now - self.last_attempt_time < 1.0:
                return False
            self.last_attempt_time = now

            effective_bind_ip = self.bind_ip
            if effective_bind_ip:
                active_eth = get_ethernet_ip()
                if not active_eth:
                    effective_bind_ip = None
                elif active_eth != effective_bind_ip:
                    self.bind_ip = active_eth
                    effective_bind_ip = active_eth
                # If binding to Ethernet failed twice, fall back to default route (Wi-Fi)
                if self.bind_fail_count >= 2:
                    effective_bind_ip = None

            self._close_socket()

            try:
                bulb = BoundBulbDevice(
                    dev_id=self.device_id,
                    address=self.local_ip,
                    local_key=self.local_key,
                    version=self.version,
                    persist=True,
                    bind_ip=effective_bind_ip,
                    connect_timeout=1.2,
                    rw_timeout=self.rw_timeout
                )
                bulb.set_version(self.version)
                bulb.set_socketPersistent(True)
                bulb.set_socketTimeout(self.rw_timeout)
                bulb.socketRetryLimit = 1
                bulb.socketRetryDelay = 0.05

                # Verify connection & session key negotiation
                sock_ok = bulb._get_socket(True)
                if sock_ok is not True or not getattr(bulb, 'socket', None):
                    if effective_bind_ip:
                        self.bind_fail_count += 1
                    try:
                        bulb.close()
                    except Exception:
                        pass
                    return False

                # Ensure bulb is powered on in colour mode
                res = bulb.set_multiple_values({'20': True, '21': 'colour'}, nowait=True)
                if isinstance(res, dict) and ('Error' in res or 'Err' in res):
                    try:
                        bulb.close()
                    except Exception:
                        pass
                    return False

                self.bulb = bulb
                self.is_connected = True
                # ONLY reset bind_fail_count if we actually connected using bind_ip!
                if effective_bind_ip:
                    self.bind_fail_count = 0
                self.last_send_time = time.time()
                if not self._drain_socket():
                    self._close_socket()
                    return False
                return True
            except Exception:
                if effective_bind_ip:
                    self.bind_fail_count += 1
                self._close_socket()
                return False

    def _drain_socket(self) -> bool:
        """
        Drains incoming response packets non-blocking without resetting socket timeout.
        Returns False if the remote peer closed the connection (EOF b'') or errored.
        """
        sock = getattr(self.bulb, 'socket', None) if self.bulb else None
        if not sock:
            return False
        try:
            while True:
                r, _, _ = select.select([sock], [], [], 0.0)
                if not r:
                    break
                data = sock.recv(4096)
                if not data:
                    # Peer sent TCP FIN (EOF) -> socket is dead
                    return False
            return True
        except BlockingIOError:
            return True
        except Exception:
            return False

    def poll_and_heartbeat(self, now: float) -> bool:
        """
        1. Constantly drains delayed async ACKs from the bulb on every loop tick.
        2. Sends a Tuya 0x09 heartbeat every 3.5s when idle so the MCU never drops
           the session key or enters Wi-Fi DTIM power-save sleep.
        """
        if not self.is_connected or not self.bulb or not getattr(self.bulb, 'socket', None):
            return False

        with self.lock:
            if not self._drain_socket():
                self._close_socket()
                return False

            if now - self.last_send_time >= 3.5:
                try:
                    self.bulb.heartbeat(nowait=True)
                    self.last_send_time = now
                    if not self._drain_socket():
                        self._close_socket()
                        return False
                except Exception:
                    self._close_socket()
                    return False
            return True

    def send_hsv(self, h: int, s: int, v: int) -> bool:
        """Sends HSV color directly over local Wi-Fi TCP socket (non-blocking nowait=True)."""
        if not self.is_connected or not self.bulb or not getattr(self.bulb, 'socket', None):
            if not self.connect():
                return False

        hex_val = f"{int(h):04x}{int(s):04x}{int(v):04x}"
        with self.lock:
            if not self.is_connected or not self.bulb or not getattr(self.bulb, 'socket', None):
                return False
            try:
                # Drain any stale responses / detect EOF before writing
                if not self._drain_socket():
                    self._close_socket()
                    return False

                res = self.bulb.set_value('24', hex_val, nowait=True)
                if (isinstance(res, dict) and ('Error' in res or 'Err' in res)) or not getattr(self.bulb, 'socket', None):
                    self._close_socket()
                    return False
                self.last_send_time = time.time()
                if not self._drain_socket():
                    self._close_socket()
                    return False
                return True
            except Exception:
                self._close_socket()
                return False

    def send_white(self, brightness: int, temp: int = 500) -> bool:
        """Sets bulb to normal white mode with specified brightness (10-1000) and temp (0-1000)."""
        if not self.is_connected or not self.bulb or not getattr(self.bulb, 'socket', None):
            if not self.connect():
                return False

        with self.lock:
            if not self.is_connected or not self.bulb or not getattr(self.bulb, 'socket', None):
                return False
            try:
                if not self._drain_socket():
                    self._close_socket()
                    return False
                res = self.bulb.set_multiple_values({
                    '20': True,
                    '21': 'white',
                    '22': int(brightness),
                    '23': int(temp)
                }, nowait=True)
                if (isinstance(res, dict) and ('Error' in res or 'Err' in res)) or not getattr(self.bulb, 'socket', None):
                    self._close_socket()
                    return False
                self.last_send_time = time.time()
                if not self._drain_socket():
                    self._close_socket()
                    return False
                return True
            except Exception:
                self._close_socket()
                return False

    def set_power(self, power: bool) -> bool:
        """Powers the bulb on or off."""
        if not self.is_connected or not self.bulb or not getattr(self.bulb, 'socket', None):
            if power and not self.connect():
                return False
        with self.lock:
            try:
                if self.bulb:
                    if power:
                        res = self.bulb.turn_on(nowait=True)
                    else:
                        res = self.bulb.turn_off(nowait=True)
                    if isinstance(res, dict) and ('Error' in res or 'Err' in res):
                        self._close_socket()
                        return False
                    self.last_send_time = time.time()
                    if not self._drain_socket():
                        self._close_socket()
                        return False
                    return True
                return False
            except Exception:
                self._close_socket()
                return False

    def close(self):
        with self.lock:
            self._close_socket()


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
                resp = requests.get(f"{self.base_url}{path}", headers=headers, timeout=3)
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
            resp = requests.post(f"{self.base_url}{path}", headers=headers, data=body_str, timeout=2.5)
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

    def send_white(self, brightness: int) -> bool:
        """Sets bulb to normal white mode via Tuya Cloud OpenAPI."""
        try:
            token = self.get_token()
        except Exception:
            return False

        now = time.time()
        t = str(int(now * 1000))
        commands = [
            {"code": "switch_led", "value": True},
            {"code": "work_mode", "value": "white"},
            {"code": "bright_value_v2", "value": int(brightness)}
        ]
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
            resp = requests.post(f"{self.base_url}{path}", headers=headers, data=body_str, timeout=2.5)
            return bool(resp.json().get("success"))
        except Exception:
            return False

    def set_power(self, power: bool) -> bool:
        """Powers the bulb on or off via Tuya Cloud OpenAPI."""
        try:
            token = self.get_token()
        except Exception:
            return False

        now = time.time()
        t = str(int(now * 1000))
        commands = [{"code": "switch_led", "value": bool(power)}]
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
            resp = requests.post(f"{self.base_url}{path}", headers=headers, data=body_str, timeout=2.5)
            return bool(resp.json().get("success"))
        except Exception:
            return False


class TuyaBulbSync:
    """
    Tuya Smart Bulb Controller:
    - Direct Wi-Fi local socket via tinytuya.BulbDevice (persistent TCP, ~10 Hz default)
    - Full color range expansion: saturation weighting so colors pop and never wash out to white
    - Dynamic brightness shifts: bulb scales dynamically with screen luminance (clamped 10%-100%)
    - Smooth gradient easing: RGB linear cross-fade + adaptive snappiness
    - Non-blocking Cloud OpenAPI fallback only after sustained local offline (>5s)
    - JSON control API: real-time brightness and dynamic mode control at http://127.0.0.1:5556
    """
    def __init__(self, config_path: str = None, cli_brightness: int = None,
                 cli_id: str = None, cli_secret: str = None, cli_device: str = None,
                 cli_region: str = None, cli_ip: str = None, cli_key: str = None,
                 cli_dynamic: bool = None, cli_hz: float = None):
        self.config_path = resolve_config_path(config_path)
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
            self.config["brightness"] = max(10, min(100, int(cli_brightness)))
            changed = True
        if cli_hz is not None:
            self.config["local_refresh_hz"] = max(2.0, min(30.0, float(cli_hz)))
            changed = True

        if changed:
            self.save_config()

        self.device_id = self.config.get("device_id", "")
        self.local_ip = self.config.get("local_ip", "")
        self.local_key = self.config.get("local_key", "")
        self.protocol_version = float(self.config.get("protocol_version", 3.5))

        self.enabled = bool(self.config.get("enabled", True) and self.device_id)
        self.power_on = bool(self.config.get("power_on", True))
        self.brightness = max(10, min(100, int(self.config.get("brightness", 70))))
        self.min_brightness = max(10, int(self.config.get("min_brightness", 10)))
        self.max_brightness = min(100, int(self.config.get("max_brightness", 100)))
        self.dynamic_brightness = bool(self.config.get("dynamic_brightness", True))
        self.phone_flash_enabled = bool(self.config.get("phone_flash_enabled", True))
        self.alpha = float(self.config.get("smoothing_alpha", 0.45))
        raw_hz = float(self.config.get("local_refresh_hz", 10.0))
        self.local_hz = max(2.0, min(30.0, raw_hz))
        self.cloud_refresh = float(self.config.get("refresh_interval_sec", 0.50))
        self.web_port = int(self.config.get("web_slider_port", 5556))

        self.white_mode = bool(self.config.get("white_mode", False))
        self.white_temp = int(self.config.get("white_temp", 500))
        self.last_sent_white = -1

        self.use_ethernet_only = bool(self.config.get("use_ethernet_only", True))
        self.ethernet_ip = get_ethernet_ip()
        self.last_eth_check = 0.0

        # Target color from screen frame in RGB space [0.0 .. 1.0]
        self.target_r = 1.0
        self.target_g = 1.0
        self.target_b = 1.0
        self.target_lum = 0.1
        self.target_peak = 0.1
        self.target_bloom = 0.0
        self.target_black = 0.0

        # Current smoothed color state in RGB space
        self.curr_r = 1.0
        self.curr_g = 1.0
        self.curr_b = 1.0
        self.curr_v = float(self.min_brightness * 10)
        self.last_sent_hsv = (-1, -1, -1)
        self.current_display_b = self.min_brightness

        self.lock = threading.Lock()
        self.running = False
        self._rgb_f32 = np.empty((HEIGHT, WIDTH, 3), dtype=np.float32)

        self.local_device = None
        self.cloud_client = None
        self.mode = "Inactive"
        self.local_offline_since = 0.0

        # Async cloud worker state so HTTPS never stalls local TCP loop
        self._cloud_pending = None
        self._cloud_event = threading.Event()
        self._cloud_lock = threading.Lock()

        if self.enabled:
            # Auto-retrieve local key from Cloud if missing but cloud credentials exist
            if HAS_TINYTUYA and not self.local_key and self.config.get("access_id") and self.config.get("access_secret"):
                self._fetch_local_key_from_cloud()

            # Initialize local device bound strictly to Ethernet if enabled
            if HAS_TINYTUYA and self.local_ip and self.local_key:
                bind_ip = self.ethernet_ip if self.use_ethernet_only else None
                self.local_device = TuyaLocalDevice(
                    device_id=self.device_id,
                    local_ip=self.local_ip,
                    local_key=self.local_key,
                    version=self.protocol_version,
                    bind_ip=bind_ip
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
            "min_brightness": 10,
            "max_brightness": 100,
            "dynamic_brightness": True,
            "phone_flash_enabled": True,
            "use_ethernet_only": True,
            "power_on": True,
            "white_mode": False,
            "white_temp": 500,
            "saturation_boost": 1.8,
            "smoothing_alpha": 0.65,
            "local_refresh_hz": 10.0,
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
            cfg_dir = os.path.dirname(os.path.abspath(self.config_path))
            os.makedirs(cfg_dir, exist_ok=True)
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

    def set_refresh_hz(self, hz: float):
        clamped = max(2.0, min(30.0, float(hz)))
        with self.lock:
            self.local_hz = clamped
            self.config["local_refresh_hz"] = clamped
        self.save_config()

    def set_smoothing_alpha(self, alpha: float):
        clamped = max(0.20, min(0.95, float(alpha)))
        with self.lock:
            self.alpha = clamped
            self.config["smoothing_alpha"] = clamped
        self.save_config()

    def force_reconnect(self) -> bool:
        if self.local_device:
            self.local_device.disconnect()
            eth = get_ethernet_ip()
            if self.use_ethernet_only:
                self.ethernet_ip = eth
                self.local_device.set_bind_ip(eth)
            else:
                self.local_device.set_bind_ip(None)
            success = self.local_device.connect()
            if success:
                self.local_offline_since = 0.0
                self.mode = "Local Ethernet" if getattr(self.local_device, 'bind_ip', None) else "Local Wi-Fi"
            elif self.cloud_client:
                self.mode = "Cloud OpenAPI"
            else:
                self.mode = "Disconnected"
            return success
        return False

    def set_dynamic_brightness(self, enable: bool):
        with self.lock:
            self.dynamic_brightness = bool(enable)
            self.config["dynamic_brightness"] = self.dynamic_brightness
        self.save_config()

    def set_phone_flash(self, enable: bool):
        with self.lock:
            self.phone_flash_enabled = bool(enable)
            self.config["phone_flash_enabled"] = self.phone_flash_enabled
        self.save_config()

    def set_ethernet_only(self, enable: bool):
        with self.lock:
            self.use_ethernet_only = bool(enable)
            self.config["use_ethernet_only"] = self.use_ethernet_only
            self.ethernet_ip = get_ethernet_ip()
            bind_ip = self.ethernet_ip if self.use_ethernet_only else None
        self.save_config()
        if self.local_device:
            self.local_device.set_bind_ip(bind_ip)
            self.local_device.connect()

    def set_power(self, enable: bool):
        with self.lock:
            self.power_on = bool(enable)
            self.config["power_on"] = self.power_on
            self.last_sent_white = -1
            self.last_sent_hsv = (-1, -1, -1)
        self.save_config()
        if self.local_device:
            self.local_device.set_power(self.power_on)
        elif self.cloud_client:
            self._dispatch_cloud("power", (self.power_on,))

    def set_white_mode(self, enable: bool):
        with self.lock:
            self.white_mode = bool(enable)
            self.config["white_mode"] = self.white_mode
            self.last_sent_white = -1
            self.last_sent_hsv = (-1, -1, -1)
        self.save_config()
        if self.white_mode:
            b_val = int(self.brightness * 10)
            if self.local_device:
                self.local_device.send_white(b_val, self.white_temp)
            elif self.cloud_client:
                self._dispatch_cloud("white", (b_val,))

    def _dispatch_cloud(self, kind: str, args: tuple):
        """Queues only the latest cloud command so slow HTTPS calls never stall the local loop."""
        if not self.cloud_client:
            return
        with self._cloud_lock:
            self._cloud_pending = (kind, args)
        self._cloud_event.set()

    def _cloud_worker_loop(self):
        last_cloud_send = 0.0
        while self.running:
            self._cloud_event.wait(timeout=0.5)
            self._cloud_event.clear()
            if not self.running:
                break

            with self._cloud_lock:
                item = self._cloud_pending
                self._cloud_pending = None
            if not item or not self.cloud_client:
                continue

            # Rate-limit cloud calls
            elapsed = time.time() - last_cloud_send
            if elapsed < self.cloud_refresh:
                time.sleep(self.cloud_refresh - elapsed)

            kind, args = item
            ok = False
            if kind == "hsv":
                ok = self.cloud_client.send_color(*args)
            elif kind == "white":
                ok = self.cloud_client.send_white(*args)
            elif kind == "power":
                ok = self.cloud_client.set_power(*args)
            last_cloud_send = time.time()

            if ok and (not self.local_device or not self.local_device.is_connected):
                self.mode = "Cloud OpenAPI"

    def update_frame(self, arr: np.ndarray):
        """
        Extracts natural screen average RGB, ambient luminance, and saturated bloom highlights.
        Zero-allocation via pre-allocated float32 buffer.
        """
        if not self.enabled or self.white_mode:
            return

        try:
            np.multiply(arr, 1.0 / 255.0, out=self._rgb_f32, dtype=np.float32)
            r = self._rgb_f32[..., 0]
            g = self._rgb_f32[..., 1]
            b = self._rgb_f32[..., 2]

            avg_r = float(np.mean(r))
            avg_g = float(np.mean(g))
            avg_b = float(np.mean(b))

            cmax = np.maximum(np.maximum(r, g), b)
            cmin = np.minimum(np.minimum(r, g), b)
            delta = cmax - cmin

            sat = np.zeros_like(cmax)
            mask_nz = cmax > 0.001
            sat[mask_nz] = delta[mask_nz] / cmax[mask_nz]

            lum_map = 0.299 * r + 0.587 * g + 0.114 * b
            mean_lum = float(np.mean(lum_map))
            p90_lum = float(np.percentile(lum_map, 90))
            bloom_p90 = float(np.percentile(sat * cmax, 90))
            black_frac = float(np.mean(lum_map < 0.12))

            with self.lock:
                self.target_r = avg_r
                self.target_g = avg_g
                self.target_b = avg_b
                self.target_lum = mean_lum
                self.target_peak = p90_lum
                self.target_bloom = bloom_p90
                self.target_black = black_frac
        except Exception:
            pass

    def _gradient_sync_loop(self):
        """
        Runs smooth direct color cross-fade in RGB space towards target frame
        and smoothly eases dynamic brightness as a gradient.
        Streams to the bulb over persistent TCP socket (with non-blocking Cloud fallback).
        """
        time.sleep(0.3)
        while self.running:
            loop_start = time.time()

            if not self.enabled:
                time.sleep(0.2)
                continue

            # Automatic fallback / switch between USB Ethernet and local Wi-Fi route:
            if loop_start - self.last_eth_check >= 1.0:
                self.last_eth_check = loop_start
                if self.use_ethernet_only:
                    detected_eth = get_ethernet_ip()
                    if detected_eth != self.ethernet_ip:
                        old_eth = self.ethernet_ip
                        self.ethernet_ip = detected_eth
                        if self.local_device:
                            self.local_device.disconnect()
                            self.local_device.set_bind_ip(detected_eth)
                            self.local_device.connect()
                        if old_eth and not detected_eth:
                            print(f"\n[*] Phone unplugged: automatic fallback to local Wi-Fi for bulb streaming")
                        elif detected_eth and not old_eth:
                            print(f"\n[*] Phone plugged in: switched bulb streaming to USB Ethernet ({detected_eth})")
                    elif self.local_device and not self.local_device.is_connected:
                        self.local_device.connect()

            with self.lock:
                power = self.power_on
                w_mode = self.white_mode
                bright_pct = self.brightness
                min_b = self.min_brightness
                dyn = self.dynamic_brightness
                alpha = self.alpha
                tr = self.target_r
                tg = self.target_g
                tb = self.target_b
                tlum = self.target_lum
                tpeak = self.target_peak
                tbloom = self.target_bloom
                tblack = self.target_black
                w_temp = self.white_temp
                hz = self.local_hz

            is_local = (self.local_device is not None and self.local_device.is_connected)
            interval = (1.0 / hz) if (is_local or not self.cloud_client) else self.cloud_refresh

            if not power:
                time.sleep(interval)
                continue

            # If Bulb is in Normal White Mode:
            if w_mode:
                b_val = int(bright_pct * 10)
                if b_val != self.last_sent_white:
                    sent = False
                    if self.local_device:
                        sent = self.local_device.send_white(b_val, w_temp)
                        if sent:
                            self.local_offline_since = 0.0
                            self.mode = "Local Ethernet" if getattr(self.local_device, 'bind_ip', None) else "Local Wi-Fi"
                    if not sent and self.cloud_client:
                        now = time.time()
                        if self.local_offline_since == 0.0:
                            self.local_offline_since = now
                        if not self.local_device or (now - self.local_offline_since >= 5.0):
                            self._dispatch_cloud("white", (b_val,))
                            sent = True
                    if sent:
                        self.last_sent_white = b_val
                elif self.local_device and self.local_device.is_connected:
                    if not self.local_device.poll_and_heartbeat(time.time()):
                        self.local_device.connect()
                time.sleep(interval)
                continue

            # Display Sync Mode (Eased RGB linear cross-fade with adaptive snappiness):
            color_delta = max(abs(tr - self.curr_r), abs(tg - self.curr_g), abs(tb - self.curr_b))
            eff_alpha = min(0.95, alpha * 1.5) if color_delta > 0.15 else alpha
            self.curr_r += eff_alpha * (tr - self.curr_r)
            self.curr_g += eff_alpha * (tg - self.curr_g)
            self.curr_b += eff_alpha * (tb - self.curr_b)

            if dyn:
                eff_light = max(tlum, 0.35 * tpeak, 0.45 * tbloom)
                dark_factor = max(0.0, min(1.0, (tblack - 0.20) / 0.60)) if tlum < 0.35 else 0.0
                scene_factor = max(0.0, min(1.0, (eff_light / 0.35) ** 0.75))
                if dark_factor > 0:
                    scene_factor *= (1.0 - dark_factor * 0.90)

                base_v = min_b + (bright_pct - min_b) * scene_factor

                min_rgb = min(tr, tg, tb)
                if min_rgb > 0.65 and tlum > 0.65:
                    white_progress = min(1.0, (min_rgb - 0.65) / 0.30)
                    target_v = base_v + (100.0 - base_v) * white_progress
                else:
                    target_v = base_v

                target_v = max(float(min_b), min(100.0, target_v))
            else:
                target_v = float(bright_pct)

            target_v_tuya = target_v * 10.0
            v_delta = abs(target_v_tuya - self.curr_v) / 1000.0
            eff_alpha_v = min(0.95, alpha * 1.5) if v_delta > 0.15 else alpha
            self.curr_v += eff_alpha_v * (target_v_tuya - self.curr_v)

            cr, cg, cb = self.curr_r, self.curr_g, self.curr_b
            cmax = max(cr, cg, cb)
            cmin = min(cr, cg, cb)
            delta = cmax - cmin

            if delta < 1e-4 or cmax < 1e-4:
                h_tuya = 0
                s_tuya = 0
            else:
                if cmax == cr:
                    h = ((cg - cb) / delta) % 6.0
                elif cmax == cg:
                    h = ((cb - cr) / delta) + 2.0
                else:
                    h = ((cr - cg) / delta) + 4.0
                h_tuya = int((h * 60.0) % 360.0)

                s_raw = delta / cmax
                if s_raw < 0.08:
                    s_tuya = 0
                    h_tuya = 0
                else:
                    progress = min(1.0, (s_raw - 0.08) / 0.92)
                    s_tuya = int(250 + progress * 750)

            v_tuya = int(max(min_b * 10, min(1000, self.curr_v)))
            self.current_display_b = int(v_tuya / 10)

            # Deadband check: send if changed, or periodic keepalive every 3.5s if idle
            last_h, last_s, last_v = self.last_sent_hsv
            th_h = 2 if is_local else 4
            th_s = 4 if is_local else 10
            th_v = 4 if is_local else 8
            now = time.time()
            idle_keepalive = (
                is_local
                and self.local_device
                and (now - self.local_device.last_send_time >= 3.5)
            )
            changed = (
                abs(h_tuya - last_h) >= th_h
                or abs(s_tuya - last_s) >= th_s
                or abs(v_tuya - last_v) >= th_v
            )

            if changed or idle_keepalive:
                sent = False
                if self.local_device:
                    if changed:
                        sent = self.local_device.send_hsv(h_tuya, s_tuya, v_tuya)
                    else:
                        # Screen is static: send 0x09 heartbeat (or re-send HSV) to keep MCU awake
                        sent = self.local_device.poll_and_heartbeat(now)

                    if sent:
                        self.local_offline_since = 0.0
                        self.mode = "Local Ethernet" if (getattr(self.local_device, 'bind_ip', None) and self.local_device.bind_fail_count < 2) else "Local Wi-Fi"

                if not sent and changed and self.cloud_client:
                    if self.local_offline_since == 0.0:
                        self.local_offline_since = now
                    if not self.local_device or (now - self.local_offline_since >= 5.0):
                        self._dispatch_cloud("hsv", (h_tuya, s_tuya, v_tuya))
                        sent = True

                if sent and changed:
                    self.last_sent_hsv = (h_tuya, s_tuya, v_tuya)
                    self.last_sent_white = -1
                elif not sent:
                    self.mode = "Disconnected"
            elif self.local_device and self.local_device.is_connected:
                # Drain delayed ACKs on every tick and immediately detect if bulb closed socket
                if not self.local_device.poll_and_heartbeat(now):
                    self.local_device.connect()

            elapsed = time.time() - loop_start
            sleep_for = interval - elapsed
            if sleep_for > 0.002:
                time.sleep(sleep_for)

    def start(self):
        if not self.enabled:
            return

        self.running = True

        # Try initial local connect
        if self.local_device:
            if self.local_device.connect():
                self.mode = "Local Ethernet" if getattr(self.local_device, 'bind_ip', None) else "Local Wi-Fi"
                bind_note = f"Bound to Ethernet: {self.local_device.bind_ip}" if self.local_device.bind_ip else "Default Local Route (Wi-Fi)"
                print(f"[+] Tuya Bulb Local Sync Active: {self.local_ip}:6668 ({bind_note}, ~{int(self.local_hz)}Hz)")
            else:
                print(f"[!] Local connection to {self.local_ip} pending, will keep retrying locally.")

        if not self.mode.startswith("Local") and self.cloud_client:
            self.mode = "Cloud OpenAPI"
            print(f"[+] Tuya Bulb Cloud Fallback Ready: Device {self.device_id[:6]}... (Region: {self.config.get('region')})")

        dyn_str = "Dynamic Brightness" if self.dynamic_brightness else "Static Brightness"
        print(f"[+] Tuya Brightness: {self.brightness}% ({dyn_str}, Min {self.min_brightness}%)")

        if self.cloud_client:
            threading.Thread(target=self._cloud_worker_loop, daemon=True).start()
        threading.Thread(target=self._gradient_sync_loop, daemon=True).start()
        self._start_web_server()

    def stop(self):
        self.running = False
        self._cloud_event.set()
        if self.local_device:
            self.local_device.close()

    def mode_short(self) -> str:
        if not self.power_on:
            return "Off"
        if self.mode in ("Local Ethernet", "Local Wi-Fi"):
            base = self.mode
        elif self.mode.startswith("Local"):
            base = self.mode
        elif self.mode == "Disconnected":
            base = "Offline"
        else:
            base = "Cloud"
        if self.white_mode:
            return f"{base} White"
        return base

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
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"service":"display-sync","status":"running"}')

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

                elif parsed.path == "/refresh_hz":
                    qs = parse_qs(parsed.query)
                    if "val" in qs:
                        try:
                            sync_self.set_refresh_hz(float(qs["val"][0]))
                        except Exception:
                            pass
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')

                elif parsed.path == "/smoothing":
                    qs = parse_qs(parsed.query)
                    if "val" in qs:
                        try:
                            sync_self.set_smoothing_alpha(float(qs["val"][0]) / 100.0)
                        except Exception:
                            pass
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')

                elif parsed.path == "/reconnect":
                    unlock_and_launch_phone()
                    ok = sync_self.force_reconnect()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}' if ok else b'{"status":"failed"}')

                elif parsed.path in ("/wake_phone", "/reconnect_phone"):
                    unlock_and_launch_phone()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')

                elif parsed.path == "/power_toggle":
                    qs = parse_qs(parsed.query)
                    if "val" in qs:
                        sync_self.set_power(qs["val"][0] == "1")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')

                elif parsed.path == "/ethernet_toggle":
                    qs = parse_qs(parsed.query)
                    if "val" in qs:
                        sync_self.set_ethernet_only(qs["val"][0] == "1")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')

                elif parsed.path == "/white_mode":
                    qs = parse_qs(parsed.query)
                    if "val" in qs:
                        sync_self.set_white_mode(qs["val"][0] == "1")
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

                elif parsed.path == "/phone_flash":
                    qs = parse_qs(parsed.query)
                    if "val" in qs:
                        sync_self.set_phone_flash(qs["val"][0] == "1")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')

                elif parsed.path == "/status":
                    with sync_self.lock:
                        b = sync_self.brightness
                        dyn = sync_self.dynamic_brightness
                        flash = sync_self.phone_flash_enabled
                        white = sync_self.white_mode
                        eth = sync_self.use_ethernet_only
                        eth_ip = sync_self.ethernet_ip
                        power = sync_self.power_on
                        cur_hz = sync_self.local_hz
                        cur_alpha = sync_self.alpha
                        mode_str = sync_self.mode
                        if not power:
                            r_d, g_d, b_d = 40, 40, 40
                            display_mode = f"{mode_str} (Off)"
                        elif white:
                            r_d, g_d, b_d = 255, 255, 255
                            display_mode = f"{mode_str} (White)"
                        else:
                            r_d = int(max(0.0, min(1.0, sync_self.curr_r)) * 255)
                            g_d = int(max(0.0, min(1.0, sync_self.curr_g)) * 255)
                            b_d = int(max(0.0, min(1.0, sync_self.curr_b)) * 255)
                            display_mode = mode_str

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    adb_connected = check_adb_device()
                    self.wfile.write(json.dumps({
                        "brightness": b,
                        "dynamic": dyn,
                        "phone_flash": flash,
                        "white_mode": white,
                        "ethernet_only": eth,
                        "ethernet_ip": eth_ip,
                        "power": power,
                        "refresh_hz": cur_hz,
                        "smoothing_alpha": cur_alpha,
                        "mode": display_mode,
                        "r": r_d, "g": g_d, "b": b_d,
                        "adb_connected": adb_connected,
                        "adb_status": "Connected" if adb_connected else "Disconnected"
                    }).encode('utf-8'))
                else:
                    self.send_response(404)
                    self.end_headers()

        class ReusableHTTPServer(HTTPServer):
            allow_reuse_address = True

        def run_server():
            try:
                server = ReusableHTTPServer(("127.0.0.1", self.web_port), SliderHandler)
                print(f"[+] Display Sync JSON Control API running at: http://127.0.0.1:{self.web_port}")
                server.serve_forever()
            except Exception as e:
                print(f"[!] Warning: Control API server failed on port {self.web_port}: {e}")

        threading.Thread(target=run_server, daemon=True).start()


# ==============================================================================
# Screen Capture & Multi-Device Dispatcher (Mutter / PipeWire)
# ==============================================================================

class MutterScreenSync:
    def __init__(self, port=PORT, fps=FPS_TARGET, auto_dismiss_ui=True, skip_phone=False,
                 tuya_brightness=None, tuya_id=None, tuya_secret=None, tuya_device=None,
                 tuya_region=None, tuya_ip=None, tuya_key=None, tuya_dynamic=None, tuya_hz=None):
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
        self.last_vibe = -1
        self._last_u64 = np.zeros(WIDTH * HEIGHT * 3 // 8, dtype=np.uint64)
        self._payload = bytearray(4)
        self.last_print_time = 0.0
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
                cli_dynamic=tuya_dynamic,
                cli_hz=tuya_hz
            )
            if self.tuya.enabled:
                self.tuya.start()
        except Exception as e:
            print(f"[!] Warning: Tuya init failed: {e}")

    def _phone_connect_worker(self):
        """Asynchronously connects to the phone in the background without blocking screen capture or Tuya sync."""
        time.sleep(0.5)
        last_wake_attempt = 0.0
        while self.phone_worker_running:
            if self.sock is not None:
                time.sleep(1.0)
                continue

            try:
                if check_adb_device():
                    now = time.time()
                    if now - last_wake_attempt >= 5.0:
                        last_wake_attempt = now
                        unlock_and_launch_phone()
                        time.sleep(0.3)

                setup_adb_forward(self.port)
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(2.0)
                s.connect((HOST, self.port))
                s.settimeout(None)
                self.sock = s
                print("\n[+] Connected to Android phone app!")
                if self.auto_dismiss_ui:
                    dismiss_initial_ui()
            except (ConnectionRefusedError, socket.timeout, OSError):
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
            curr_u64 = np.frombuffer(map_info.data, dtype=np.uint64)
            if np.array_equal(curr_u64, self._last_u64):
                is_duplicate = True
            else:
                is_duplicate = False
                self._last_u64[:] = curr_u64

            arr = np.frombuffer(map_info.data, dtype=np.uint8).reshape((HEIGHT, WIDTH, 3))

            # Phone Ambient Color (Strictly UNCHANGED)
            avg_rgb = arr.mean(axis=(0, 1)).astype(np.uint8)
            r, g, b = int(avg_rgb[0]), int(avg_rgb[1]), int(avg_rgb[2])

            with vibration_lock:
                flash_allowed = self.tuya.phone_flash_enabled if self.tuya else True
                vibe = current_vibration if flash_allowed else 0

            self._payload[0] = r
            self._payload[1] = g
            self._payload[2] = b
            self._payload[3] = vibe

            # 1. Stream to Phone over ADB USB
            if self.sock:
                try:
                    self.sock.sendall(self._payload)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None

            # 2. Feed frame to Tuya bulb controller
            if self.tuya and self.tuya.enabled and not is_duplicate:
                self.tuya.update_frame(arr)

            # 3. Terminal status display
            now = time.time()
            if sys.stdout.isatty() and ((r, g, b) != self.last_rgb or vibe != self.last_vibe) and (now - self.last_print_time >= 0.35):
                self.last_print_time = now
                self.last_rgb = (r, g, b)
                self.last_vibe = vibe
                flash_str = "" if flash_allowed else " [Flash: Off]"
                bulb_str = f" | Bulb ({self.tuya.mode_short()}): {self.tuya.current_brightness_display()}%" if (self.tuya and self.tuya.enabled) else ""
                phone_str = f" | Phone: Active{flash_str}" if self.sock else " | Phone: Connecting..."
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

        if not self.skip_phone:
            setup_adb_forward(self.port)
            threading.Thread(target=self._phone_connect_worker, daemon=True).start()

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
    parser.add_argument("--fps", type=int, default=FPS_TARGET, help="Target FPS for screen sync (default 60)")
    parser.add_argument("--simulate-vibration", action="store_true", help="Simulate vibration every 4s to test torch")
    parser.add_argument("--controller", type=str, default=None, help="Explicit input device node (e.g. /dev/input/event17)")
    parser.add_argument("--no-unlock", action="store_true", help="Skip waking/unlocking phone on launch")
    parser.add_argument("--no-dismiss-ui", action="store_true", help="Do not auto-dismiss phone UI")
    parser.add_argument("--no-phone", action="store_true", help="Skip phone connection and sync only Tuya bulb")
    parser.add_argument("--tuya-brightness", type=int, default=None, help="Tuya bulb brightness percentage (10-100)")
    parser.add_argument("--tuya-id", type=str, default=None, help="Tuya Cloud Access ID")
    parser.add_argument("--tuya-secret", type=str, default=None, help="Tuya Cloud Access Secret")
    parser.add_argument("--tuya-device", type=str, default=None, help="Tuya Bulb Device ID")
    parser.add_argument("--tuya-region", type=str, default=None, help="Tuya Cloud Region (in, us, eu, cn)")
    parser.add_argument("--tuya-ip", type=str, default=None, help="Tuya bulb local IP on Wi-Fi (e.g. 192.168.1.74)")
    parser.add_argument("--tuya-key", type=str, default=None, help="Tuya bulb local 16-character encryption key")
    parser.add_argument("--tuya-dynamic", action="store_true", default=None, help="Enable dynamic brightness shifts")
    parser.add_argument("--no-tuya-dynamic", dest="tuya_dynamic", action="store_false", help="Disable dynamic brightness shifts")
    parser.add_argument("--tuya-hz", type=float, default=None, help="Tuya bulb local refresh rate in Hz (default 10)")
    args = parser.parse_args()

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
        tuya_dynamic=args.tuya_dynamic,
        tuya_hz=args.tuya_hz
    )

    def sig_handler(*_):
        cleanup_phone_and_exit(sync)
        if sync.loop and sync.loop.is_running():
            sync.loop.quit()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    try:
        try:
            from gi.repository import GLibUnix
            signal_add_fn = GLibUnix.signal_add
        except (ImportError, AttributeError):
            signal_add_fn = GLib.unix_signal_add

        def glib_sig():
            cleanup_phone_and_exit(sync)
            if sync.loop and sync.loop.is_running():
                sync.loop.quit()
            return GLib.SOURCE_REMOVE

        signal_add_fn(GLib.PRIORITY_HIGH, signal.SIGINT, glib_sig)
        signal_add_fn(GLib.PRIORITY_HIGH, signal.SIGTERM, glib_sig)
    except Exception:
        pass
    if not sync.skip_phone:
        atexit.register(cleanup_phone_and_exit, sync)

    vibe_thread = threading.Thread(
        target=monitor_controller_vibration,
        args=(args.controller, args.simulate_vibration),
        daemon=True
    )
    vibe_thread.start()

    sync.run()


if __name__ == "__main__":
    main()
