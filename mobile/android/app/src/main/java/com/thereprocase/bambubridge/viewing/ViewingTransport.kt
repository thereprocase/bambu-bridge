package com.thereprocase.bambubridge.viewing

import com.thereprocase.bambubridge.pairing.PairedTransport
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** Immutable credential scope shared by the camera and native print monitor. */
class ViewingTransport(val config: JSONObject) {
    private val closed = AtomicBoolean(false)
    val token = config.getString("bearer").also { require(it.isNotBlank()) }
    val primary = base(config.getString("base"))
    val alternate = optional("alternate")?.let { base(it) }
    val printer = config.getString("printer").also { require(it.isNotBlank() && it.length <= 128) }
    private val paired = optional("pin")?.let {
        PairedTransport(config.getString("pairedBase"), it, optional("remote"))
    }
    private val ordinary = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .retryOnConnectionFailure(false).connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS).callTimeout(0, TimeUnit.SECONDS).build()
    init { client(primary); alternate?.let { client(it) } }

    private fun optional(name: String): String? = if (config.isNull(name)) null else config.optString(name).takeIf { it.isNotBlank() }

    fun client(url: HttpUrl): OkHttpClient {
        require(listOfNotNull(primary, alternate).any { PairedTransport.sameOrigin(it, url) })
        return paired?.clientFor(url)?.newBuilder()?.connectTimeout(5, TimeUnit.SECONDS)
            ?.readTimeout(12, TimeUnit.SECONDS)?.callTimeout(0, TimeUnit.SECONDS)?.build() ?: ordinary
    }
    fun url(base: HttpUrl, suffix: String = ""): HttpUrl = base.newBuilder()
        .addPathSegment("printers").addPathSegment(printer).apply {
            if (suffix.isNotEmpty()) addPathSegments(suffix)
        }.build()
    fun request(url: HttpUrl) = Request.Builder().url(url)
        .header("Authorization", "Bearer $token").header("Cache-Control", "no-cache").build()
    fun close() {
        if (closed.getAndSet(true)) return
        // TLS close_notify may write to the socket. Player lifecycle callbacks
        // run on the UI thread, so pooled connections must be closed off-thread.
        cleanup.execute {
            paired?.close(); ordinary.dispatcher.cancelAll(); ordinary.connectionPool.evictAll()
        }
    }
    companion object {
        private val cleanup = Executors.newSingleThreadExecutor { task ->
            Thread(task, "BridgeTransportClose").apply { isDaemon = true }
        }
        fun base(value: String): HttpUrl = value.trimEnd('/').toHttpUrl().also {
            require(it.scheme in listOf("http", "https") && it.username.isEmpty() && it.password.isEmpty()
                && it.query == null && it.fragment == null && it.encodedPath == "/api/v1")
        }
        fun failure(error: Throwable): String {
            var e: Throwable? = error
            while (e != null) {
                if (e is javax.net.ssl.SSLException || e is java.security.cert.CertificateException || e is SecurityException)
                    return "identity"
                e = e.cause
            }
            return "network"
        }
    }
}
