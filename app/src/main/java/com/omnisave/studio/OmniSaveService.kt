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
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

class OmniSaveService : Service() {

    companion object {
        const val PORT = 18980
        const val ENGINE_CHANNEL_ID = "omnisave_engine_channel"
        // IMPORTANCE_DEFAULT + no sound/vibration keeps live progress bars visible at TOP of One UI / Android 14!
        const val PROGRESS_CHANNEL_ID = "omnisave_dl_progress_v2"
        const val ALERT_CHANNEL_ID = "omnisave_done_channel"
        const val ENGINE_NOTIF_ID = 1001
        const val ACTION_STOP = "com.omnisave.studio.ACTION_STOP"
        const val ACTION_CANCEL_JOB = "com.omnisave.studio.ACTION_CANCEL_JOB"
        const val EXTRA_JOB_ID = "job_id"

        private val alertNotifSeq = AtomicInteger(20000)
        private val lastJobTickMs = ConcurrentHashMap<String, Long>()

        @Volatile
        var isServerThreadRunning: Boolean = false

        @Volatile
        private var activeService: OmniSaveService? = null

        private fun progressNotifId(jobId: String): Int {
            return 3000 + ((jobId.hashCode() and 0x7fffffff) % 10000)
        }

        fun updateEngineSummary(activeCount: Int) {
            val svc = activeService ?: return
            val nm = svc.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
            val stage = if (activeCount > 0) {
                if (activeCount == 1) "1 active download running" else "$activeCount active downloads running"
            } else {
                "Ready for downloads"
            }
            nm.notify(ENGINE_NOTIF_ID, svc.buildEngineNotification(stage))
        }

        fun showJobProgress(jobId: String, title: String, stage: String, percent: Int, subtext: String) {
            val svc = activeService ?: return
            val now = System.currentTimeMillis()
            val prev = lastJobTickMs[jobId] ?: 0L
            if (percent in 1..98 && (now - prev) < 400L) {
                return
            }
            lastJobTickMs[jobId] = now

            val nm = svc.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
            val nid = progressNotifId(jobId)

            val openIntent = Intent(svc, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            }
            val openPending = PendingIntent.getActivity(
                svc,
                nid,
                openIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            val cancelIntent = Intent(svc, OmniSaveService::class.java).apply {
                action = ACTION_CANCEL_JOB
                putExtra(EXTRA_JOB_ID, jobId)
            }
            val cancelPending = PendingIntent.getService(
                svc,
                nid + 50000,
                cancelIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            val clamped = percent.coerceIn(0, 100)
            val pctPrefix = if (clamped > 0) "$clamped% · " else ""
            val bodyLine = if (subtext.isNotBlank()) "$pctPrefix$subtext" else "$pctPrefix$stage"

            val notif = NotificationCompat.Builder(svc, PROGRESS_CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_omnisave)
                .setContentTitle(if (title.isNotBlank()) title else "Downloading...")
                .setContentText(bodyLine)
                .setContentIntent(openPending)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setSilent(true)
                .setCategory(NotificationCompat.CATEGORY_PROGRESS)
                .setProgress(100, clamped, clamped == 0)
                .addAction(0, "Cancel", cancelPending)
                .build()

            nm.notify(nid, notif)
        }

        fun cancelJobNotification(jobId: String) {
            val svc = activeService ?: return
            lastJobTickMs.remove(jobId)
            val nm = svc.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
            nm.cancel(progressNotifId(jobId))
        }

        fun showJobComplete(jobId: String, title: String, filepath: String, sizeText: String) {
            val svc = activeService ?: return
            lastJobTickMs.remove(jobId)
            val nm = svc.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
            nm.cancel(progressNotifId(jobId))

            val file = File(filepath)
            val baseName = if (file.name.isNotBlank()) file.name else title
            val label = if (sizeText.isNotBlank() && sizeText != "0 MB") "$baseName ($sizeText)" else baseName

            val nid = alertNotifSeq.incrementAndGet()
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

            if (tapPending == null) {
                val appIntent = Intent(svc, MainActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
                }
                tapPending = PendingIntent.getActivity(
                    svc,
                    nid,
                    appIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )
            }

            val notif = NotificationCompat.Builder(svc, ALERT_CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_omnisave)
                .setContentTitle("Download Complete ✓")
                .setContentText(label)
                .setStyle(NotificationCompat.BigTextStyle().bigText(label))
                .setAutoCancel(true)
                .setContentIntent(tapPending)
                .build()

            nm.notify(nid, notif)
        }

        fun showJobError(jobId: String, title: String, errorMsg: String) {
            val svc = activeService ?: return
            lastJobTickMs.remove(jobId)
            val nm = svc.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
            nm.cancel(progressNotifId(jobId))

            val nid = alertNotifSeq.incrementAndGet()
            val openIntent = Intent(svc, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            }
            val openPending = PendingIntent.getActivity(
                svc,
                nid,
                openIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            val shortErr = errorMsg.trim().ifBlank { "Download failed" }
            val headTitle = if (title.isNotBlank()) "Failed: ${title.take(36)}" else "Download Failed ✕"

            val notif = NotificationCompat.Builder(svc, ALERT_CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_omnisave)
                .setContentTitle(headTitle)
                .setContentText(shortErr)
                .setStyle(NotificationCompat.BigTextStyle().bigText(shortErr))
                .setAutoCancel(true)
                .setContentIntent(openPending)
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
            ENGINE_NOTIF_ID,
            buildEngineNotification("Ready for downloads"),
            fgType
        )
        startPythonEngineIfNeeded()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopPythonEngine()
                ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_CANCEL_JOB -> {
                val jid = intent.getStringExtra(EXTRA_JOB_ID) ?: ""
                if (jid.isNotBlank()) {
                    cancelJobNotification(jid)
                    Thread {
                        try {
                            if (Python.isStarted()) {
                                Python.getInstance().getModule("omnisave_studio").callAttr("cancel_job", jid)
                            }
                        } catch (_: Exception) {
                        }
                    }.start()
                }
                return START_STICKY
            }
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
                Os.setenv("XDG_CACHE_HOME", cacheDir.absolutePath, true)
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
                val nm = getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
                nm?.notify(ENGINE_NOTIF_ID, buildEngineNotification("Engine error: ${e.message ?: "stopped"}"))
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
                ENGINE_CHANNEL_ID,
                "OmniSave Background Engine",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Keeps the local download engine ready in the background"
                setShowBadge(false)
            }

            val progChannel = NotificationChannel(
                PROGRESS_CHANNEL_ID,
                "Active Downloads Progress",
                NotificationManager.IMPORTANCE_DEFAULT
            ).apply {
                description = "Live progress bar & speed for active downloads"
                setSound(null, null)
                enableVibration(false)
                setShowBadge(false)
            }

            val alertChannel = NotificationChannel(
                ALERT_CHANNEL_ID,
                "Download Completed / Failed Alerts",
                NotificationManager.IMPORTANCE_DEFAULT
            ).apply {
                description = "Notifies you when a download finishes or fails"
            }

            nm.createNotificationChannel(engChannel)
            nm.createNotificationChannel(progChannel)
            nm.createNotificationChannel(alertChannel)
        }
    }

    fun buildEngineNotification(stageText: String): Notification {
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

        return NotificationCompat.Builder(this, ENGINE_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_omnisave)
            .setContentTitle("OmniSave Studio")
            .setContentText(stageText)
            .setContentIntent(openPending)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .addAction(0, "Stop Engine", stopPending)
            .build()
    }
}
