package com.lovedeep.aivideoeditor

import android.content.ContentValues
import android.os.Build
import android.provider.MediaStore
import android.util.Log
import java.io.File

/**
 * Automatic check used by the build robot on an Android emulator (`am start ... --ez selftest true`):
 * makes sample videos, saves one into the gallery and reads it back through Android's content system
 * (the same way picked files are read), then runs real edits and chat changes. Results go to the log.
 */
object SelfTest {
    fun run(activity: MainActivity) {
        Thread({
            try {
                val dir = File(activity.cacheDir, "selftest").apply { deleteRecursively(); mkdirs() }
                Engine.call("selftest_make", dir.path)
                var safPath = ""
                if (Build.VERSION.SDK_INT >= 29) {
                    val values = ContentValues().apply {
                        put(MediaStore.MediaColumns.DISPLAY_NAME, "selftest_talk.mp4")
                        put(MediaStore.MediaColumns.MIME_TYPE, "video/mp4")
                        put(MediaStore.MediaColumns.RELATIVE_PATH, "Movies/AIEditorSelfTest")
                    }
                    val uri = activity.contentResolver.insert(
                        MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY), values)
                    if (uri != null) {
                        activity.contentResolver.openOutputStream(uri)!!.use { out ->
                            File(dir, "talk.mp4").inputStream().use { it.copyTo(out) }
                        }
                        safPath = Sources.register(uri).getString("path")
                    }
                }
                Engine.call("selftest_run", dir.path, safPath, Engine.port, Engine.token)
            } catch (e: Throwable) {
                Log.e("SELFTEST", "crashed", e)
                println("SELFTEST: FAIL crashed ${e.message}")
            }
        }, "selftest").start()
    }
}
