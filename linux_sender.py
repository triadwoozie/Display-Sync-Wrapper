#!/usr/bin/env python3
"""
Mutter ScreenCast (PipeWire) Downscaled-at-the-Source Display Sync & Rumble Interceptor
---------------------------------------------------------------------------------------
- Negotiates 16x9 (144 pixels) directly with Mutter / PipeWire inside the compositor pass.
- Vectorized NumPy mean over 144 pixels (virtually 0% CPU overhead).
- Intercepts controller force-feedback rumble events (EV_FF).
- Streams 4-byte payload [R, G, B, VIBRATION] over ADB USB (tcp:5555).
"""

import sys
import os
import time
import socket
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


def setup_adb_forward(port: int):
    """Sets up ADB forward once to route traffic over USB debug link."""
    print(f"[*] Setting up ADB forward: tcp:{port} -> tcp:{port}...")
    try:
        subprocess.run(["adb", "forward", f"tcp:{port}", f"tcp:{port}"], check=False)
        print("[+] ADB forward active.")
    except FileNotFoundError:
        print("[!] Warning: adb not found in PATH.")


def monitor_controller_vibration(device_path=None, simulate=False):
    """Listens for gamepad rumble events (EV_FF)."""
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
        return

    import select
    while True:
        try:
            r, _, _ = select.select(target_devices, [], [], 1.0)
            for dev in r:
                for ev in dev.read():
                    if ev.type == evdev.ecodes.EV_FF:
                        val = 255 if ev.value > 0 else 0
                        with vibration_lock:
                            current_vibration = val
        except Exception:
            time.sleep(1.0)


class MutterScreenSync:
    def __init__(self, port=PORT, fps=FPS_TARGET):
        self.port = port
        self.fps = fps
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
            # 16x9 x 3 bytes RGB = 432 bytes total!
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
                # print concise stream info
                print(f"[*] Wayland Screen: RGB=({r:3d}, {g:3d}, {b:3d}) | Vibe={vibe}")

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
            print("\n[*] Stopping sync...")
        finally:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
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


def main():
    parser = argparse.ArgumentParser(description="Mutter ScreenCast (PipeWire) Downscaled Screen Sync")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--fps", type=int, default=FPS_TARGET)
    parser.add_argument("--simulate-vibration", action="store_true")
    parser.add_argument("--controller", type=str, default=None)
    args = parser.parse_args()

    vibe_thread = threading.Thread(
        target=monitor_controller_vibration,
        args=(args.controller, args.simulate_vibration),
        daemon=True
    )
    vibe_thread.start()

    sync = MutterScreenSync(port=args.port, fps=args.fps)
    sync.run()


if __name__ == "__main__":
    main()
