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
    fun onJobProgress(jobId: String, title: String, stage: String, percent: Int, subtext: String) {
        try {
            OmniSaveService.showJobProgress(jobId, title, stage, percent, subtext)
        } catch (_: Exception) {
        }
    }

    @JvmStatic
    fun onJobComplete(jobId: String, title: String, filepath: String, sizeText: String) {
        try {
            OmniSaveService.showJobComplete(jobId, title, filepath, sizeText)
        } catch (_: Exception) {
        }
    }

    @JvmStatic
    fun onJobError(jobId: String, title: String, errorMsg: String) {
        try {
            OmniSaveService.showJobError(jobId, title, errorMsg)
        } catch (_: Exception) {
        }
    }

    @JvmStatic
    fun onJobCancelled(jobId: String) {
        try {
            OmniSaveService.cancelJobNotification(jobId)
        } catch (_: Exception) {
        }
    }

    @JvmStatic
    fun onEngineSummary(activeCount: Int) {
        try {
            OmniSaveService.updateEngineSummary(activeCount)
        } catch (_: Exception) {
        }
    }
}
