package com.omnisave.studio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Environment
import android.os.IBinder
import android.system.Os
import android.webkit.MimeTypeMap
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.FileProvider
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.util.concurrent.atomic.AtomicInteger

class OmniSaveService : Service() {

    companion object {
        // Dedicated port so standalone APK never collides with Termux on port 8000!
        const val PORT = 18980
        const val CHANNEL_ID = "omnisave_engine_channel"
        const val DONE_CHANNEL_ID = "omnisave_done_channel"
        const val NOTIF_ID = 1001
        const val ACTION_STOP = "com.omnisave.studio.ACTION_STOP"

        private val doneNotifSeq = AtomicInteger(2000)

        @Volatile
        var isServerThreadRunning: Boolean = false

        @Volatile
        private var activeService: OmniSaveService? = null

        @Volatile
        private var lastNotifUpdateMs: Long = 0L

        fun updateNotification(title: String, stage: String, percent: Int, active: Boolean) {
            val svc = activeService ?: return
            val now = System.currentTimeMillis()
            if (active && percent in 1..98 && (now - lastNotifUpdateMs) < 450L) {
                return
            }
            lastNotifUpdateMs = now
            val nm = svc.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
            nm.notify(NOTIF_ID, svc.buildNotification(title, stage, percent, active))
        }

        fun showDownloadCompleteNotification(title: String, filepath: String) {
            val svc = activeService ?: return
            val nm = svc.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
            val file = File(filepath)
            val label = if (file.name.isNotBlank()) file.name else title

            val nid = doneNotifSeq.incrementAndGet()
            var tapPending: PendingIntent? = null
            try {
                if (file.exists()) {
                    val uri = FileProvider.getUriForFile(svc, "${svc.packageName}.fileprovider", file)
                    val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(file.extension.lowercase()) ?: "*/*"
                    val viewIntent = Intent(Intent.ACTION_VIEW).apply {
                        setDataAndType(uri, mime)
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                    }
                    tapPending = PendingIntent.getActivity(
                        svc,
                        nid,
                        viewIntent,
                        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                    )
                }
            } catch (_: Exception) {
            }

            val notif = NotificationCompat.Builder(svc, DONE_CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_omnisave)
                .setContentTitle("Download Complete ✓")
                .setContentText(label)
                .setAutoCancel(true)
                .apply { if (tapPending != null) setContentIntent(tapPending) }
                .build()

            nm.notify(nid, notif)
        }
    }

    override fun onCreate() {
        super.onCreate()
        activeService = this
        AndroidBridge.init(applicationContext)
        createNotificationChannels()

        val fgType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        } else {
            0
        }
        ServiceCompat.startForeground(
            this,
            NOTIF_ID,
            buildNotification("OmniSave Studio", "Ready for downloads", 0, false),
            fgType
        )
        startPythonEngineIfNeeded()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopPythonEngine()
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        startPythonEngineIfNeeded()
        return START_STICKY
    }

    override fun onDestroy() {
        if (activeService === this) {
            activeService = null
        }
        stopPythonEngine()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startPythonEngineIfNeeded() {
        if (isServerThreadRunning) return
        synchronized(OmniSaveService::class.java) {
            if (isServerThreadRunning) return
            isServerThreadRunning = true
        }

        Thread {
            try {
                val dlDir = Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS
                ).absolutePath

                Os.setenv("HOME", filesDir.absolutePath, true)
                Os.setenv("TMPDIR", cacheDir.absolutePath, true)
                Os.setenv("OMNISAVE_ANDROID_APP", "1", true)
                Os.setenv("OMNISAVE_DATA_DIR", filesDir.absolutePath, true)
                Os.setenv("OMNISAVE_DOWNLOAD_DIR", dlDir, true)
                Os.setenv("OMNISAVE_NATIVE_LIB_DIR", applicationInfo.nativeLibraryDir, true)

                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(applicationContext))
                }
                val py = Python.getInstance()
                val mod = py.getModule("omnisave_studio")
                mod.callAttr("start_android_server", PORT)
            } catch (e: Exception) {
                updateNotification("OmniSave Error", e.message ?: "Engine stopped", 0, false)
            } finally {
                isServerThreadRunning = false
            }
        }.apply {
            name = "OmniSavePythonEngine"
            isDaemon = true
            start()
        }
    }

    private fun stopPythonEngine() {
        Thread {
            try {
                if (Python.isStarted()) {
                    val py = Python.getInstance()
                    py.getModule("omnisave_studio").callAttr("stop_android_server")
                }
            } catch (_: Exception) {
            } finally {
                isServerThreadRunning = false
            }
        }.start()
    }

    private fun createNotificationChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val engChannel = NotificationChannel(
                CHANNEL_ID,
                "OmniSave Engine & Progress",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Live progress bar for background downloads"
                setShowBadge(false)
            }
            val doneChannel = NotificationChannel(
                DONE_CHANNEL_ID,
                "Completed Downloads",
                NotificationManager.IMPORTANCE_DEFAULT
            ).apply {
                description = "Alerts when a background download finishes"
            }
            nm.createNotificationChannel(engChannel)
            nm.createNotificationChannel(doneChannel)
        }
    }

    fun buildNotification(title: String, stage: String, percent: Int, active: Boolean): Notification {
        val openIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val openPending = PendingIntent.getActivity(
            this,
            0,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val stopIntent = Intent(this, OmniSaveService::class.java).apply {
            action = ACTION_STOP
        }
        val stopPending = PendingIntent.getService(
            this,
            1,
            stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val displayTitle = if (active && title.isNotBlank()) title else "OmniSave Studio"
        val displayStage = if (active) {
            if (percent > 0) "$stage ($percent%)" else stage
        } else {
            if (stage.isNotBlank()) stage else "Ready"
        }

        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_omnisave)
            .setContentTitle(displayTitle)
            .setContentText(displayStage)
            .setContentIntent(openPending)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .addAction(0, "Stop Engine", stopPending)

        if (active) {
            val clamped = percent.coerceIn(0, 100)
            builder.setProgress(100, clamped, clamped == 0)
        } else {
            builder.setProgress(0, 0, false)
        }

        return builder.build()
    }
}
