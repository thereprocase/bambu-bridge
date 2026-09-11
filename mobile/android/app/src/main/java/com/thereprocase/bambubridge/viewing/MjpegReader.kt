package com.thereprocase.bambubridge.viewing

import okio.BufferedSource
import java.io.IOException

/** Bounded multipart reader. One JPEG at a time; never buffers an endless response. */
class MjpegReader(private val source: BufferedSource) {
    fun next(): ByteArray {
        // A peer trickling bytes cannot extend a single frame indefinitely.
        source.timeout().deadline(12, java.util.concurrent.TimeUnit.SECONDS)
        var budget = 4096L
        var length: Int? = null
        var boundary = false
        while (true) {
            if (budget <= 0) throw IOException("Camera headers too large")
            val line = source.readUtf8LineStrict(budget)
            budget -= line.toByteArray().size + 2
            if (line.startsWith("--")) {
                if (line.endsWith("--")) throw IOException("Camera stream ended")
                boundary = true
            } else if (boundary && line.isEmpty()) break
            else if (boundary && line.substringBefore(':').equals("Content-Length", true)) {
                if (length != null) throw IOException("Duplicate camera length")
                length = line.substringAfter(':').trim().toIntOrNull()
                    ?: throw IOException("Invalid camera length")
            }
        }
        val size = length?.takeIf { it in 4..8*1024*1024 }
            ?: throw IOException("Invalid camera length")
        return source.readByteArray(size.toLong()).also {
            if (it[0] != 0xff.toByte() || it[1] != 0xd8.toByte() ||
                it[it.size-2] != 0xff.toByte() || it.last() != 0xd9.toByte()) {
                throw IOException("Invalid camera JPEG")
            }
        }
    }
}
