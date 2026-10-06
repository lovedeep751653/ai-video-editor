package com.lovedeep.aivideoeditor

import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.view.View
import android.webkit.CookieManager
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

/**
 * The app is a full-screen window onto the AI editor open source running on the
 * user's own server. On first launch it asks once for that address and
 * remembers it; everything else (uploading, editing, downloading) happens in
 * the page itself.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var setup: View
    private var filePicker: ValueCallback<Array<Uri>>? = null

    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val data = result.data
        val uris = when {
            data?.clipData != null -> (0 until data.clipData!!.itemCount).map { data.clipData!!.getItemAt(it).uri }
            data?.data != null -> listOf(data.data!!)
            else -> emptyList()
        }
        filePicker?.onReceiveValue(uris.toTypedArray())
        filePicker = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        web = findViewById(R.id.web)
        setup = findViewById(R.id.setup)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.root)) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        configureWebView()
        findViewById<Button>(R.id.connect).setOnClickListener { saveAddress() }
        findViewById<Button>(R.id.change_address).setOnClickListener { showSetup(null) }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (setup.visibility != View.VISIBLE && web.canGoBack()) web.goBack() else finish()
            }
        })

        val saved = prefs().getString(KEY_URL, null)
        if (saved.isNullOrBlank()) showSetup(null) else open(saved)
    }

    private fun prefs() = getSharedPreferences("editor", Context.MODE_PRIVATE)

    private fun configureWebView() = with(web.settings) {
        javaScriptEnabled = true
        domStorageEnabled = true
        mediaPlaybackRequiresUserGesture = false
        allowFileAccess = true
        loadWithOverviewMode = true
        useWideViewPort = true
        cacheMode = WebSettings.LOAD_DEFAULT
        CookieManager.getInstance().setAcceptCookie(true)

        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView?, callback: ValueCallback<Array<Uri>>?, params: FileChooserParams?
            ): Boolean {
                filePicker?.onReceiveValue(null)
                filePicker = callback
                val intent = (params?.createIntent() ?: Intent(Intent.ACTION_GET_CONTENT).apply {
                    type = "*/*"
                }).apply { putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true) }
                return try {
                    pickFiles.launch(Intent.createChooser(intent, "Choose videos and photos"))
                    true
                } catch (e: Exception) {
                    filePicker = null
                    false
                }
            }
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, req: WebResourceRequest?): Boolean {
                val url = req?.url ?: return false
                val host = Uri.parse(prefs().getString(KEY_URL, "") ?: "").host
                // Links to other sites (like the Google AI key page) open in the phone's browser.
                if (url.host != null && url.host != host) {
                    startActivity(Intent(Intent.ACTION_VIEW, url))
                    return true
                }
                return false
            }

            override fun onReceivedError(view: WebView?, req: WebResourceRequest?, err: WebResourceError?) {
                if (req?.isForMainFrame == true) {
                    showSetup("Couldn't reach that address. Check it is correct and that you are online.")
                }
            }
        }
        web.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
            try {
                val request = DownloadManager.Request(Uri.parse(url))
                    .setMimeType(mimeType)
                    .addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url) ?: "")
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                val name = android.webkit.URLUtil.guessFileName(url, contentDisposition, mimeType)
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_MOVIES, name)
                (getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).enqueue(request)
                Toast.makeText(this@MainActivity, "Saving $name to your Movies folder", Toast.LENGTH_LONG).show()
            } catch (e: Exception) {
                Toast.makeText(this@MainActivity, "Couldn't save the file: ${e.message}", Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun showSetup(message: String?) {
        setup.visibility = View.VISIBLE
        web.visibility = View.GONE
        findViewById<TextView>(R.id.setup_error).apply {
            text = message ?: ""
            visibility = if (message == null) View.GONE else View.VISIBLE
        }
        findViewById<EditText>(R.id.address).setText(prefs().getString(KEY_URL, "") ?: "")
    }

    private fun saveAddress() {
        var address = findViewById<EditText>(R.id.address).text.toString().trim()
        if (address.isEmpty()) {
            showSetup("Please paste the address of your editor first.")
            return
        }
        if (!address.startsWith("http://") && !address.startsWith("https://")) address = "https://$address"
        address = address.trimEnd('/')
        prefs().edit().putString(KEY_URL, address).apply()
        open(address)
    }

    private fun open(address: String) {
        setup.visibility = View.GONE
        web.visibility = View.VISIBLE
        web.loadUrl(address)
    }

    companion object {
        private const val KEY_URL = "server_url"
    }
}
