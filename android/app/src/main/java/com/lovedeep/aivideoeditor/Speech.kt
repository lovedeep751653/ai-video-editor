package com.lovedeep.aivideoeditor

import com.k2fsa.sherpa.onnx.OfflineModelConfig
import com.k2fsa.sherpa.onnx.OfflineOmnilingualAsrCtcModelConfig
import com.k2fsa.sherpa.onnx.OfflineRecognizer
import com.k2fsa.sherpa.onnx.OfflineRecognizerConfig
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Speech to text on the phone (Meta Omnilingual ASR through sherpa-onnx), called from
 * the Python engine (engine/speech.py) to make captions. The model ships inside the APK
 * and is unpacked once to app storage on first use.
 */
object Speech {
    private const val ASSET_DIR = "speech"
    private val FILES = listOf("model.int8.onnx", "tokens.txt")
    private var recognizer: OfflineRecognizer? = null

    private fun modelDir(): File {
        val app = Native.app
        val dir = File(app.filesDir, ASSET_DIR)
        dir.mkdirs()
        for (name in FILES) {
            val out = File(dir, name)
            val size = app.assets.openFd("$ASSET_DIR/$name").use { it.length }
            if (out.length() != size) {
                val tmp = File(dir, "$name.part")
                app.assets.open("$ASSET_DIR/$name").use { input -> tmp.outputStream().use { input.copyTo(it, 1 shl 20) } }
                tmp.renameTo(out)
            }
        }
        return dir
    }

    private fun get(): OfflineRecognizer {
        recognizer?.let { return it }
        val dir = modelDir()
        val threads = Runtime.getRuntime().availableProcessors().coerceIn(1, 4)
        val config = OfflineRecognizerConfig(
            modelConfig = OfflineModelConfig(
                omnilingual = OfflineOmnilingualAsrCtcModelConfig(model = File(dir, "model.int8.onnx").path),
                tokens = File(dir, "tokens.txt").path,
                numThreads = threads,
            ),
        )
        return OfflineRecognizer(null, config).also { recognizer = it }
    }

    /** Copies the bundled English test recording out for the self-test; returns its path. */
    @JvmStatic
    fun selftestWav(): String {
        val out = File(Native.app.cacheDir, "selftest_en.wav")
        Native.app.assets.open("$ASSET_DIR/selftest_en.wav").use { input -> out.outputStream().use { input.copyTo(it) } }
        return out.path
    }

    /** Recognises 16 kHz mono float samples stored in [path]; returns {"text","tokens","timestamps"} as JSON. */
    @JvmStatic
    @Synchronized
    fun recognize(path: String): String {
        return try {
            val bytes = File(path).readBytes()
            val samples = FloatArray(bytes.size / 4)
            ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer().get(samples)
            val rec = get()
            val stream = rec.createStream()
            try {
                stream.acceptWaveform(samples, 16000)
                rec.decode(stream)
                val r = rec.getResult(stream)
                JSONObject()
                    .put("text", r.text)
                    .put("tokens", JSONArray(r.tokens.toList()))
                    .put("timestamps", JSONArray(r.timestamps.map { it.toDouble() }))
                    .toString()
            } finally {
                stream.release()
            }
        } catch (e: Throwable) {
            JSONObject().put("error", "Speech recognition failed: ${e.message ?: e.javaClass.simpleName}").toString()
        }
    }
}
