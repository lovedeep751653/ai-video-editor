package com.lovedeep.aivideoeditor

import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.net.Uri
import android.provider.OpenableColumns
import org.json.JSONObject
import java.util.UUID

/**
 * Files the user picked. Each gets a short token; the engine sees it as the path "/saf/<token>.<ext>".
 * The app keeps permission to read them, so edits can continue after the phone restarts the app.
 */
object Sources {
    private lateinit var prefs: SharedPreferences
    private lateinit var app: Context

    fun init(ctx: Context) {
        app = ctx.applicationContext
        prefs = ctx.getSharedPreferences("sources", Context.MODE_PRIVATE)
    }

    fun uri(token: String): Uri? = prefs.getString("t_$token", null)?.let { Uri.parse(it) }

    fun register(uri: Uri): JSONObject {
        try {
            app.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        } catch (e: Exception) {
            // Not every picker allows keeping access; the file still works while the app is open.
        }
        val key = "u_$uri"
        val token = prefs.getString(key, null) ?: UUID.randomUUID().toString().replace("-", "").take(16).also {
            prefs.edit().putString(key, it).putString("t_$it", uri.toString()).apply()
        }
        var name = "file"
        var size = -1L
        try {
            app.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
                null, null, null)?.use { c ->
                if (c.moveToFirst()) {
                    c.getString(0)?.let { name = it }
                    if (!c.isNull(1)) size = c.getLong(1)
                }
            }
        } catch (e: Exception) {
        }
        val mime = app.contentResolver.getType(uri) ?: ""
        var ext = name.substringAfterLast('.', "").lowercase().filter { it.isLetterOrDigit() }.take(5)
        if (ext.isEmpty()) ext = when {
            mime.startsWith("video/") -> "mp4"
            mime == "image/png" -> "png"
            mime.startsWith("image/") -> "jpg"
            mime.startsWith("audio/") -> "m4a"
            else -> "bin"
        }
        return JSONObject().put("path", "/saf/$token.$ext").put("name", name).put("size", size).put("mime", mime)
    }
}
