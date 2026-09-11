package com.thereprocase.bambubridge.pairing

import android.annotation.SuppressLint
import android.net.http.SslError
import android.webkit.*
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.WritableMap
import com.facebook.react.uimanager.SimpleViewManager
import com.facebook.react.uimanager.ThemedReactContext
import com.facebook.react.uimanager.annotations.ReactProp
import com.facebook.react.uimanager.UIManagerHelper
import com.facebook.react.uimanager.events.Event
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import java.io.ByteArrayInputStream
import java.io.FilterInputStream
import com.thereprocase.bambubridge.viewing.ViewingTransport

/** The self-contained viewer uses GET/fetch polling, not browser WebSockets.
 * Every resource passes through the SAME paired transport as REST and WSS.
 * Returning a response for all requests prevents WebView network fall-through.
 */
class PairedViewerManager : SimpleViewManager<PairedViewer>() {
    override fun getName() = "PairedBridgeViewer"
    override fun createViewInstance(context: ThemedReactContext) = PairedViewer(context)
    @ReactProp(name = "uri") fun setUri(view: PairedViewer, uri: String?) {
        if (uri != null) view.open(uri)
    }
    override fun getExportedCustomDirectEventTypeConstants(): MutableMap<String, Any> = mutableMapOf(
        "topPairMessage" to mapOf("registrationName" to "onMessage"),
        "topPairError" to mapOf("registrationName" to "onError"),
        "topPairLoad" to mapOf("registrationName" to "onLoad")
    )
    override fun onDropViewInstance(view: PairedViewer) {
        view.stopLoading(); view.removeJavascriptInterface("ReactNativeWebView")
        view.destroy(); super.onDropViewInstance(view)
    }
}

@SuppressLint("SetJavaScriptEnabled")
class PairedViewer(private val reactContext: ThemedReactContext) : WebView(reactContext) {
    private var initial: String? = null
    private var transport: PairedTransport? = null
    init {
        setBackgroundColor(0xff111113.toInt())
        settings.javaScriptEnabled = true
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        settings.domStorageEnabled = false
        settings.cacheMode = WebSettings.LOAD_NO_CACHE
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        settings.setSupportMultipleWindows(false)
        settings.setGeolocationEnabled(false)
        addJavascriptInterface(object {
            @JavascriptInterface fun postMessage(data: String) {
                if (data.length <= 4096) emit("topPairMessage", data)
            }
        }, "ReactNativeWebView")
        webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest) = true
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                handler.cancel(); emit("topPairError", reason = "identity")
            }
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) emit("topPairError", reason = "network")
            }
            override fun onPageFinished(view: WebView, url: String) { emit("topPairLoad") }
            override fun shouldInterceptRequest(view: WebView, req: WebResourceRequest): WebResourceResponse {
                var response: okhttp3.Response? = null
                try {
                    val current = transport ?: throw SecurityException()
                    require(current === SecureBridgeModule.active)
                    val first = initial!!.toHttpUrl()
                    val url = req.url.toString().toHttpUrl()
                    require(req.method == "GET" && PairedTransport.sameOrigin(first, url))
                    // Only this printer's snapshot and viewer data, never command/admin routes.
                    val root = first.encodedPath.removeSuffix("/viz")
                    require(url.encodedPath == root || url.encodedPath == "$root/viz" ||
                        url.encodedPath.startsWith("$root/viz/") || url.encodedPath == "$root/mesh")
                    val token = first.queryParameter("token") ?: throw SecurityException()
                    val clean = url.newBuilder().removeAllQueryParameters("token").build()
                    val request = Request.Builder().url(clean).header("Authorization", "Bearer $token").build()
                    response = current.clientForRequest(url, "GET").newCall(request).execute()
                    require(current === SecureBridgeModule.active)
                    val r = response
                    require(r.code !in 300..399)
                    val body = r.body ?: throw IllegalStateException()
                    if (r.code >= 400) emit("topPairError", reason = if (r.code in listOf(401,403)) "auth" else "unavailable")
                    val stream = object : FilterInputStream(body.byteStream()) {
                        override fun close() { super.close(); r.close() }
                    }
                    return WebResourceResponse(
                        body.contentType()?.let { "${it.type}/${it.subtype}" } ?: "application/octet-stream",
                        "utf-8", r.code, r.message.ifBlank { "Response" }, mapOf(
                            "Cache-Control" to "no-store", "Referrer-Policy" to "no-referrer",
                            "Content-Security-Policy" to "default-src 'none'; script-src 'unsafe-inline'; " +
                                "style-src 'unsafe-inline'; connect-src 'self'; img-src data:; " +
                                "base-uri 'none'; form-action 'none'; frame-src 'none'; worker-src 'none'"
                        ), stream)
                } catch (e: Exception) {
                    response?.close(); emit("topPairError", reason = if (e is IllegalArgumentException) "identity" else ViewingTransport.failure(e))
                    return WebResourceResponse("text/plain", "utf-8", 502, "Unavailable",
                        mapOf("Cache-Control" to "no-store"), ByteArrayInputStream("Secure viewer unavailable".toByteArray()))
                }
            }
        }
    }
    fun open(uri: String) {
        try {
            val current = SecureBridgeModule.active ?: throw SecurityException()
            val url = uri.toHttpUrl()
            current.clientFor(url)
            require(url.encodedPath.matches(Regex("/api/v1/printers/[^/]+/viz")))
            require(!url.queryParameter("token").isNullOrEmpty())
            initial = uri; transport = current
            loadUrl(uri)
        } catch (_: Exception) { emit("topPairError", reason = "identity") }
    }
    private fun emit(name: String, data: String? = null, reason: String? = null) {
        post {
            UIManagerHelper.getEventDispatcherForReactTag(reactContext, id)?.dispatchEvent(
                ViewerEvent(UIManagerHelper.getSurfaceId(this), id, name,
                    Arguments.createMap().apply {
                        if (data != null) putString("data", data)
                        if (reason != null) putString("reason", reason)
                    }))
        }
    }
}

private class ViewerEvent(surface: Int, tag: Int, private val name: String,
    private val data: WritableMap) : Event<ViewerEvent>(surface, tag) {
    override fun getEventName() = name
    override fun getEventData() = data
    override fun canCoalesce() = false
}
