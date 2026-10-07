package com.lovedeep.aivideoeditor

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Firely, the app's own thinking AI (a small language model with eyes, run by llama.cpp in
 * libbrain.so), called from the Python engine (engine/brain.py). The model ships inside the APK
 * and is unpacked once to app storage on first use. It is let go of after a couple of idle
 * minutes so video rendering has the phone's memory to itself.
 */
object Brain {
    private const val ASSET_DIR = "brain"
    private const val MODEL = "model.gguf"
    private const val VISION = "vision.gguf"
    private const val CONTEXT = 4096
    private const val IDLE_SECONDS = 120L

    private var handle = 0L
    private var hasVision = false
    private val idle = Executors.newSingleThreadScheduledExecutor()
    private var release: ScheduledFuture<*>? = null

    init {
        // Phones from about 2018 on have fast "dot product" maths; the engine built for it is about
        // twice as fast. Older phones get the version that works everywhere.
        val features = try { java.io.File("/proc/cpuinfo").readText() } catch (e: Exception) { "" }
        val fast = Regex("""\basimddp\b""").containsMatchIn(features) && Regex("""\basimdhp\b""").containsMatchIn(features)
        try {
            System.loadLibrary(if (fast) "brain_dotprod" else "brain")
        } catch (e: UnsatisfiedLinkError) {
            System.loadLibrary("brain")
        }
    }

    @JvmStatic private external fun nativeLoad(path: String, threads: Int, ctx: Int): Long
    @JvmStatic private external fun nativeLoadVision(handle: Long, path: String, threads: Int): Boolean
    @JvmStatic private external fun nativeGenerate(
        handle: Long, roles: Array<String>, contents: Array<String>, images: Array<String>,
        grammar: String, maxTokens: Int, temperature: Float, seed: Int,
    ): String?
    @JvmStatic private external fun nativeError(): String
    @JvmStatic private external fun nativeFree(handle: Long)

    private fun bundled(name: String): Boolean =
        try { Native.app.assets.openFd("$ASSET_DIR/$name").use { true } } catch (e: Exception) { false }

    /** True when the model is inside this APK. */
    @JvmStatic
    fun available(): Boolean = bundled(MODEL)

    private fun unpack(name: String): File {
        val app = Native.app
        val dir = File(app.filesDir, ASSET_DIR).apply { mkdirs() }
        val out = File(dir, name)
        val size = app.assets.openFd("$ASSET_DIR/$name").use { it.length }
        if (out.length() != size) {
            if (dir.usableSpace < size + (200L shl 20)) {
                throw IllegalStateException("not enough free space on the phone for the AI (needs ${size shr 20} MB)")
            }
            val tmp = File(dir, "$name.part")
            app.assets.open("$ASSET_DIR/$name").use { input -> tmp.outputStream().use { input.copyTo(it, 1 shl 20) } }
            out.delete()
            tmp.renameTo(out)
        }
        return out
    }

    private fun threads() = Runtime.getRuntime().availableProcessors().coerceIn(2, 4)

    private fun load(): Long {
        if (handle != 0L) return handle
        val h = nativeLoad(unpack(MODEL).path, threads(), CONTEXT)
        if (h == 0L) throw IllegalStateException(nativeError())
        handle = h
        hasVision = false
        return h
    }

    private fun loadVision() {
        if (hasVision || !bundled(VISION)) return
        if (!nativeLoadVision(handle, unpack(VISION).path, threads())) throw IllegalStateException(nativeError())
        hasVision = true
    }

    /**
     * Runs the model on [messagesJson] ([{"role","content"}]) and returns {"text"} or {"error"} as JSON.
     * [grammar] (GBNF, may be empty) fixes the answer's shape; [imagesJson] lists picture files.
     */
    @JvmStatic
    @Synchronized
    fun chat(messagesJson: String, grammar: String, maxTokens: Int, temperature: Float, seed: Int,
             imagesJson: String): String {
        release?.cancel(false)
        return try {
            val msgs = JSONArray(messagesJson)
            val roles = Array(msgs.length()) { msgs.getJSONObject(it).getString("role") }
            val contents = Array(msgs.length()) { msgs.getJSONObject(it).getString("content") }
            val imgs = JSONArray(imagesJson)
            val images = Array(imgs.length()) { imgs.getString(it) }
            val h = load()
            if (images.isNotEmpty()) loadVision()
            val text = nativeGenerate(h, roles, contents, images, grammar, maxTokens, temperature, seed)
                ?: return JSONObject().put("error", nativeError()).toString()
            JSONObject().put("text", text).toString()
        } catch (e: Throwable) {
            JSONObject().put("error", e.message ?: e.javaClass.simpleName).toString()
        } finally {
            release = idle.schedule({ unload() }, IDLE_SECONDS, TimeUnit.SECONDS)
        }
    }

    /** Frees the model's memory (it loads again by itself when next needed). */
    @JvmStatic
    @Synchronized
    fun unload() {
        if (handle != 0L) {
            nativeFree(handle)
            handle = 0L
            hasVision = false
        }
    }
}
