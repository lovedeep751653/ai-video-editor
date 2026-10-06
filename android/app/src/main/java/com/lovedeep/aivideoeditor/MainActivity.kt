package com.lovedeep.aivideoeditor

import android.Manifest
import android.annotation.SuppressLint
import android.content.ContentValues
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Log
import android.view.View
import android.webkit.ConsoleMessage
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * The whole app is one screen: a WebView showing the editor that runs inside this app
 * (see Engine). This activity adds what a web page can't do on its own: picking files from
 * the gallery without copying them, saving to the gallery, sharing, and the progress notification.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var loading: View
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var pageReady = false
    private val pendingJs = mutableListOf<String>()
    private var filePicker: ValueCallback<Array<Uri>>? = null
    private var pendingSave: (() -> Unit)? = null

    private val pickVisual = registerForActivityResult(ActivityResultContracts.PickMultipleVisualMedia(60)) { uris ->
        deliverPicked("visual", uris)
    }
    private val pickAudio = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        deliverPicked("audio", listOfNotNull(uri))
    }
    private val pickAny = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val data = result.data
        val uris = when {
            data?.clipData != null -> (0 until data.clipData!!.itemCount).map { data.clipData!!.getItemAt(it).uri }
            data?.data != null -> listOf(data.data!!)
            else -> emptyList()
        }
        filePicker?.onReceiveValue(uris.toTypedArray())
        filePicker = null
    }
    private val askPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        val action = pendingSave
        pendingSave = null
        if (ok) action?.invoke() else js("onNativeSaved", JSONObject().put("ok", false)
            .put("message", "Saving needs permission to use your phone's storage."))
    }
    private val askNotify = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    private val watcher = object : Runnable {
        override fun run() {
            if (Engine.ready && Engine.busy()?.optBoolean("working") == true) WorkService.ensure(this@MainActivity)
            main.postDelayed(this, 1000)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        web = findViewById(R.id.web)
        loading = findViewById(R.id.loading)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.root)) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) WebView.setWebContentsDebuggingEnabled(true)
        configureWebView()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else moveTaskToBack(true)
            }
        })
        main.postDelayed({
            if (!Engine.ready) findViewById<TextView>(R.id.loading_text).setText(R.string.first_start)
        }, 2500)
        Engine.startAsync(this)
        Engine.whenReady { onEngine() }
        handleShare(intent)
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            askNotify.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    override fun onResume() {
        super.onResume()
        main.post(watcher)
    }

    override fun onPause() {
        main.removeCallbacks(watcher)
        super.onPause()
    }

    private fun onEngine() {
        val err = Engine.error
        if (err != null) {
            findViewById<View>(R.id.spinner).visibility = View.GONE
            findViewById<TextView>(R.id.loading_text).text = getString(R.string.start_failed, err)
            return
        }
        val cm = CookieManager.getInstance()
        cm.setAcceptCookie(true)
        cm.setCookie(Engine.baseUrl, "editor_token=${Engine.token}; path=/")
        cm.flush()
        if (intent?.getBooleanExtra("selftest", false) == true) SelfTest.run(this)
        web.loadUrl(Engine.baseUrl + "/")
    }

    private fun configureWebView() {
        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = false
            allowContentAccess = true
            cacheMode = WebSettings.LOAD_NO_CACHE
            textZoom = 100
        }
        web.setBackgroundColor(ContextCompat.getColor(this, R.color.bg))
        web.addJavascriptInterface(Bridge(), "Android")
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView?, callback: ValueCallback<Array<Uri>>?,
                                           params: FileChooserParams?): Boolean {
                filePicker?.onReceiveValue(null)
                filePicker = callback
                val intent = (params?.createIntent() ?: Intent(Intent.ACTION_GET_CONTENT).apply { type = "*/*" })
                    .apply { putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true) }
                return try {
                    pickAny.launch(Intent.createChooser(intent, "Choose files"))
                    true
                } catch (e: Exception) {
                    filePicker = null
                    false
                }
            }

            override fun onConsoleMessage(m: ConsoleMessage): Boolean {
                Log.i("EditorPage", "${m.messageLevel()}: ${m.message()} (${m.sourceId()}:${m.lineNumber()})")
                return true
            }
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, req: WebResourceRequest?): Boolean {
                val url = req?.url ?: return false
                if (url.host == "127.0.0.1") return false
                // Links to other sites (like the Google AI key page) open in the phone's browser.
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, url))
                } catch (e: Exception) {
                }
                return true
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                pageReady = true
                loading.animate().alpha(0f).setDuration(200).withEndAction { loading.visibility = View.GONE }
                web.visibility = View.VISIBLE
                val queued = pendingJs.toList()
                pendingJs.clear()
                queued.forEach { web.evaluateJavascript(it, null) }
            }
        }
    }

    // ---------------------------------------------------------------- page ⇄ app

    private fun js(fn: String, arg: JSONObject) {
        val code = "window.$fn && window.$fn(${arg});"
        main.post {
            if (pageReady) web.evaluateJavascript(code, null) else pendingJs.add(code)
        }
    }

    private fun deliverPicked(kind: String, uris: List<Uri>, fn: String = "onNativePicked") {
        if (uris.isEmpty()) {
            js(fn, JSONObject().put("kind", kind).put("cancelled", true))
            return
        }
        io.execute {
            val items = JSONArray()
            for (u in uris) {
                try {
                    items.put(Sources.register(u))
                } catch (e: Exception) {
                    Log.w("Picker", "couldn't use $u", e)
                }
            }
            if (items.length() == 0) {
                js(fn, JSONObject().put("kind", kind).put("error", "The phone didn't share those files."))
            } else {
                js(fn, JSONObject().put("kind", kind).put("items", items))
            }
        }
    }

    private fun handleShare(intent: Intent?) {
        if (intent == null) return
        val uris = when (intent.action) {
            Intent.ACTION_SEND -> listOfNotNull(
                if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE ->
                (if (Build.VERSION.SDK_INT >= 33) intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM)) ?: emptyList()
            else -> emptyList()
        }
        if (uris.isNotEmpty()) deliverPicked("visual", uris, "onNativeShared")
    }

    private fun download(urlPath: String): Pair<InputStream, HttpURLConnection> {
        val conn = URL(Engine.baseUrl + urlPath).openConnection() as HttpURLConnection
        conn.setRequestProperty("Cookie", "editor_token=${Engine.token}")
        conn.connectTimeout = 10000
        conn.readTimeout = 60000
        if (conn.responseCode !in 200..299) throw IllegalStateException("The file isn't available (${conn.responseCode}).")
        return conn.inputStream to conn
    }

    private fun saveToGallery(urlPath: String, fileName: String, mime: String) {
        io.execute {
            try {
                val video = mime.startsWith("video/")
                val folder = if (video) Environment.DIRECTORY_MOVIES else Environment.DIRECTORY_PICTURES
                val (input, conn) = download(urlPath)
                input.use { inp ->
                    if (Build.VERSION.SDK_INT >= 29) {
                        val values = ContentValues().apply {
                            put(MediaStore.MediaColumns.DISPLAY_NAME, fileName)
                            put(MediaStore.MediaColumns.MIME_TYPE, mime)
                            put(MediaStore.MediaColumns.RELATIVE_PATH, "$folder/AI editor open source")
                            put(MediaStore.MediaColumns.IS_PENDING, 1)
                        }
                        val collection = if (video) MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                        else MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                        val item = contentResolver.insert(collection, values) ?: throw IllegalStateException("No space in the gallery")
                        contentResolver.openOutputStream(item)!!.use { inp.copyTo(it, 1 shl 20) }
                        values.clear()
                        values.put(MediaStore.MediaColumns.IS_PENDING, 0)
                        contentResolver.update(item, values, null, null)
                    } else {
                        @Suppress("DEPRECATION")
                        val dir = File(Environment.getExternalStoragePublicDirectory(folder), "AI editor open source")
                        dir.mkdirs()
                        val out = File(dir, fileName)
                        out.outputStream().use { inp.copyTo(it, 1 shl 20) }
                        MediaScannerConnection.scanFile(this, arrayOf(out.path), arrayOf(mime), null)
                    }
                }
                conn.disconnect()
                js("onNativeSaved", JSONObject().put("ok", true)
                    .put("message", "Saved to your gallery (${if (video) "Movies" else "Pictures"} › AI editor open source)."))
            } catch (e: Exception) {
                js("onNativeSaved", JSONObject().put("ok", false).put("message", "Couldn't save: ${e.message}"))
            }
        }
    }

    private fun share(urlPath: String, fileName: String, mime: String) {
        io.execute {
            try {
                val dir = File(cacheDir, "share").apply { deleteRecursively(); mkdirs() }
                val out = File(dir, fileName.replace(Regex("[^\\w. -]"), "_"))
                val (input, conn) = download(urlPath)
                input.use { inp -> out.outputStream().use { inp.copyTo(it, 1 shl 20) } }
                conn.disconnect()
                val uri = FileProvider.getUriForFile(this, "$packageName.files", out)
                val send = Intent(Intent.ACTION_SEND).setType(mime).putExtra(Intent.EXTRA_STREAM, uri)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                main.post { startActivity(Intent.createChooser(send, "Share video")) }
            } catch (e: Exception) {
                main.post { Toast.makeText(this, "Couldn't share: ${e.message}", Toast.LENGTH_LONG).show() }
            }
        }
    }

    inner class Bridge {
        @JavascriptInterface
        fun pickMedia(kind: String) {
            main.post {
                try {
                    if (kind == "audio") pickAudio.launch(arrayOf("audio/*", "video/*"))
                    else pickVisual.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageAndVideo))
                } catch (e: Exception) {
                    js("onNativePicked", JSONObject().put("kind", kind).put("error", "Couldn't open your gallery: ${e.message}"))
                }
            }
        }

        @JavascriptInterface
        fun saveToGallery(urlPath: String, fileName: String, mime: String) {
            main.post {
                if (Build.VERSION.SDK_INT < 29 && ContextCompat.checkSelfPermission(this@MainActivity,
                        Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                    pendingSave = { saveToGallery(urlPath, fileName, mime) }
                    askPermission.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                } else {
                    saveToGallery(urlPath, fileName, mime)
                }
            }
        }

        @JavascriptInterface
        fun share(urlPath: String, fileName: String, mime: String) = this@MainActivity.share(urlPath, fileName, mime)

        @JavascriptInterface
        fun openExternal(url: String) {
            main.post {
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                } catch (e: Exception) {
                }
            }
        }

        @JavascriptInterface
        fun appVersion(): String = try {
            packageManager.getPackageInfo(packageName, 0).versionName ?: ""
        } catch (e: Exception) {
            ""
        }
    }
}
