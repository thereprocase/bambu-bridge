package com.thereprocase.bambubridge.pairing

/** Resource responses belong to the page, including its toolpath-to-mesh fallback. */
object ViewerResponsePolicy {
    fun errorReason(mainFrame: Boolean, status: Int): String? = when {
        status == 401 || status == 403 -> "auth"
        mainFrame && status >= 400 -> "unavailable"
        else -> null
    }
}
