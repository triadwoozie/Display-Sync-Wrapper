package com.flashlight.torch

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.content.BroadcastReceiver
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.*
import java.io.BufferedInputStream
import java.io.IOException
import java.net.ServerSocket
import kotlin.math.sin

enum class SyncMode(val label: String) {
    BOTH("Both (Color + Flash)"),
    SCREEN_ONLY("Screen Colors Only"),
    FLASH_ONLY("Vibration Flash Only")
}

class MainActivity : ComponentActivity() {

    // --- Torch / Flashlight Controller ---
    private var cameraManager: CameraManager? = null
    private var cameraId: String? = null
    private var maxBrightnessLevel = 1
    private var supportsBrightnessControl = false
    private var isTorchOn = false

    // --- Reactive UI States ---
    private val isConnectedState = mutableStateOf(false)
    private val displayColorState = mutableStateOf(Color(0xFF101014))
    private val rawRgbValuesState = mutableStateOf(Triple(0, 0, 0))
    private val vibrationLevelState = mutableIntStateOf(0)
    private val packetCountState = mutableLongStateOf(0L)
    private val hasCameraPermissionState = mutableStateOf(false)
    private val selectedModeState = mutableStateOf(SyncMode.BOTH)
    private val isControlsVisibleState = mutableStateOf(false)
    private val colorBoostFactorState = mutableFloatStateOf(2.0f)

    // --- ADB UI Control Receiver ---
    private val uiControlReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                "com.flashlight.torch.HIDE_UI" -> isControlsVisibleState.value = false
                "com.flashlight.torch.SHOW_UI" -> isControlsVisibleState.value = true
                "com.flashlight.torch.TOGGLE_UI" -> isControlsVisibleState.value = !isControlsVisibleState.value
            }
        }
    }

    // --- Burn-in Tracking ---
    private var baseColor = Color.Black
    private var lastReceivedRawRgb = Triple(-1, -1, -1)
    private var lastColorChangeTimestamp = System.currentTimeMillis()

    // --- Background Network Job ---
    private var serverJob: Job? = null

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        hasCameraPermissionState.value = isGranted
        if (isGranted) {
            initializeFlashlight()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Keep screen on & set max brightness
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val layoutParams = window.attributes
        layoutParams.screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_FULL
        window.attributes = layoutParams

        // Fullscreen immersive mode
        WindowCompat.setDecorFitsSystemWindows(window, false)
        val insetsController = WindowInsetsControllerCompat(window, window.decorView)
        insetsController.hide(WindowInsetsCompat.Type.systemBars())
        insetsController.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE

        // Check camera permission & initialize
        hasCameraPermissionState.value = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA
        ) == PackageManager.PERMISSION_GRANTED

        if (hasCameraPermissionState.value) {
            initializeFlashlight()
        } else {
            permissionLauncher.launch(Manifest.permission.CAMERA)
        }

        // Register ADB UI control receiver
        val filter = IntentFilter().apply {
            addAction("com.flashlight.torch.HIDE_UI")
            addAction("com.flashlight.torch.SHOW_UI")
            addAction("com.flashlight.torch.TOGGLE_UI")
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(uiControlReceiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            registerReceiver(uiControlReceiver, filter)
        }
        handleIntent(intent)

        // Start background socket listener & burn-in ticker
        startNetworkListener()
        startBurnInMonitor()

        setContent {
            WrapperApp(
                isConnected = isConnectedState.value,
                displayColor = displayColorState.value,
                rawRgb = rawRgbValuesState.value,
                vibration = vibrationLevelState.intValue,
                packetCount = packetCountState.longValue,
                hasCameraPermission = hasCameraPermissionState.value,
                selectedMode = selectedModeState.value,
                isControlsVisible = isControlsVisibleState.value,
                colorBoost = colorBoostFactorState.floatValue,
                onModeSelected = { selectedModeState.value = it },
                onBoostChanged = { newBoost ->
                    colorBoostFactorState.floatValue = newBoost
                    val raw = rawRgbValuesState.value
                    applyColorUpdate(raw.first, raw.second, raw.third)
                },
                onToggleControls = { isControlsVisibleState.value = !isControlsVisibleState.value },
                onRequestPermission = { permissionLauncher.launch(Manifest.permission.CAMERA) }
            )
        }
    }

    private fun initializeFlashlight() {
        cameraManager = getSystemService(Context.CAMERA_SERVICE) as? CameraManager
        val manager = cameraManager ?: return

        try {
            var fallbackId: String? = null
            for (id in manager.cameraIdList) {
                val characteristics = manager.getCameraCharacteristics(id)
                val hasFlash = characteristics.get(CameraCharacteristics.FLASH_INFO_AVAILABLE) ?: false
                if (hasFlash) {
                    val facing = characteristics.get(CameraCharacteristics.LENS_FACING)
                    if (facing == CameraCharacteristics.LENS_FACING_BACK) {
                        cameraId = id
                        break
                    } else if (fallbackId == null) {
                        fallbackId = id
                    }
                }
            }
            if (cameraId == null) cameraId = fallbackId
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    private fun updateTorchState(vibrationIntensity: Int) {
        val manager = cameraManager ?: return
        val id = cameraId ?: return

        // If mode disables torch, ensure torch is off
        if (selectedModeState.value == SyncMode.SCREEN_ONLY) {
            if (isTorchOn) {
                try {
                    manager.setTorchMode(id, false)
                    isTorchOn = false
                } catch (e: Exception) {}
            }
            return
        }

        try {
            if (vibrationIntensity > 0) {
                if (!isTorchOn) {
                    manager.setTorchMode(id, true)
                    isTorchOn = true
                }
            } else {
                if (isTorchOn) {
                    manager.setTorchMode(id, false)
                    isTorchOn = false
                }
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    /**
     * Perceptual gamma lift using HSV value channel.
     * Pure black (v <= 0.005) remains strictly 0 (true OLED black preserved).
     * Mid and darker desktop tones are lifted perceptually via gamma curve v^(1/boost)
     * so that ambient glow is actually visible and vibrant.
     */
    private fun calculateBoostedColor(r: Int, g: Int, b: Int, boost: Float): Color {
        if (boost <= 1.0f) {
            return Color(r / 255f, g / 255f, b / 255f)
        }

        val hsv = FloatArray(3)
        android.graphics.Color.RGBToHSV(r, g, b, hsv)

        val v = hsv[2] // Value [0..1]
        if (v > 0.005f) {
            val gamma = 1.0f / boost
            val boostedV = Math.pow(v.toDouble(), gamma.toDouble()).toFloat().coerceIn(0f, 1f)
            hsv[2] = boostedV
        }

        val colorInt = android.graphics.Color.HSVToColor(hsv)
        return Color(colorInt)
    }

    private fun applyColorUpdate(r: Int, g: Int, b: Int) {
        val boostedColor = calculateBoostedColor(r, g, b, colorBoostFactorState.floatValue)
        baseColor = boostedColor
        if (selectedModeState.value != SyncMode.FLASH_ONLY) {
            displayColorState.value = boostedColor
        }
    }

    private fun onDataReceived(r: Int, g: Int, b: Int, vibe: Int) {
        val now = System.currentTimeMillis()
        val currentRaw = Triple(r, g, b)

        rawRgbValuesState.value = currentRaw
        vibrationLevelState.intValue = vibe
        packetCountState.longValue += 1

        if (currentRaw != lastReceivedRawRgb) {
            lastReceivedRawRgb = currentRaw
            lastColorChangeTimestamp = now
            applyColorUpdate(r, g, b)
        }

        // Apply torch state
        updateTorchState(vibe)
    }

    private fun startBurnInMonitor() {
        lifecycleScope.launch(Dispatchers.Default) {
            while (isActive) {
                delay(500)
                if (!isConnectedState.value) continue
                if (selectedModeState.value == SyncMode.FLASH_ONLY) {
                    withContext(Dispatchers.Main) {
                        displayColorState.value = Color.Black
                    }
                    continue
                }

                val now = System.currentTimeMillis()
                val elapsed = now - lastColorChangeTimestamp

                if (elapsed > 60_000L) {
                    // Over 1 minute of static color: turn off OLED (pure black)
                    withContext(Dispatchers.Main) {
                        displayColorState.value = Color.Black
                    }
                } else if (elapsed > 10_000L) {
                    // Static for > 10s: micro-jitter to prevent burn-in (±1.5% subtle RGB drift)
                    val phase = ((now / 1500.0) % (2 * Math.PI))
                    val drift = (sin(phase) * 0.015f).toFloat()

                    val jitteredColor = Color(
                        red = (baseColor.red + drift).coerceIn(0f, 1f),
                        green = (baseColor.green + drift).coerceIn(0f, 1f),
                        blue = (baseColor.blue - drift).coerceIn(0f, 1f)
                    )
                    withContext(Dispatchers.Main) {
                        displayColorState.value = jitteredColor
                    }
                }
            }
        }
    }

    private fun startNetworkListener() {
        serverJob?.cancel()
        serverJob = lifecycleScope.launch(Dispatchers.IO) {
            var serverSocket: ServerSocket? = null
            try {
                serverSocket = ServerSocket(5555)
                serverSocket.reuseAddress = true

                while (isActive) {
                    try {
                        val clientSocket = serverSocket.accept()
                        clientSocket.tcpNoDelay = true
                        val inputStream = BufferedInputStream(clientSocket.getInputStream())

                        withContext(Dispatchers.Main) {
                            isConnectedState.value = true
                            isControlsVisibleState.value = false
                        }

                        val packet = ByteArray(4)
                        while (isActive) {
                            var readTotal = 0
                            while (readTotal < 4) {
                                val count = inputStream.read(packet, readTotal, 4 - readTotal)
                                if (count == -1) throw IOException("Socket disconnected")
                                readTotal += count
                            }

                            val r = packet[0].toInt() and 0xFF
                            val g = packet[1].toInt() and 0xFF
                            val b = packet[2].toInt() and 0xFF
                            val vibe = packet[3].toInt() and 0xFF

                            withContext(Dispatchers.Main) {
                                onDataReceived(r, g, b, vibe)
                            }
                        }
                    } catch (e: Exception) {
                        // Connection lost or closed
                        withContext(Dispatchers.Main) {
                            isConnectedState.value = false
                            updateTorchState(0)
                        }
                    }
                }
            } catch (e: Exception) {
                e.printStackTrace()
            } finally {
                try {
                    serverSocket?.close()
                } catch (e: Exception) {
                    // Ignore
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        if (intent != null && intent.hasExtra("hide_controls")) {
            isControlsVisibleState.value = !intent.getBooleanExtra("hide_controls", true)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        try {
            unregisterReceiver(uiControlReceiver)
        } catch (e: Exception) {}
        serverJob?.cancel()
        updateTorchState(0)
    }
}

@Composable
fun WrapperApp(
    isConnected: Boolean,
    displayColor: Color,
    rawRgb: Triple<Int, Int, Int>,
    vibration: Int,
    packetCount: Long,
    hasCameraPermission: Boolean,
    selectedMode: SyncMode,
    isControlsVisible: Boolean,
    colorBoost: Float,
    onModeSelected: (SyncMode) -> Unit,
    onBoostChanged: (Float) -> Unit,
    onToggleControls: () -> Unit,
    onRequestPermission: () -> Unit
) {
    val animatedColor by animateColorAsState(
        targetValue = if (selectedMode == SyncMode.FLASH_ONLY) Color.Black else displayColor,
        animationSpec = tween(durationMillis = 100),
        label = "ambientColor"
    )

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(animatedColor)
            .clickable(
                interactionSource = remember { MutableInteractionSource() },
                indication = null
            ) {
                onToggleControls()
            }
    ) {
        if (isControlsVisible) {
            // Server Check & Settings Overlay Card
            Card(
                shape = RoundedCornerShape(20.dp),
                colors = CardDefaults.cardColors(containerColor = Color(0xEE161620)),
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(24.dp)
                    .align(Alignment.Center)
            ) {
                Column(
                    modifier = Modifier
                        .padding(20.dp)
                        .fillMaxWidth(),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(14.dp)
                ) {
                    // Status Badge
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Box(
                            modifier = Modifier
                                .size(12.dp)
                                .background(
                                    color = if (isConnected) Color(0xFF4CAF50) else Color(0xFFFFB300),
                                    shape = CircleShape
                                )
                        )
                        Text(
                            text = if (isConnected) "SERVER CONNECTED" else "WAITING FOR CONNECTION",
                            color = Color.White,
                            fontWeight = FontWeight.Bold,
                            fontSize = 14.sp,
                            letterSpacing = 1.sp
                        )
                    }

                    Text(
                        text = "Port 5555 (ADB USB Link)",
                        fontFamily = FontFamily.Monospace,
                        color = Color.White.copy(alpha = 0.5f),
                        fontSize = 12.sp
                    )

                    HorizontalDivider(color = Color.White.copy(alpha = 0.1f))

                    // Live Received Values Swatch
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(Color(0xFF22222E), RoundedCornerShape(12.dp))
                            .padding(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Column {
                            Text(
                                text = "Raw Stream -> Boosted Color",
                                color = Color.White.copy(alpha = 0.6f),
                                fontSize = 12.sp
                            )
                            Text(
                                text = "Raw: R:${rawRgb.first} G:${rawRgb.second} B:${rawRgb.third}",
                                fontFamily = FontFamily.Monospace,
                                color = Color.White,
                                fontWeight = FontWeight.Bold,
                                fontSize = 14.sp
                            )
                            Text(
                                text = "Packets: $packetCount | Vibe: $vibration",
                                color = Color.White.copy(alpha = 0.4f),
                                fontSize = 11.sp
                            )
                        }

                        // Color swatch preview (shows boosted color)
                        Box(
                            modifier = Modifier
                                .size(46.dp)
                                .background(
                                    color = displayColor,
                                    shape = RoundedCornerShape(10.dp)
                                )
                                .border(1.5.dp, Color.White.copy(alpha = 0.4f), RoundedCornerShape(10.dp))
                        )
                    }

                    // Brightness Boost Slider Section
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(Color(0xFF22222E), RoundedCornerShape(12.dp))
                            .padding(12.dp)
                    ) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(
                                text = "Brightness Boost Curve",
                                color = Color.White,
                                fontWeight = FontWeight.SemiBold,
                                fontSize = 13.sp
                            )
                            Text(
                                text = String.format("%.1fx", colorBoost),
                                color = Color(0xFFFFB300),
                                fontWeight = FontWeight.Bold,
                                fontFamily = FontFamily.Monospace,
                                fontSize = 14.sp
                            )
                        }

                        Slider(
                            value = colorBoost,
                            onValueChange = onBoostChanged,
                            valueRange = 1.0f..3.5f,
                            colors = SliderDefaults.colors(
                                thumbColor = Color(0xFFFFB300),
                                activeTrackColor = Color(0xFFFFB300),
                                inactiveTrackColor = Color.White.copy(alpha = 0.2f)
                            ),
                            modifier = Modifier.padding(vertical = 2.dp)
                        )

                        Text(
                            text = "Preserves deep blacks while boosting lighter shades into vivid brightness",
                            color = Color.White.copy(alpha = 0.45f),
                            fontSize = 11.sp,
                            lineHeight = 14.sp
                        )
                    }

                    // Mode Toggles Selector
                    Text(
                        text = "Sync Mode",
                        color = Color.White,
                        fontWeight = FontWeight.SemiBold,
                        fontSize = 13.sp,
                        modifier = Modifier.align(Alignment.Start)
                    )

                    Column(
                        modifier = Modifier.fillMaxWidth(),
                        verticalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        SyncMode.values().forEach { mode ->
                            val isSelected = mode == selectedMode
                            Surface(
                                shape = RoundedCornerShape(10.dp),
                                color = if (isSelected) Color(0xFFFFB300).copy(alpha = 0.18f) else Color(0xFF22222E),
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .border(
                                        width = if (isSelected) 1.5.dp else 0.dp,
                                        color = if (isSelected) Color(0xFFFFB300) else Color.Transparent,
                                        shape = RoundedCornerShape(10.dp)
                                    )
                                    .clickable { onModeSelected(mode) }
                            ) {
                                Row(
                                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    RadioButton(
                                        selected = isSelected,
                                        onClick = { onModeSelected(mode) },
                                        colors = RadioButtonDefaults.colors(selectedColor = Color(0xFFFFB300))
                                    )
                                    Spacer(modifier = Modifier.width(6.dp))
                                    Text(
                                        text = mode.label,
                                        color = if (isSelected) Color(0xFFFFB300) else Color.White,
                                        fontSize = 13.sp,
                                        fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal
                                    )
                                }
                            }
                        }
                    }

                    if (!hasCameraPermission) {
                        Button(
                            onClick = onRequestPermission,
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFFFB300)),
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Text("Grant Camera Permission (for Torch)", color = Color.Black)
                        }
                    }

                    Button(
                        onClick = onToggleControls,
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF333344)),
                        shape = RoundedCornerShape(10.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("Hide Controls & Enter Fullscreen", color = Color.White)
                    }

                    Text(
                        text = "Tap anywhere on the screen anytime to show/hide controls",
                        color = Color.White.copy(alpha = 0.4f),
                        fontSize = 11.sp,
                        textAlign = TextAlign.Center
                    )
                }
            }
        }
    }
}
