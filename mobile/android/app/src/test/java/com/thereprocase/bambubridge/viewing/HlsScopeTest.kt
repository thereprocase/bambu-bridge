package com.thereprocase.bambubridge.viewing

import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Test

class HlsScopeTest {
    private val playlist = "https://bridge.example/api/v1/printers/p/camera/hls/index.m3u8".toHttpUrl()
    @Test fun acceptsSameCameraParts() {
        HlsScope.requireAllowed(playlist, playlist.resolve("video1_part0.mp4?_HLS_msn=1")!!)
    }
    @Test(expected = SecurityException::class) fun rejectsForeignHost() {
        HlsScope.requireAllowed(playlist, "https://evil.example/api/v1/printers/p/camera/hls/a.mp4".toHttpUrl())
    }
    @Test(expected = SecurityException::class) fun rejectsPlaintext() {
        HlsScope.requireAllowed(playlist, playlist.newBuilder().scheme("http").build())
    }
    @Test(expected = SecurityException::class) fun rejectsOtherCamera() {
        HlsScope.requireAllowed(playlist, playlist.resolve("../../../../other/camera/hls/a.mp4")!!)
    }
    @Test(expected = SecurityException::class) fun rejectsEmbeddedCredentials() {
        HlsScope.requireAllowed(playlist, playlist.newBuilder().username("leak").build())
    }
}
