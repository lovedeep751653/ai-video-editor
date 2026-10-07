plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// The editor engine (app/engine, app/server.py), the phone screen (app/static) and the
// caption fonts (app/fonts) are copied in by the `prepare` script before each build.
val abis = (findProperty("abis") as String? ?: "arm64-v8a,x86_64").split(",")

android {
    namespace = "com.lovedeep.aivideoeditor"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.lovedeep.aivideoeditor"
        minSdk = 24          // Android 7 and newer
        targetSdk = 35
        versionCode = (findProperty("versionCode") as String? ?: "2").toInt()
        versionName = findProperty("versionName") as String? ?: "2.0"
        ndk { abiFilters += abis }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Signed with the debug key so the APK installs straight from a download.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    packaging {
        // Compress the video and Python libraries inside the APK (smaller download).
        jniLibs { useLegacyPackaging = true }
    }
    // The speech and Firely models are stored uncompressed so they can be copied out quickly (and openFd gives their size).
    androidResources { noCompress += listOf("ttf", "otf", "onnx", "txt", "gguf") }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        pip { install("numpy") }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.core:core-ktx:1.15.0")
    // FFmpeg 8 built for Android, with captions (libass), x264 and the phone's hardware encoder.
    implementation("com.antonkarpenko:ffmpeg-kit-full-gpl:2.2.3")
    // On-phone speech recognition for captions (downloaded by .github/scripts/prepare-android.sh).
    implementation(files("libs/sherpa-onnx.aar"))
}
