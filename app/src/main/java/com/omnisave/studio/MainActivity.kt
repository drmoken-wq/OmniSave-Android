package com.omnisave.studio

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.MimeTypeMap
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var splashOverlay: LinearLayout
    private lateinit var splashStatus: TextView

    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private var pendingShareUrl: String? = null
    private var serverReady: Boolean = false
    private var askedStorageOnce: Boolean = false

    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri: Uri? ->
        val cb = filePathCallback
        filePathCallback = null
        if (cb != null) {
            cb.onReceiveValue(if (uri != null) arrayOf(uri) else null)
        }
    }

    private val cookieLoginLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val count = result.data?.getIntExtra(CookieLoginActivity.EXTRA_SYNCED_COUNT, 0) ?: 0
            webView.evaluateJavascript("window.__onOmniCookiesSynced && window.__onOmniCookiesSynced($count);", null)
        }
    }

    private val notifPermLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ -> }

    private val legacyStorageLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ -> }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AndroidBridge.init(applicationContext)

        pendingShareUrl = extractSharedUrl(intent)
        startEngineService()
        requestPermissionsIfNeeded()

        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.parseColor("#09090F"))
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }

        webView = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#09090F"))
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.allowFileAccess = true
            settings.mediaPlaybackRequiresUserGesture = false
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE

            addJavascriptInterface(WebAppBridge(), "OmniSaveAndroid")

            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView?, url: String?) {
                    super.onPageFinished(view, url)
                    splashOverlay.visibility = View.GONE
                }
            }

            webChromeClient = object : WebChromeClient() {
                override fun onShowFileChooser(
                    webView: WebView?,
                    callback: ValueCallback<Array<Uri>>?,
                    fileChooserParams: FileChooserParams?
                ): Boolean {
                    filePathCallback?.onReceiveValue(null)
                    filePathCallback = callback
                    try {
                        fileChooserLauncher.launch(arrayOf("text/plain", "*/*"))
                        return true
                    } catch (_: Exception) {
                        filePathCallback = null
                        return false
                    }
                }
            }
        }

        splashOverlay = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#09090F"))
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }

        val titleTv = TextView(this).apply {
            text = "OmniSave Studio"
            setTextColor(Color.WHITE)
            textSize = 22f
            gravity = Gravity.CENTER
            setPadding(0, 0, 0, dp(16))
        }

        val spinner = ProgressBar(this)

        splashStatus = TextView(this).apply {
            text = "Starting local Python engine..."
            setTextColor(Color.parseColor("#94A3B8"))
            textSize = 13f
            gravity = Gravity.CENTER
            setPadding(0, dp(14), 0, 0)
        }

        splashOverlay.addView(titleTv)
        splashOverlay.addView(spinner)
        splashOverlay.addView(splashStatus)

        root.addView(webView)
        root.addView(splashOverlay)
        setContentView(root)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack()
                } else {
                    moveTaskToBack(true)
                }
            }
        })

        waitForServerAndLoad()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val shared = extractSharedUrl(intent)
        if (!shared.isNullOrBlank()) {
            pendingShareUrl = shared
            if (serverReady) {
                loadStudioUrl(shared)
            }
        }
    }

    private fun startEngineService() {
        val svcIntent = Intent(this, OmniSaveService::class.java)
        ContextCompat.startForegroundService(this, svcIntent)
    }

    private fun waitForServerAndLoad() {
        Thread {
            val pingUrl = "http://127.0.0.1:${OmniSaveService.PORT}/api/ping"
            var ok = false
            for (attempt in 1..120) {
                try {
                    val conn = (URL(pingUrl).openConnection() as HttpURLConnection).apply {
                        connectTimeout = 500
                        readTimeout = 500
                    }
                    val code = conn.responseCode
                    conn.disconnect()
                    if (code == 200) {
                        ok = true
                        break
                    }
                } catch (_: Exception) {
                }
                if (attempt == 15) {
                    runOnUiThread { splashStatus.text = "Initializing yt-dlp & FFmpeg..." }
                }
                Thread.sleep(250)
            }

            runOnUiThread {
                if (ok) {
                    serverReady = true
                    loadStudioUrl(pendingShareUrl)
                } else {
                    splashStatus.text = "Engine took too long to start. Reopen app to retry."
                }
            }
        }.start()
    }

    private fun loadStudioUrl(sharedUrl: String?) {
        val base = "http://127.0.0.1:${OmniSaveService.PORT}/"
        val target = if (!sharedUrl.isNullOrBlank()) {
            pendingShareUrl = null
            base + "?url=" + URLEncoder.encode(sharedUrl, "UTF-8")
        } else {
            base
        }
        webView.loadUrl(target)
    }

    private fun extractSharedUrl(intent: Intent?): String? {
        if (intent == null) return null
        if (intent.action == Intent.ACTION_SEND && intent.type == "text/plain") {
            val text = intent.getStringExtra(Intent.EXTRA_TEXT)?.trim()
            if (!text.isNullOrEmpty()) return text
        }
        return null
    }

    private fun requestPermissionsIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager() && !askedStorageOnce) {
                askedStorageOnce = true
                AlertDialog.Builder(this)
                    .setTitle("Storage Access Needed")
                    .setMessage("Grant 'All files access' so OmniSave can save downloads directly to your Downloads folder and read WhatsApp Statuses.")
                    .setPositiveButton("Grant Access") { _, _ ->
                        try {
                            val i = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION).apply {
                                data = Uri.parse("package:$packageName")
                            }
                            startActivity(i)
                        } catch (_: Exception) {
                            startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
                        }
                    }
                    .setNegativeButton("Later", null)
                    .show()
            }
        } else {
            val perms = arrayOf(
                Manifest.permission.READ_EXTERNAL_STORAGE,
                Manifest.permission.WRITE_EXTERNAL_STORAGE
            )
            val missing = perms.filter {
                ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
            }
            if (missing.isNotEmpty()) {
                legacyStorageLauncher.launch(missing.toTypedArray())
            }
        }
    }

    private fun getMimeForFile(file: File): String {
        val ext = file.extension.lowercase()
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "*/*"
    }

    inner class WebAppBridge {
        @JavascriptInterface
        fun openCookieLogin(site: String) {
            runOnUiThread {
                val i = Intent(this@MainActivity, CookieLoginActivity::class.java).apply {
                    putExtra(CookieLoginActivity.EXTRA_SITE, site)
                }
                cookieLoginLauncher.launch(i)
            }
        }

        @JavascriptInterface
        fun openFile(path: String) {
            runOnUiThread {
                try {
                    val file = File(path)
                    if (!file.exists()) {
                        Toast.makeText(this@MainActivity, "File not found: ${file.name}", Toast.LENGTH_SHORT).show()
                        return@runOnUiThread
                    }
                    val uri = FileProvider.getUriForFile(this@MainActivity, "${packageName}.fileprovider", file)
                    val i = Intent(Intent.ACTION_VIEW).apply {
                        setDataAndType(uri, getMimeForFile(file))
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    }
                    startActivity(Intent.createChooser(i, "Open with"))
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "Cannot open file: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }

        @JavascriptInterface
        fun shareFile(path: String) {
            runOnUiThread {
                try {
                    val file = File(path)
                    if (!file.exists()) {
                        Toast.makeText(this@MainActivity, "File not found: ${file.name}", Toast.LENGTH_SHORT).show()
                        return@runOnUiThread
                    }
                    val uri = FileProvider.getUriForFile(this@MainActivity, "${packageName}.fileprovider", file)
                    val i = Intent(Intent.ACTION_SEND).apply {
                        type = getMimeForFile(file)
                        putExtra(Intent.EXTRA_STREAM, uri)
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    }
                    startActivity(Intent.createChooser(i, "Share media"))
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "Cannot share file: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
