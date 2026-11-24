package com.omnisave.studio

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.Outline
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.ViewOutlineProvider
import android.webkit.JavascriptInterface
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
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Floating SnapTube-style bottom sheet launched over YouTube / Facebook / Instagram / TikTok
 * when sharing a URL to OmniSave. Lets you pick format & quality, then closes immediately while
 * OmniSaveService finishes downloading in the background!
 */
class QuickShareActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var loadingBox: LinearLayout
    private lateinit var loadingTxt: TextView

    private var sharedText: String = ""
    private var serverReady: Boolean = false

    private val cookieLoginLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val count = result.data?.getIntExtra(CookieLoginActivity.EXTRA_SYNCED_COUNT, 0) ?: 0
            webView.evaluateJavascript("window.__onOmniCookiesSynced && window.__onOmniCookiesSynced($count);", null)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AndroidBridge.init(applicationContext)

        sharedText = extractSharedText(intent)
        if (sharedText.isBlank()) {
            finish()
            return
        }

        ContextCompat.startForegroundService(this, Intent(this, OmniSaveService::class.java))

        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.TRANSPARENT)
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
            setOnClickListener { finish() }
        }

        val screenH = resources.displayMetrics.heightPixels
        val sheetH = (screenH * 0.62f).toInt().coerceAtLeast(dp(360))
        val cornerPx = dp(24).toFloat()

        val sheetBg = GradientDrawable().apply {
            setColor(Color.parseColor("#121420"))
            cornerRadii = floatArrayOf(cornerPx, cornerPx, cornerPx, cornerPx, 0f, 0f, 0f, 0f)
            setStroke(dp(1), Color.parseColor("#2A2F4A"))
        }

        val sheetContainer = FrameLayout(this).apply {
            background = sheetBg
            elevation = dp(16).toFloat()
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                sheetH,
                Gravity.BOTTOM
            )
            setOnClickListener { }
            outlineProvider = object : ViewOutlineProvider() {
                override fun getOutline(view: View, outline: Outline) {
                    outline.setRoundRect(0, 0, view.width, view.height + dp(24), cornerPx)
                }
            }
            clipToOutline = true
        }

        webView = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#121420"))
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE

            addJavascriptInterface(QuickBridge(), "OmniSaveAndroid")

            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView?, url: String?) {
                    super.onPageFinished(view, url)
                    loadingBox.visibility = View.GONE
                }
            }
            webChromeClient = WebChromeClient()
        }

        loadingBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#121420"))
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }

        val spin = ProgressBar(this)
        loadingTxt = TextView(this).apply {
            text = "Waking OmniSave engine..."
            setTextColor(Color.parseColor("#CBD5E1"))
            textSize = 13.5f
            gravity = Gravity.CENTER
            setPadding(dp(16), dp(12), dp(16), 0)
        }
        loadingBox.addView(spin)
        loadingBox.addView(loadingTxt)

        sheetContainer.addView(webView)
        sheetContainer.addView(loadingBox)
        root.addView(sheetContainer)
        setContentView(root)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                finish()
            }
        })

        waitAndLoadQuickUrl()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val txt = extractSharedText(intent)
        if (txt.isNotBlank()) {
            sharedText = txt
            if (serverReady) {
                loadQuickPage(txt)
            }
        }
    }

    private fun extractSharedText(intent: Intent?): String {
        if (intent == null) return ""
        if (intent.action == Intent.ACTION_SEND && intent.type == "text/plain") {
            return intent.getStringExtra(Intent.EXTRA_TEXT)?.trim() ?: ""
        }
        return ""
    }

    private fun waitAndLoadQuickUrl() {
        Thread {
            val pingUrl = "http://127.0.0.1:${OmniSaveService.PORT}/api/ping"
            var ok = false
            for (i in 1..120) {
                try {
                    val conn = (URL(pingUrl).openConnection() as HttpURLConnection).apply {
                        connectTimeout = 400
                        readTimeout = 400
                    }
                    val code = conn.responseCode
                    conn.disconnect()
                    if (code == 200) {
                        ok = true
                        break
                    }
                } catch (_: Exception) {
                }
                if (i == 12) {
                    runOnUiThread { loadingTxt.text = "Loading yt-dlp..." }
                }
                Thread.sleep(200)
            }

            runOnUiThread {
                if (ok) {
                    serverReady = true
                    loadQuickPage(sharedText)
                } else {
                    Toast.makeText(this, "OmniSave engine failed to start.", Toast.LENGTH_SHORT).show()
                    finish()
                }
            }
        }.start()
    }

    private fun loadQuickPage(urlText: String) {
        val enc = URLEncoder.encode(urlText, "UTF-8")
        webView.loadUrl("http://127.0.0.1:${OmniSaveService.PORT}/?quick=1&url=$enc")
    }

    inner class QuickBridge {
        @JavascriptInterface
        fun dismissAfterDownload(title: String) {
            runOnUiThread {
                val msg = if (title.isNotBlank()) "Downloading: ${title.take(40)}" else "Downloading in background..."
                Toast.makeText(applicationContext, msg, Toast.LENGTH_SHORT).show()
                finish()
            }
        }

        @JavascriptInterface
        fun closeSheet() {
            runOnUiThread { finish() }
        }

        @JavascriptInterface
        fun openFullApp(url: String) {
            runOnUiThread {
                val i = Intent(this@QuickShareActivity, MainActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                    if (url.isNotBlank()) {
                        putExtra(MainActivity.EXTRA_OPEN_URL, url)
                    }
                }
                startActivity(i)
                finish()
            }
        }

        @JavascriptInterface
        fun openCookieLogin(site: String) {
            runOnUiThread {
                val i = Intent(this@QuickShareActivity, CookieLoginActivity::class.java).apply {
                    putExtra(CookieLoginActivity.EXTRA_SITE, site)
                }
                cookieLoginLauncher.launch(i)
            }
        }
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
