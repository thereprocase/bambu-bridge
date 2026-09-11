package com.thereprocase.bambubridge.viewing

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ViewingTransportTest {
    @Test fun absentFallbackIsNotTreatedAsAnAddressAndRedirectsNeverCarryCredentials() {
        MockWebServer().use { server ->
            MockWebServer().use { other ->
                server.start(); other.start()
                val config = JSONObject().put("base", server.url("/api/v1").toString())
                    .put("alternate", JSONObject.NULL).put("bearer", "fixture-token").put("printer", "fixture")
                val transport = ViewingTransport(config)
                assertNull(transport.alternate)
                val url = transport.url(transport.primary, "camera/stream.mjpeg")
                server.enqueue(MockResponse().setResponseCode(302).setHeader("Location",other.url("/leak")))
                transport.client(url).newCall(transport.request(url)).execute().use { assertEquals(302,it.code) }
                assertEquals(0,other.requestCount)
                val received = server.takeRequest()
                assertEquals("Bearer fixture-token",received.getHeader("Authorization"))
                assertEquals("/api/v1/printers/fixture/camera/stream.mjpeg",received.path)
                assertThrows(IllegalArgumentException::class.java) { transport.client(other.url("/api/v1")) }
                transport.close()
            }
        }
    }
    @Test fun pairedCredentialCannotBeUsedOnAnHttpRoute() {
        val config = JSONObject().put("base", "http://home.invalid/api/v1").put("pairedBase", "https://home.invalid/api/v1")
            .put("pin", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=").put("bearer","fixture").put("printer","fixture")
        assertThrows(IllegalArgumentException::class.java) { ViewingTransport(config) }
    }
}
