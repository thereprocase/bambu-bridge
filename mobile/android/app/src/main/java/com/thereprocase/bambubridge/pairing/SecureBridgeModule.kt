package com.thereprocase.bambubridge.pairing

import android.net.Uri
import com.facebook.react.bridge.*
import com.facebook.react.modules.core.DeviceEventManagerModule
import okhttp3.*
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okio.ByteString.Companion.toByteString
import okio.BufferedSink
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLException

class SecureBridgeModule(private val context: ReactApplicationContext) : ReactContextBaseJavaModule(context) {
    override fun getName() = "SecureBridge"
    private val executor = Executors.newFixedThreadPool(3)
    private val calls = ConcurrentHashMap<String, Call>()
    private val sockets = ConcurrentHashMap<String, WebSocket>()

    companion object {
        private var pendingScan: Promise? = null
        fun finishScan(contents: String?) {
            pendingScan?.resolve(contents); pendingScan = null
        }
        @Volatile var active: PairedTransport? = null
            private set
    }

    @ReactMethod fun scan(promise: Promise) {
        val activity = context.currentActivity as? com.thereprocase.bambubridge.MainActivity
        if (activity == null) { promise.reject("PAIR_SCAN", "Open the app to scan a code"); return }
        activity.runOnUiThread {
            if (pendingScan != null) { promise.reject("PAIR_SCAN", "Scanner already open"); return@runOnUiThread }
            pendingScan = promise
            try { activity.scanBridgeCode() }
            catch (_: Exception) {
                pendingScan = null; promise.reject("PAIR_SCAN", "Couldn't open the camera. Paste the pairing code instead.")
            }
        }
    }

    @ReactMethod fun configure(base: String?, pin: String?, remote: String?, promise: Promise) {
        try {
            val next = if (base != null && pin != null) PairedTransport(base, pin, remote) else null
            val old = active
            active = next
            old?.close()
            sockets.values.forEach { it.cancel() }
            sockets.clear()
            calls.values.forEach { it.cancel() }
            calls.clear()
            promise.resolve(null)
        } catch (_: Exception) { promise.reject("PAIR_CONFIG", "Invalid paired connection") }
    }

    @ReactMethod fun claim(base: String, pin: String, secret: String, name: String, promise: Promise) {
        executor.execute {
            var transport: PairedTransport? = null
            try {
                require(secret.length in 32..128 && name.length in 1..64)
                transport = PairedTransport(base, pin)
                val body = JSONObject().put("secret", secret).put("name", name).toString()
                    .toRequestBody("application/json".toMediaType())
                val url = base.trimEnd('/') + "/pairing/claim"
                transport.localClient.newCall(Request.Builder().url(url).post(body).build()).execute().use { r ->
                    if (r.code != 200) {
                        promise.reject("PAIR_REJECTED", "Pairing code expired or already used. Create a new code.")
                    } else {
                        val result = r.body?.string() ?: throw IOException()
                        val data = JSONObject(result)
                        require(data.getString("token").matches(Regex("bbd_[A-Za-z0-9_-]{43}")))
                        require(data.getString("device_id").matches(Regex("[a-f0-9]{24}")))
                        promise.resolve(result)
                    }
                }
            } catch (e: Exception) { reject(promise, e) }
            finally { transport?.close() }
        }
    }

    @ReactMethod fun request(id: String, url: String, method: String, headers: ReadableMap,
                             body: String?, timeout: Double, binary: Boolean, promise: Promise) {
        try {
            val transport = active ?: throw SecurityException()
            val target = url.toHttpUrl()
            val client = transport.clientForRequest(target, method)
            require(method in setOf("GET", "POST", "PUT", "PATCH", "DELETE"))
            val request = Request.Builder().url(target)
            val keys = headers.keySetIterator()
            while (keys.hasNextKey()) {
                val key = keys.nextKey()
                if (key.lowercase() in setOf("authorization", "accept", "content-type")) {
                    request.header(key, headers.getString(key) ?: "")
                }
            }
            request.method(method, if (method == "GET") null else
                (body ?: "").toRequestBody("application/json".toMediaType()))
            val call = client.newCall(request.build())
            call.timeout().timeout(timeout.toLong().coerceIn(1000, 120000), TimeUnit.MILLISECONDS)
            calls[id] = call
            call.enqueue(object : okhttp3.Callback {
                override fun onFailure(call: Call, e: IOException) { calls.remove(id); reject(promise, e) }
                override fun onResponse(call: Call, response: Response) {
                    response.use { r ->
                        try {
                            if (active !== transport) throw SecurityException()
                            val data = r.body?.bytes() ?: byteArrayOf()
                            if (active !== transport) throw SecurityException()
                            promise.resolve(Arguments.createMap().apply {
                                putInt("status", r.code)
                                putString("contentType", r.header("Content-Type") ?: "application/octet-stream")
                                putString("body", if (binary && r.isSuccessful) data.toByteString().base64()
                                    else data.toString(Charsets.UTF_8))
                            })
                        } catch (e: Exception) { reject(promise, e) }
                        finally { calls.remove(id) }
                    }
                }
            })
        } catch (e: Exception) { reject(promise, e) }
    }

    @ReactMethod fun cancelRequest(id: String) { calls.remove(id)?.cancel() }

    /** Stream a backup into app-private cache; avoid a huge base64 bridge response. */
    @ReactMethod fun downloadNativeBackup(url: String, token: String, promise: Promise) {
        executor.execute {
            var file: File? = null
            try {
                val transport = active ?: throw SecurityException()
                val target = url.toHttpUrl()
                val client = transport.clientForRequest(target, "GET").newBuilder()
                    .callTimeout(10, TimeUnit.MINUTES).readTimeout(2, TimeUnit.MINUTES).build()
                val request = Request.Builder().url(target)
                    .header("Authorization", "Bearer $token").get().build()
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) {
                        promise.reject("BACKUP_HTTP", "Backup download refused (HTTP ${response.code})")
                        return@execute
                    }
                    context.cacheDir.listFiles()?.filter {
                        it.name.startsWith("native-backup-") &&
                            System.currentTimeMillis() - it.lastModified() > TimeUnit.DAYS.toMillis(1)
                    }?.forEach { it.delete() }
                    file = File.createTempFile("native-backup-", ".zip", context.cacheDir)
                    val source = response.body?.byteStream() ?: throw IOException("Empty backup response")
                    source.use { input -> file!!.outputStream().use { input.copyTo(it) } }
                    if (active !== transport) throw SecurityException()
                    promise.resolve(Uri.fromFile(file!!).toString())
                }
            } catch (e: Exception) { file?.delete(); reject(promise, e) }
        }
    }

    /** Upload a document through the same pinned paired transport. */
    @ReactMethod fun importNativeBackup(url: String, token: String, uri: String, promise: Promise) {
        executor.execute {
            try {
                val transport = active ?: throw SecurityException()
                val target = url.toHttpUrl()
                val client = transport.clientForRequest(target, "POST").newBuilder()
                    .callTimeout(10, TimeUnit.MINUTES).writeTimeout(2, TimeUnit.MINUTES).build()
                val sourceUri = Uri.parse(uri)
                val body = object : RequestBody() {
                    override fun contentType() = "application/zip".toMediaType()
                    override fun writeTo(sink: BufferedSink) {
                        val input = context.contentResolver.openInputStream(sourceUri)
                            ?: throw IOException("Cannot read selected backup")
                        input.use { it.copyTo(sink.outputStream()) }
                    }
                }
                val multipart = MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("file", "native-inbox.zip", body).build()
                val request = Request.Builder().url(target)
                    .header("Authorization", "Bearer $token").post(multipart).build()
                client.newCall(request).execute().use { response ->
                    val result = response.body?.string() ?: ""
                    if (!response.isSuccessful) {
                        promise.reject("BACKUP_HTTP", "Backup import refused (HTTP ${response.code})")
                        return@execute
                    }
                    if (active !== transport) throw SecurityException()
                    promise.resolve(result)
                }
            } catch (e: Exception) { reject(promise, e) }
        }
    }

    @ReactMethod fun connect(id: String, url: String, token: String) {
        try {
            val transport = active ?: throw SecurityException()
            val target = url.replaceFirst("wss://", "https://").toHttpUrl()
            val request = Request.Builder().url(target).header("Authorization", "Bearer $token").build()
            val socket = transport.clientFor(target).newWebSocket(request, object : WebSocketListener() {
                override fun onOpen(ws: WebSocket, response: Response) {
                    if (active !== transport) { ws.cancel(); return }
                    emit(id, "open")
                }
                override fun onMessage(ws: WebSocket, text: String) {
                    if (active === transport) emit(id, "message", text)
                }
                override fun onClosing(ws: WebSocket, code: Int, reason: String) { ws.close(code, null) }
                override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                    sockets.remove(id); emit(id, "close", code = code)
                }
                override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                    sockets.remove(id)
                    emit(id, "error", if (securityFailure(t)) "PAIR_IDENTITY" else "PAIR_NETWORK")
                    // 1006, never 1008: a TLS reset or pin failure is a transport
                    // failure. The JS side treats 1008 as a terminal credential
                    // close (only the bridge's own close frame, via onClosed, may
                    // send it) and retries 1006 on the other URL with backoff.
                    emit(id, "close", code = 1006)
                }
            })
            sockets[id] = socket
        } catch (e: Exception) {
            emit(id, "error", if (securityFailure(e)) "PAIR_IDENTITY" else "PAIR_NETWORK")
            emit(id, "close", code = 1008)
        }
    }

    @ReactMethod fun send(id: String, text: String) { sockets[id]?.send(text) }
    @ReactMethod fun close(id: String, code: Int) { sockets.remove(id)?.close(code, null) }
    @ReactMethod fun addListener(name: String) { /* NativeEventEmitter contract */ }
    @ReactMethod fun removeListeners(count: Int) { /* NativeEventEmitter contract */ }

    private fun emit(id: String, type: String, data: String? = null, code: Int = 0) {
        context.getJSModule(DeviceEventManagerModule.RCTDeviceEventEmitter::class.java)
            .emit("secureBridgeSocket", Arguments.createMap().apply {
                putString("id", id); putString("type", type); putString("data", data); putInt("code", code)
            })
    }
    private fun securityFailure(e: Throwable): Boolean =
        e is SSLException || e is SecurityException || e is IllegalArgumentException ||
            (e.cause != null && e.cause !== e && securityFailure(e.cause!!))

    private fun reject(promise: Promise, e: Throwable) {
        if (securityFailure(e)) promise.reject("PAIR_IDENTITY", "Couldn't verify this bridge. Check its identity and pair again.")
        else promise.reject("PAIR_NETWORK", "Couldn't reach the paired bridge. Check your connection.")
    }

    override fun invalidate() {
        pendingScan?.reject("PAIR_SCAN", "Scanner closed"); pendingScan = null
        active?.close(); active = null
        sockets.values.forEach { it.cancel() }; sockets.clear()
        calls.values.forEach { it.cancel() }; calls.clear()
        executor.shutdownNow()
        super.invalidate()
    }
}
