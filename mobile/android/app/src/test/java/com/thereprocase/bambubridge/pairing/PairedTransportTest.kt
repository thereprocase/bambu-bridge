package com.thereprocase.bambubridge.pairing

import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import okio.ByteString.Companion.toByteString
import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLException

class PairedTransportTest {
    private fun pin(cert: HeldCertificate) = cert.certificate.publicKey.encoded.toByteString().sha256().base64()
    private fun server(cert: HeldCertificate): MockWebServer = MockWebServer().apply {
        useHttps(HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(), false)
        start()
    }

    @Test fun readsRecoverAClosedPoolConnectionWithoutEnablingCommandReplay() {
        val cert = HeldCertificate.Builder().build()
        server(cert).use { server ->
            val transport = PairedTransport(server.url("/api/v1").toString(), pin(cert))
            val url = server.url("/api/v1/printers")
            val reader = transport.clientForRequest(url, "GET")
            for (method in listOf("POST", "PUT", "PATCH", "DELETE"))
                assertFalse(transport.clientForRequest(url, method).retryOnConnectionFailure)
            assertFalse(reader.followRedirects)
            assertFalse(reader.followSslRedirects)
            server.enqueue(MockResponse().setBody("first"))
            reader.newCall(Request.Builder().url(url).build()).execute().use { assertEquals("first", it.body!!.string()) }
            server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
            server.enqueue(MockResponse().setBody("recovered"))
            reader.newCall(Request.Builder().url(url).build()).execute().use { assertEquals("recovered", it.body!!.string()) }
            transport.close()
        }
    }

    @Test fun pairedIdentityAuthenticatesDespiteNoPublicCaOrDnsName() {
        val cert = HeldCertificate.Builder().commonName("Bambu Bridge local").build()
        server(cert).use { server ->
            server.enqueue(MockResponse().setBody("verified"))
            val transport = PairedTransport(server.url("/api/v1").toString(), pin(cert))
            transport.localClient.newCall(Request.Builder().url(server.url("/api/v1/health")).build()).execute().use {
                assertEquals("verified", it.body!!.string())
            }
            transport.close()
        }
    }

    @Test fun wrongIdentityNeverReceivesTheCredential() {
        val actual = HeldCertificate.Builder().build()
        val expected = HeldCertificate.Builder().build()
        server(actual).use { server ->
            val transport = PairedTransport(server.url("/api/v1").toString(), pin(expected))
            assertThrows(SSLException::class.java) {
                transport.localClient.newCall(Request.Builder().url(server.url("/api/v1/printers"))
                    .header("Authorization", "Bearer must-not-arrive").build()).execute()
            }
            assertNull(server.takeRequest(100, TimeUnit.MILLISECONDS))
            transport.close()
        }
    }

    @Test fun expiredIdentityFailsClosed() {
        val expired = HeldCertificate.Builder().validityInterval(1, 1000).build()
        server(expired).use { server ->
            val transport = PairedTransport(server.url("/api/v1").toString(), pin(expired))
            assertThrows(SSLException::class.java) {
                transport.localClient.newCall(Request.Builder().url(server.url("/api/v1/health")).build()).execute()
            }
            transport.close()
        }
    }

    @Test fun noHttpNoOtherOriginAndNoRedirects() {
        val cert = HeldCertificate.Builder().build()
        server(cert).use { server ->
            val transport = PairedTransport(server.url("/api/v1").toString(), pin(cert))
            assertThrows(IllegalArgumentException::class.java) {
                transport.clientFor(server.url("/api/v1").newBuilder().scheme("http").build())
            }
            assertThrows(SecurityException::class.java) {
                transport.clientFor(server.url("/api/v1").newBuilder().port(server.port + 1).build())
            }
            server.enqueue(MockResponse().setResponseCode(302).addHeader("Location", "http://example.invalid/"))
            transport.localClient.newCall(Request.Builder().url(server.url("/api/v1/health")).build()).execute().use {
                assertEquals(302, it.code)
            }
            assertEquals(1, server.requestCount)
            assertFalse(transport.localClient.retryOnConnectionFailure)
            transport.close()
        }
    }

    @Test fun secureWebSocketUsesThePairedIdentity() {
        val cert = HeldCertificate.Builder().build()
        server(cert).use { server ->
            val done = CountDownLatch(1)
            server.enqueue(MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                override fun onOpen(ws: WebSocket, response: Response) { ws.send("snapshot") }
            }))
            val transport = PairedTransport(server.url("/api/v1").toString(), pin(cert))
            val ws = transport.localClient.newWebSocket(Request.Builder().url(server.url("/api/v1/status")).build(),
                object : WebSocketListener() {
                    override fun onMessage(ws: WebSocket, text: String) {
                        if (text == "snapshot") done.countDown()
                        ws.close(1000, null)
                    }
                })
            assertTrue(done.await(5, TimeUnit.SECONDS))
            ws.cancel(); transport.close()
        }
    }
}
