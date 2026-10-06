package com.lovedeep.aivideoeditor

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat

/**
 * Keeps the app alive while a video is being made, even with the screen off or another app open,
 * and shows the real progress in a notification. Stops by itself when the work is done.
 */
class WorkService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var wake: PowerManager.WakeLock? = null
    private var idleTicks = 0

    private val tick = object : Runnable {
        override fun run() {
            val b = Engine.busy()
            if (b == null || !b.optBoolean("working")) {
                if (++idleTicks >= 2) {
                    stopSelf()
                    return
                }
            } else {
                idleTicks = 0
                update(b.optString("stage", "Working"), b.optDouble("progress", 0.0),
                    if (b.isNull("eta")) -1 else b.optInt("eta", -1))
            }
            handler.postDelayed(this, 1500)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, getString(R.string.channel_name),
                NotificationManager.IMPORTANCE_LOW))
        }
        val type = when {
            Build.VERSION.SDK_INT >= 35 -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROCESSING
            Build.VERSION.SDK_INT >= 29 -> ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            else -> 0
        }
        try {
            ServiceCompat.startForeground(this, ID, build("Starting", 0.0, -1), type)
        } catch (e: Exception) {
            stopSelf()
            return
        }
        wake = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "aieditor:work").apply { acquire(6 * 60 * 60 * 1000L) }
        handler.post(tick)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_NOT_STICKY

    override fun onDestroy() {
        handler.removeCallbacks(tick)
        wake?.let { if (it.isHeld) it.release() }
        running = false
        super.onDestroy()
    }

    private fun build(stage: String, progress: Double, eta: Int): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)
        val pct = (progress * 100).toInt().coerceIn(0, 100)
        val left = when {
            eta < 0 -> ""
            eta < 60 -> " · less than a minute left"
            else -> " · about ${(eta + 30) / 60} min left"
        }
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(getString(R.string.notif_working))
            .setContentText("$stage · $pct%$left")
            .setProgress(100, pct, false)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .build()
    }

    private fun update(stage: String, progress: Double, eta: Int) {
        getSystemService(NotificationManager::class.java).notify(ID, build(stage, progress, eta))
    }

    companion object {
        private const val CHANNEL = "work"
        private const val ID = 7
        @Volatile var running = false

        fun ensure(ctx: Context) {
            if (running) return
            running = true
            try {
                androidx.core.content.ContextCompat.startForegroundService(ctx, Intent(ctx, WorkService::class.java))
            } catch (e: Exception) {
                running = false
            }
        }
    }
}
