# 🔦 Flashlight - Android Torch App

A modern, sleek Android flashlight application with **brightness control** and **strobe mode**. Built with Kotlin and the Camera2 API.

<p align="center">
  <a href="https://github.com/rtvkiz/Flashlight/releases/latest/download/app-debug.apk">
    <img src="https://img.shields.io/badge/📥_Download_APK-v1.0.0-brightgreen?style=for-the-badge" alt="Download APK">
  </a>
</p>

![Min SDK](https://img.shields.io/badge/Min%20SDK-23-blue)
![Target SDK](https://img.shields.io/badge/Target%20SDK-34-green)
![Kotlin](https://img.shields.io/badge/Kotlin-1.9.24-purple)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Release](https://img.shields.io/github/v/release/rtvkiz/Flashlight?color=blue)

---

## 📥 Quick Install

1. **[Download the APK](https://github.com/rtvkiz/Flashlight/releases/latest/download/app-debug.apk)** on your Android phone
2. Open the downloaded file
3. Tap **Install** (enable "Install from unknown sources" if prompted)
4. Done! 🎉

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 📱 **One-Tap Toggle** | Tap the power button to turn flashlight on/off |
| 🔆 **Brightness Control** | Adjust flashlight intensity with a smooth slider (Android 13+ with supported hardware) |
| ⚡ **Strobe Mode** | Blinking light effect with adjustable speed (1-20 Hz) |
| 💫 **Smooth Animations** | Satisfying button animations and glow effects |
| 📳 **Haptic Feedback** | Tactile vibration feedback for all interactions |
| 🔒 **Permission Handling** | Graceful camera permission requests |
| ⚡ **Battery Efficient** | Automatically turns off when app goes to background |

## 📱 Screenshots

The app features a **deep dark theme** with **electric amber accents** for a premium look and feel.

## 📋 Requirements

- **Android 6.0** (API 23) or higher
- Device with camera flash
- For brightness control: **Android 13** (API 33) or higher with supported hardware

## 🔧 Brightness Control Support

The brightness/strength control feature uses the `Camera2 API` introduced in Android 13 (Tiramisu).

| Device | Support |
|--------|---------|
| Android 13+ with hardware support | ✅ Full brightness control |
| Older devices or unsupported hardware | ⚡ Standard on/off only |

The app automatically detects your device's capabilities and adjusts the UI accordingly.

## ⚡ Strobe Mode

The strobe feature provides a blinking light effect:
- **Speed Range**: 1 Hz (slow) to 20 Hz (fast)
- **Use Cases**: Signaling, parties, emergency attention
- **Safety**: Automatically stops when app goes to background

## 🛠️ Building the Project

### Prerequisites
- Android Studio Hedgehog (2023.1.1) or later
- JDK 17 or 21

### Build Steps

1. Clone this repository
```bash
git clone https://github.com/YOUR_USERNAME/flashlight.git
cd flashlight
```

2. Open in Android Studio or build from command line
```bash
# Build debug APK
./gradlew assembleDebug

# Build release APK
./gradlew assembleRelease

# Install on connected device
./gradlew installDebug
```

3. APK location: `app/build/outputs/apk/debug/app-debug.apk`

## 📁 Project Structure

```
app/
├── src/main/
│   ├── java/com/flashlight/torch/
│   │   └── MainActivity.kt          # Main activity with flashlight logic
│   ├── res/
│   │   ├── layout/
│   │   │   └── activity_main.xml    # Main UI layout
│   │   ├── drawable/                # Custom drawables & icons
│   │   ├── values/
│   │   │   ├── colors.xml           # Color palette
│   │   │   ├── strings.xml          # String resources
│   │   │   ├── themes.xml           # App theme
│   │   │   └── dimens.xml           # Dimensions
│   │   └── mipmap-*/                # App icons
│   └── AndroidManifest.xml          # App manifest
└── build.gradle.kts                  # App-level build config
```

## 🎨 Design

The app features a **deep dark theme** with **electric amber accents**:

| Color | Hex | Usage |
|-------|-----|-------|
| Background | `#0A0A0F` | Main background |
| Surface | `#1E1E2E` | Cards and elevated surfaces |
| Primary | `#FFB800` | Accent color, active states |
| Text Primary | `#FFFFFF` | Main text |
| Text Secondary | `#8A8A9A` | Subtle text |

## 🔐 Permissions

| Permission | Purpose |
|------------|---------|
| `CAMERA` | Required to control the flashlight LED |
| `FLASHLIGHT` | Legacy permission for flash access |
| `VIBRATE` | Haptic feedback on button presses |

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🤝 Contributing

Contributions are welcome! Feel free to:
- 🐛 Report bugs
- 💡 Suggest features
- 🔧 Submit pull requests

## ⚠️ Disclaimer

- **Strobe Warning**: Flashing lights may cause discomfort or seizures in people with photosensitive epilepsy. Use strobe mode responsibly.
- The app requires a device with a camera flash to function.

---

Made with ❤️ for Android
