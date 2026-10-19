# Display Sync Wrapper

A high-performance, ultra-low-latency ambient backlight and controller rumble synchronizer that links your Linux desktop with an Android device over USB.

- **Linux Sender**: Uses native GNOME Mutter ScreenCast and PipeWire to capture and downscale screen frames to **16×9 (144 pixels)** directly inside the compositor GPU pass. Employs vectorized NumPy math to compute average RGB values at virtually 0% CPU overhead, intercepts gamepad force-feedback (`EV_FF` / rumble) events via `evdev`, and streams 4-byte payloads over an ADB TCP socket (`127.0.0.1:5555`) with sub-5ms latency.
- **Android App**: Written in Jetpack Compose, featuring smooth color interpolation, quadratic brightness boost, Camera2 torch integration for controller rumble events, and OLED burn-in protection.

---

## Architecture Overview

```
Display-Sync-Wrapper/
├── github/
│   └── Wrapper/              # Android App Project (Jetpack Compose + Camera2 Torch)
│       ├── app/
│       │   ├── src/main/java/com/flashlight/torch/MainActivity.kt
│       │   ├── src/main/res/
│       │   └── build.gradle.kts
│       ├── build.gradle.kts
│       └── gradlew
├── gitlab/
│   └── tools/                # Linux Interception Tools (evdev interceptor)
│       ├── Makefile
│       └── intercept.c
├── linux_sender.py           # Mutter ScreenCast (PipeWire) scanner & rumble streamer
├── run_sender.sh             # Easy launcher script with environment checks
├── requirements.txt          # Python dependency list
├── .gitignore
└── README.md
```

---

## Features

### 🖥️ Linux Sender
- **Downscaled at the Source**: Negotiates a $16 \times 9$ resolution directly with Mutter / PipeWire. The GPU scales down the frame before passing buffers to user space, eliminating heavy image-processing filters and CPU spikes.
- **Rumble Interception**: Automatically detects connected gamepads with force-feedback support (`EV_FF` / `FF_RUMBLE`) and transmits vibration intensity alongside color frames.
- **Sub-5ms Latency**: Routes directly over the USB debug interface via `adb forward tcp:5555 tcp:5555` with `TCP_NODELAY`.

### 📱 Android Application
- **Server Check & Mode Dashboard**:
  - Displays real-time connection status on port 5555.
  - Live packet counter, raw RGB values, and dynamic color swatch preview.
  - Current vibration / rumble intensity display.
- **Sync Mode Toggles**:
  - **Both (Color + Flash)**: Interpolates display colors + triggers camera torch on controller rumble.
  - **Screen Colors Only**: Smooth color illumination, camera torch remains off.
  - **Vibration Flash Only**: Screen stays dark, camera torch pulses dynamically with game rumble.
- **Non-Linear Brightness Boost Curve**:
  - Slider adjustable from **`1.0x`** to **`3.5x`** (default `2.0x`).
  - Uses quadratic HSV value scaling ($\text{Boosted } V = V + (\text{Boost} - 1.0) \times V^2$):
    - **Darks stay dark**: True blacks ($V \approx 0$) are preserved to prevent washed-out light bleed.
    - **Lighter shades are amplified**: Midtones and highlights pop with brilliant, vivid radiance.
- **OLED Burn-in Protection**:
  - **Micro-jitter**: Shifts RGB values by $\pm1.5\%$ if color remains unchanged for $> 10$ seconds.
  - **OLED Sleep**: Transitions display to pure black (`#000000`) if static for $> 60$ seconds.
  - **Instant Wake**: Immediately restores full brightness as soon as screen content changes.
- **Tap-to-Toggle Immersion**:
  - Tap **"Hide Controls & Enter Fullscreen"** for zero-chrome immersion.
  - Tap anywhere on the screen at any time to reveal the controls.

---

## Prerequisites

### Linux Host
- Python 3.10+
- GStreamer with PipeWire plugins:
  - **Fedora / Nobara**: `sudo dnf install python3-numpy python3-gobject python3-evdev gstreamer1-plugins-good pipewire-gstreamer`
  - **Debian / Ubuntu**: `sudo apt install python3-numpy python3-gi python3-evdev gstreamer1.0-pipewire gstreamer1.0-plugins-good`
  - Or via pip: `pip install -r requirements.txt`
- Android Platform Tools (`adb`)

### Android Device
- Android 8.0+ (API 26+)
- USB Debugging enabled in Developer Options

### 💡 Tuya Smart Bulb Sync
- **Exponential Smoothing**: Glides colors softly using an exponential moving average (EMA) filter, preventing jerky or jarring bulb transitions.
- **Rate-Limited Dispatcher**: Decoupled from the 30 FPS phone stream, sending updates to Tuya Cloud at 2 Hz (every 500ms) with deadband filtering to respect cloud latency and API quotas.
- **Hardware Brightness Clamped**: Brightness slider range is strictly 30% to 100% to keep the bulb visible and prevent low-voltage cutoff.
- **Interactive Web Slider**: Built-in dark-mode slider accessible at `http://localhost:5556` for instant live brightness control from your browser or phone.

---

## Quick Start

### 1. Install & Launch the Android App
Build and install via Gradle and ADB:
```bash
cd github/Wrapper
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n com.flashlight.torch/.MainActivity
```

### 2. Configure Tuya Smart Bulb (Optional)
Fill in your credentials in `tuya_config.json`:
```json
{
  "enabled": true,
  "access_id": "YOUR_TUYA_ACCESS_ID",
  "access_secret": "YOUR_TUYA_ACCESS_SECRET",
  "device_id": "YOUR_TUYA_DEVICE_ID",
  "region": "in",
  "brightness": 70
}
```

### 3. Start the Linux Sender
From the repository root:
```bash
./run_sender.sh
```

Optional flags:
```bash
# Adjust Tuya bulb brightness (30-100%)
./run_sender.sh --tuya-brightness 80

# Sync only Tuya bulb (skip phone connection)
./run_sender.sh --no-phone

# Target custom framerate
./run_sender.sh --fps 60

# Test with a simulated rumble pulse (every 4 seconds)
./run_sender.sh --simulate-vibration

# Explicitly bind to a specific controller event node
./run_sender.sh --controller /dev/input/event17
```

---

## License

MIT License. See individual directories for respective third-party component licenses.
