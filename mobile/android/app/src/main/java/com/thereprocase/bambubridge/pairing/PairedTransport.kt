package com.thereprocase.bambubridge.pairing

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.ConnectionPool
import okhttp3.OkHttpClient
import okio.ByteString.Companion.decodeBase64
import okio.ByteString.Companion.toByteString
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/** Out-of-band public-key trust, scoped to one exact HTTPS origin.
 * Does not modify Android's trust store, RN's global client, or other apps.
 */
class PairedTransport(base: String, pin: String, remote: String? = null) {
    val baseUrl = validateBase(base)
    val remoteUrl = remote?.takeIf { it.isNotBlank() }?.let { validateBase(it) }
    private val expected = pin.decodeBase64()?.toByteArray()
        ?.takeIf { it.size == 32 } ?: throw IllegalArgumentException("Invalid bridge identity")
    private val trust = object : X509TrustManager {
        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {
            throw CertificateException("Client trust is not supported")
        }
        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            val cert = chain.firstOrNull() ?: throw CertificateException("Missing bridge identity")
            cert.checkValidity()
            if (!matches(cert)) throw CertificateException("Bridge identity changed")
            cert.verify(cert.publicKey)
        }
    }
    private val context = SSLContext.getInstance("TLS").apply {
        init(null, arrayOf(trust), null)
    }
    val localClient: OkHttpClient = builder()
        .sslSocketFactory(context.socketFactory, trust)
        .hostnameVerifier { hostname, session ->
            // The scanned key is the identity authority; an IP/DNS name is
            // only its explicitly paired route. Never accept a different key.
            hostname == baseUrl.host && runCatching {
                matches(session.peerCertificates[0] as X509Certificate)
            }.getOrDefault(false)
        }.build()
    private val remoteClient = builder().build() // ordinary system CA + hostname checks
    // A command must not be replayed after a lost response. Give writes a
    // separate, non-idle pool so a later command does not inherit a socket
    // that the bridge or network closed between requests.
    private val localCommandClient = localClient.newBuilder()
        .connectionPool(ConnectionPool(0, 1, TimeUnit.MILLISECONDS)).build()
    private val remoteCommandClient = remoteClient.newBuilder()
        .connectionPool(ConnectionPool(0, 1, TimeUnit.MILLISECONDS)).build()

    private fun matches(cert: X509Certificate): Boolean = MessageDigest.isEqual(
        expected, cert.publicKey.encoded.toByteString().sha256().toByteArray())

    fun clientFor(url: HttpUrl): OkHttpClient {
        require(url.isHttps && url.username.isEmpty() && url.password.isEmpty()) {
            "Paired connections require HTTPS"
        }
        require(url.encodedPath == "/api/v1" || url.encodedPath.startsWith("/api/v1/")) {
            "Outside the bridge API"
        }
        if (sameOrigin(url, baseUrl)) return localClient
        if (remoteUrl != null && sameOrigin(url, remoteUrl)) return remoteClient
        throw SecurityException("Unpaired bridge address")
    }

    /** Recover a stale pooled connection only for reads. Commands are never replayed. */
    fun clientForRequest(url: HttpUrl, method: String): OkHttpClient {
        val readClient = clientFor(url) // validates scope and paired identity
        if (method == "GET") return readClient.newBuilder().retryOnConnectionFailure(true).build()
        return if (sameOrigin(url, baseUrl)) localCommandClient else remoteCommandClient
    }

    fun close() {
        for (client in listOf(localClient, remoteClient, localCommandClient, remoteCommandClient)) {
            client.dispatcher.cancelAll()
            client.connectionPool.evictAll()
        }
    }

    companion object {
        fun validateBase(value: String): HttpUrl {
            val url = value.toHttpUrl()
            require(url.isHttps && url.username.isEmpty() && url.password.isEmpty()
                && url.query == null && url.fragment == null
                && url.encodedPath.trimEnd('/') == "/api/v1") { "Invalid secure bridge address" }
            return url
        }
        fun sameOrigin(a: HttpUrl, b: HttpUrl): Boolean =
            a.scheme == b.scheme && a.host == b.host && a.port == b.port
        private fun builder() = OkHttpClient.Builder()
            .followRedirects(false).followSslRedirects(false).retryOnConnectionFailure(false)
            .connectTimeout(10, TimeUnit.SECONDS).readTimeout(30, TimeUnit.SECONDS)
            .callTimeout(30, TimeUnit.SECONDS)
    }
}
