package com.thereprocase.bambubridge.viewing

import okhttp3.HttpUrl

object HlsScope {
    fun requireAllowed(playlist: HttpUrl, target: HttpUrl) {
        val prefix = playlist.encodedPath.substringBeforeLast('/') + "/"
        if (!target.isHttps || target.host != playlist.host || target.port != playlist.port
            || target.username.isNotEmpty() || target.password.isNotEmpty()
            || !target.encodedPath.startsWith(prefix)) {
            throw SecurityException("Video resource escaped the paired camera scope")
        }
    }
}
