# Flashlight App ProGuard Rules

# Keep view binding classes
-keep class com.flashlight.torch.databinding.** { *; }

# Keep R8 from removing annotations
-keepattributes *Annotation*

# Preserve the line number information for debugging stack traces
-keepattributes SourceFile,LineNumberTable

# Keep Material components working
-keep class com.google.android.material.** { *; }
-dontwarn com.google.android.material.**

# AndroidX
-keep class androidx.** { *; }
-dontwarn androidx.**

# Kotlin
-dontwarn kotlin.**
-keep class kotlin.Metadata { *; }
