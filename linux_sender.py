#!/usr/bin/env python3
"""
Mutter ScreenCast (PipeWire) Downscaled-at-the-Source Display Sync & Rumble Interceptor
---------------------------------------------------------------------------------------
- Unlocks phone, launches app, and taps to enter fullscreen mode automatically on start.
- Negotiates 16x9 (144 pixels) directly with Mutter / PipeWire inside the compositor pass.
- Vectorized NumPy mean over 144 pixels (virtually 0% CPU overhead).
- Intercepts controller force-feedback rumble events (EV_FF).
- Streams 4-byte payload [R, G, B, VIBRATION] over ADB USB (tcp:5555).
- On exit (Ctrl-C or close): cleanly closes app, turns off phone screen, and exits.
"""

import sys
import os
import time
import socket
import signal
import atexit
import argparse
import threading
import subprocess

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gio, GLib, Gst
import numpy as np

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

current_vibration = 0
vibration_lock = threading.Lock()
is_cleaning_up = False


def unlock_and_launch_phone():
    """Wakes the phone, dismisses the lockscreen, and launches the app in fullscreen via ADB."""
    print("[*] Waking phone, unlocking, and launching app via ADB...")
    try:
        # 1. Wake screen if asleep (KEYCODE_WAKEUP = 224)
        subprocess.run(["adb", "shell", "input", "keyevent", "224"], check=False)
        # 2. Dismiss keyguard cleanly via KEYCODE_MENU (82)
        subprocess.run(["adb", "shell", "wm", "dismiss-keyguard"], check=False)
        subprocess.run(["adb", "shell", "input", "keyevent", "82"], check=False)
        time.sleep(0.3)
        # 3. Launch the app in foreground with fullscreen intent extra
        subprocess.run([
            "adb", "shell", "am", "start",
            "-W",
            "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER",
            "-n", "com.flashlight.torch/.MainActivity",
            "--ez", "hide_controls", "true"
        ], check=False)
        # 4. Broadcast HIDE_UI to ensure fullscreen
        subprocess.run(["adb", "shell", "am", "broadcast", "-a", "com.flashlight.torch.HIDE_UI"], check=False)
        print("[+] Phone unlocked and app running in fullscreen ambient mode.")
    except Exception as e:
        print(f"[!] Warning: ADB unlock/launch failed: {e}")


def dismiss_initial_ui():
    """Ensures controls are hidden in the app upon connection."""
    try:
        subprocess.run(["adb", "shell", "am", "broadcast", "-a", "com.flashlight.torch.HIDE_UI"], check=False)
    except Exception:
        pass


def cleanup_phone_and_exit(sync_instance=None):
    """Turns off phone screen, closes app, stops pipeline, and exits."""
    global is_cleaning_up
    if is_cleaning_up:
        return
    is_cleaning_up = True

    print("\n[*] Shutting down: closing app and turning off phone screen...")
    try:
        # Force-stop the app
        subprocess.run(["adb", "shell", "am", "force-stop", "com.flashlight.torch"], check=False)
        # Put phone screen to sleep
        subprocess.run(["adb", "shell", "input", "keyevent", "223"], check=False)
        print("[+] Phone screen turned off and app closed.")
    except Exception as e:
        print(f"[!] Warning: ADB cleanup failed: {e}")

    if sync_instance:
        sync_instance.stop()


def setup_adb_forward(port: int):
    """Sets up ADB forward once to route traffic over USB debug link."""
    print(f"[*] Setting up ADB forward: tcp:{port} -> tcp:{port}...")
    try:
        subprocess.run(["adb", "forward", f"tcp:{port}", f"tcp:{port}"], check=False)
        print("[+] ADB forward active.")
    except FileNotFoundError:
        print("[!] Warning: adb not found in PATH.")


def monitor_controller_vibration(device_path=None, simulate=False):
    """Listens for gamepad rumble events (EV_FF) with hotplug support and pulse decay."""
    global current_vibration

    if simulate:
        print("[*] Simulation: pulsing vibration every 4s to test torch.")
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
            time.sleep(1.5)
            continue

        print(f"[+] Controller rumble active on: {[d.name for d in target_devices]}")

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


class MutterScreenSync:
    def __init__(self, port=PORT, fps=FPS_TARGET, auto_dismiss_ui=True):
        self.port = port
        self.fps = fps
        self.auto_dismiss_ui = auto_dismiss_ui
        self.sock = None
        self.pipeline = None
        self.loop = GLib.MainLoop()
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sess_proxy = None
        self.last_rgb = (-1, -1, -1)

    def connect_socket(self):
        print(f"[*] Connecting to Android via ADB port {self.port}...")
        while True:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.connect((HOST, self.port))
                self.sock = s
                print("[+] Connected to Android app!")
                if self.auto_dismiss_ui:
                    dismiss_initial_ui()
                break
            except (ConnectionRefusedError, OSError):
                time.sleep(1.0)

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

            if self.sock:
                try:
                    self.sock.sendall(payload)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    print("[!] Connection lost. Reconnecting socket...")
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                    threading.Thread(target=self.connect_socket, daemon=True).start()

            if (r, g, b) != self.last_rgb:
                self.last_rgb = (r, g, b)
                print(f"\r[*] Live Screen Sync -> RGB: ({r:3d}, {g:3d}, {b:3d}) | Vibe: {vibe:3d}   ", end="", flush=True)

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
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def run(self):
        Gst.init(None)
        setup_adb_forward(self.port)
        self.connect_socket()

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
            cleanup_phone_and_exit(self)


def main():
    parser = argparse.ArgumentParser(description="Mutter ScreenCast (PipeWire) Downscaled Screen Sync")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--fps", type=int, default=FPS_TARGET)
    parser.add_argument("--simulate-vibration", action="store_true")
    parser.add_argument("--controller", type=str, default=None)
    parser.add_argument("--no-unlock", action="store_true", help="Skip waking/unlocking phone on launch")
    parser.add_argument("--no-dismiss-ui", action="store_true", help="Do not auto-tap to enter fullscreen")
    args = parser.parse_args()

    # 1. Unlock phone and launch app unless skipped
    if not args.no_unlock:
        unlock_and_launch_phone()

    sync = MutterScreenSync(port=args.port, fps=args.fps, auto_dismiss_ui=not args.no_dismiss_ui)

    # 2. Register signal handlers for clean exit on Ctrl-C or SIGTERM
    def sig_handler(*_):
        cleanup_phone_and_exit(sync)
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
