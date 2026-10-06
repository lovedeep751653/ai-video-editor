package com.lovedeep.aivideoeditor

import android.content.Context
import com.antonkarpenko.ffmpegkit.FFmpegKitConfig

/** Called from the Python engine (engine/ff.py) to read files the user picked. */
object Native {
    lateinit var app: Context

    /**
     * A fresh FFmpeg "saf:" address for a picked file. FFmpeg opens the file through Android's
     * content system with it, so videos are read in place (never copied).
     */
    @JvmStatic
    fun safFor(token: String): String? {
        val uri = Sources.uri(token) ?: return null
        return try {
            FFmpegKitConfig.getSafParameterForRead(app, uri)
        } catch (e: Exception) {
            null
        }
    }

    /** Whether a picked file can still be opened. */
    @JvmStatic
    fun available(token: String): Boolean {
        val uri = Sources.uri(token) ?: return false
        return try {
            app.contentResolver.openFileDescriptor(uri, "r")?.close()
            true
        } catch (e: Exception) {
            false
        }
    }
}
