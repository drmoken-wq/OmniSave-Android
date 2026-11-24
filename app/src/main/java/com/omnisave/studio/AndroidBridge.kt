package com.omnisave.studio

import android.content.Context
import android.media.MediaScannerConnection

/**
 * Static bridge called directly from Python (omnisave_studio.py) via Chaquopy.
 */
object AndroidBridge {
    @Volatile
    private var appContext: Context? = null

    @JvmStatic
    fun init(context: Context) {
        appContext = context.applicationContext
    }

    @JvmStatic
    fun scanFile(path: String) {
        val ctx = appContext ?: return
        if (path.isBlank()) return
        try {
            MediaScannerConnection.scanFile(ctx, arrayOf(path), null, null)
        } catch (_: Exception) {
        }
    }

    @JvmStatic
    fun onDownloadProgress(title: String, stage: String, percent: Int, active: Boolean) {
        try {
            OmniSaveService.updateNotification(title, stage, percent, active)
        } catch (_: Exception) {
        }
    }
}
