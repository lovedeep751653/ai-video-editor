package com.lovedeep.aivideoeditor

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.antonkarpenko.ffmpegkit.FFmpegKitConfig
import com.antonkarpenko.ffmpegkit.Level
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File
import java.security.SecureRandom

/**
 * The editor engine: Python (the same code as on a computer) running a tiny web server on
 * 127.0.0.1 inside this app, with FFmpeg built in. The screen is a WebView showing that server.
 */
object Engine {
    @Volatile var port = 0
        private set
    @Volatile var error: String? = null
        private set
    @Volatile var firstStart = false
        private set
    val token: String = ByteArray(24).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }

    private var module: PyObject? = null
    private val waiting = mutableListOf<() -> Unit>()
    private val main = Handler(Looper.getMainLooper())
    private var started = false

    val ready: Boolean get() = port > 0

    fun startAsync(ctx: Context) {
        synchronized(this) {
            if (started) return
            started = true
        }
        Thread({
            try {
                start(ctx.applicationContext)
            } catch (e: Throwable) {
                Log.e("Engine", "start failed", e)
                error = e.message ?: e.toString()
            }
            val cbs = synchronized(waiting) { waiting.toList().also { waiting.clear() } }
            main.post { cbs.forEach { it() } }
        }, "engine-start").start()
    }

    /** Runs `cb` on the main thread once the engine has started (or failed to). */
    fun whenReady(cb: () -> Unit) {
        synchronized(waiting) {
            if (port == 0 && error == null) {
                waiting.add(cb)
                return
            }
        }
        main.post(cb)
    }

    private fun start(ctx: Context) {
        val res = File(ctx.filesDir, "editor_res")
        val stamp = File(res, ".stamp")
        val info = ctx.packageManager.getPackageInfo(ctx.packageName, 0)
        val want = "${info.lastUpdateTime}"
        if (!stamp.exists() || stamp.readText() != want) {
            firstStart = true
            res.deleteRecursively()
            copyAssets(ctx, "editor", res)
            stamp.writeText(want)
        }
        val fonts = File(res, "fonts")
        FFmpegKitConfig.setLogLevel(Level.AV_LOG_ERROR)
        FFmpegKitConfig.setFontDirectoryList(ctx, listOf(fonts.path, "/system/fonts"), emptyMap())
        if (!Python.isStarted()) Python.start(AndroidPlatform(ctx))
        val data = File(ctx.filesDir, "data").apply { mkdirs() }
        val mod = Python.getInstance().getModule("android_main")
        module = mod
        port = mod.callAttr("start", data.path, File(res, "static").path, fonts.path, token,
            info.versionName ?: "", ctx.cacheDir.path).toInt()
        Log.i("Engine", "editor running on 127.0.0.1:$port")
    }

    private fun copyAssets(ctx: Context, path: String, dest: File) {
        val list = ctx.assets.list(path) ?: return
        if (list.isEmpty()) {
            dest.parentFile?.mkdirs()
            ctx.assets.open(path).use { input -> dest.outputStream().use { input.copyTo(it) } }
            return
        }
        dest.mkdirs()
        for (name in list) copyAssets(ctx, "$path/$name", File(dest, name))
    }

    /** What the engine is doing right now (for the notification). */
    fun busy(): JSONObject? = try {
        module?.callAttr("busy")?.toString()?.let { JSONObject(it) }
    } catch (e: Throwable) {
        null
    }

    fun call(name: String, vararg args: Any?): String? = try {
        module?.callAttr(name, *args)?.toString()
    } catch (e: Throwable) {
        Log.e("Engine", "$name failed", e)
        null
    }

    val baseUrl: String get() = "http://127.0.0.1:$port"
}
