package com.omnisave.studio

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class CookieLoginActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_SITE = "site"
        const val EXTRA_SYNCED_COUNT = "synced_count"
    }

    private lateinit var webView: WebView
    private var siteKey: String = "facebook"

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        siteKey = (intent.getStringExtra(EXTRA_SITE) ?: "facebook").lowercase()

        val (startUrl, label) = when (siteKey) {
            "instagram" -> "https://www.instagram.com/accounts/login/" to "Instagram Login"
            "youtube" -> "https://accounts.google.com/ServiceLogin?service=youtube" to "YouTube Login"
            "tiktok" -> "https://www.tiktok.com/login" to "TikTok Login"
            else -> "https://m.facebook.com/login/" to "Facebook Login"
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#09090F"))
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }

        val topBar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(Color.parseColor("#141622"))
            setPadding(dp(12), dp(10), dp(12), dp(10))
        }

        val closeBtn = Button(this).apply {
            text = "Close"
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#2A2E45"))
            setOnClickListener { finish() }
        }

        val titleTv = TextView(this).apply {
            text = label
            setTextColor(Color.WHITE)
            textSize = 15f
            gravity = Gravity.CENTER
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }

        val saveBtn = Button(this).apply {
            text = "Save Cookies"
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#10B981"))
            setOnClickListener { syncAndClose(this) }
        }

        topBar.addView(closeBtn)
        topBar.addView(titleTv)
        topBar.addView(saveBtn)
        root.addView(topBar)

        webView = WebView(this).apply {
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1f
            )
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            // Strip '; wv' so Google/YouTube login does not reject WebView
            val cleanUa = settings.userAgentString
                .replace("; wv", "")
                .replace("Version/4.0 ", "")
            settings.userAgentString = cleanUa
            webViewClient = WebViewClient()
            webChromeClient = WebChromeClient()
        }

        val cm = CookieManager.getInstance()
        cm.setAcceptCookie(true)
        cm.setAcceptThirdPartyCookies(webView, true)

        root.addView(webView)
        setContentView(root)
        webView.loadUrl(startUrl)
    }

    private fun syncAndClose(btn: Button) {
        btn.isEnabled = false
        btn.text = "Saving..."
        val cm = CookieManager.getInstance()
        cm.flush()

        val targets: List<Pair<String, String>> = when (siteKey) {
            "instagram" -> listOf(
                "https://www.instagram.com/" to ".instagram.com",
                "https://i.instagram.com/" to ".instagram.com"
            )
            "youtube" -> listOf(
                "https://www.youtube.com/" to ".youtube.com",
                "https://music.youtube.com/" to ".youtube.com",
                "https://accounts.google.com/" to ".google.com",
                "https://drive.google.com/" to ".google.com"
            )
            "tiktok" -> listOf(
                "https://www.tiktok.com/" to ".tiktok.com",
                "https://m.tiktok.com/" to ".tiktok.com"
            )
            else -> listOf(
                "https://www.facebook.com/" to ".facebook.com",
                "https://m.facebook.com/" to ".facebook.com",
                "https://facebook.com/" to ".facebook.com"
            )
        }

        val cookiesArr = JSONArray()
        val seen = HashSet<String>()
        val expiryFuture = (System.currentTimeMillis() / 1000L) + 31536000L

        for ((urlStr, domain) in targets) {
            val raw = cm.getCookie(urlStr) ?: continue
            for (part in raw.split(";")) {
                val trimmed = part.trim()
                val eq = trimmed.indexOf('=')
                if (eq <= 0) continue
                val name = trimmed.substring(0, eq).trim()
                val value = trimmed.substring(eq + 1).trim()
                if (name.isEmpty()) continue
                val key = "$domain|$name"
                if (seen.contains(key)) continue
                seen.add(key)

                val obj = JSONObject().apply {
                    put("domain", domain)
                    put("hostOnly", false)
                    put("path", "/")
                    put("secure", true)
                    put("expirationDate", expiryFuture)
                    put("name", name)
                    put("value", value)
                }
                cookiesArr.put(obj)
            }
        }

        if (cookiesArr.length() == 0) {
            Toast.makeText(this, "No cookies found yet. Sign in first, then tap Save Cookies.", Toast.LENGTH_LONG).show()
            btn.isEnabled = true
            btn.text = "Save Cookies"
            return
        }

        Thread {
            try {
                val payload = JSONObject().apply {
                    put("action", "ext_sync")
                    put("cookies", cookiesArr)
                }.toString().toByteArray(Charsets.UTF_8)

                val conn = (URL("http://127.0.0.1:${OmniSaveService.PORT}/api/cookies").openConnection() as HttpURLConnection).apply {
                    requestMethod = "POST"
                    connectTimeout = 5000
                    readTimeout = 5000
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json; charset=utf-8")
                }
                conn.outputStream.use { it.write(payload) }
                val code = conn.responseCode
                conn.disconnect()

                runOnUiThread {
                    if (code in 200..299) {
                        val res = Intent().putExtra(EXTRA_SYNCED_COUNT, cookiesArr.length())
                        setResult(Activity.RESULT_OK, res)
                        finish()
                    } else {
                        Toast.makeText(this, "Sync failed (HTTP $code)", Toast.LENGTH_SHORT).show()
                        btn.isEnabled = true
                        btn.text = "Save Cookies"
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this, "Error: ${e.message}", Toast.LENGTH_SHORT).show()
                    btn.isEnabled = true
                    btn.text = "Save Cookies"
                }
            }
        }.start()
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
