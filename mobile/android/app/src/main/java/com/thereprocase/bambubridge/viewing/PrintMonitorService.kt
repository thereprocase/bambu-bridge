package com.thereprocase.bambubridge.viewing

import android.app.*
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import com.thereprocase.bambubridge.MainActivity
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/** User-started connection to the printer bridge; independent of React Native's JS lifecycle. */
class PrintMonitorService : Service() {
    companion object {
        const val STOP = "com.thereprocase.bambubridge.STOP_MONITOR"
        @Volatile var running = false
        @Volatile var printer = ""
        @Volatile var status = "off"
        private const val ONGOING = 4200
        private const val ALERT = 4201
    }
    private val worker = Executors.newSingleThreadScheduledExecutor()
    private var transport: ViewingTransport? = null
    private var socket: WebSocket? = null
    private var reconnect: ScheduledFuture<*>? = null
    private var generation = 0
    private var attempts = 0
    private var useAlternate = false
    private var lastMessage = 0L
    private var opened = 0L
    private var snapshot: JSONObject? = null
    private val tracker = AlertTracker()
    private var lastNotice = ""
    private var savedTracker = ""
    private var storageSession = ""
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private fun dispatch(block: () -> Unit) { if (!worker.isShutdown) runCatching { worker.execute(block) } }
    override fun onBind(intent: Intent?): IBinder? = null
    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            manager.createNotificationChannel(NotificationChannel("print-monitor", "Print monitor", NotificationManager.IMPORTANCE_LOW))
            manager.createNotificationChannel(NotificationChannel("print-alerts", "Print finished or needs attention", NotificationManager.IMPORTANCE_HIGH))
        }
    }
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == STOP) { stopMonitoring(); return START_NOT_STICKY }
        if (running) return START_STICKY
        val notification = notice("Connecting to your printer", true)
        if (Build.VERSION.SDK_INT >= 29) startForeground(ONGOING, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        else startForeground(ONGOING, notification)
        running = true; status = "connecting"
        dispatch {
            try {
                val stored = MonitorStorage.read(this) ?: throw IllegalStateException()
                storageSession = stored.getString("session")
                transport = ViewingTransport(stored.getJSONObject("config"))
                printer = transport!!.printer
                stored.optJSONObject("tracker")?.let {
                    tracker.phase = it.optString("phase"); tracker.job = it.optString("job")
                    tracker.armed = it.optBoolean("armed")
                    tracker.errors = it.optJSONArray("errors")?.let { a -> (0 until a.length()).map { i -> a.getString(i) }.toSet() } ?: emptySet()
                }
                connect()
                worker.scheduleAtFixedRate({
                    if (socket != null && (snapshot == null && SystemClock.elapsedRealtime()-opened > 15_000 ||
                            SystemClock.elapsedRealtime()-lastMessage > 65_000)) {
                        fail("reconnecting", true)
                    }
                }, 5, 5, TimeUnit.SECONDS)
                val callback = object: ConnectivityManager.NetworkCallback() {
                    override fun onAvailable(network: Network) { dispatch { attempts=0; useAlternate=false; connect() } }
                    override fun onLost(network: Network) { dispatch { fail("reconnecting", true) } }
                }
                networkCallback = callback
                getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(callback)
            } catch (_: Exception) { fail("configuration", false) }
        }
        return START_STICKY
    }
    private fun connect() {
        val current = ++generation
        reconnect?.cancel(false); reconnect = null
        socket?.cancel(); socket = null; snapshot = null
        val transport = transport ?: return
        val base = if (useAlternate) transport.alternate ?: transport.primary else transport.primary
        val url = transport.url(base, "status")
        opened = SystemClock.elapsedRealtime(); lastMessage = opened
        updateNotice("Connecting to your printer", "connecting")
        socket = transport.client(url).newBuilder().pingInterval(25, TimeUnit.SECONDS).build()
            .newWebSocket(transport.request(url), object: WebSocketListener() {
                override fun onMessage(webSocket: WebSocket, text: String) { dispatch {
                    if (generation != current) return@dispatch
                    if (text.length > 2*1024*1024) { fail("unavailable", true); return@dispatch }
                    try {
                        val message = JSONObject(text); lastMessage = SystemClock.elapsedRealtime()
                        when (message.optString("type")) {
                            "hello" -> if (message.optInt("protocol_version") != 1) fail("incompatible", false)
                            "ping" -> webSocket.send("{\"type\":\"pong\"}")
                            "snapshot" -> { snapshot = message.getJSONObject("data"); consumeSnapshot() }
                            "delta" -> snapshot?.let { merge(it, message.getJSONObject("data")); consumeSnapshot() }
                        }
                    } catch (_: Exception) { fail("unavailable", true) }
                } }
                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { dispatch {
                    if (generation != current) return@dispatch
                    val state = if (response?.code in listOf(401,403)) "auth" else ViewingTransport.failure(t)
                    fail(state, state == "network")
                } }
                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { dispatch {
                    if (generation == current) fail(if (code == 1008) "auth" else "reconnecting", code != 1008)
                } }
            })
    }
    private fun consumeSnapshot() {
        val state = snapshot ?: return
        attempts = 0
        if (state.optJSONObject("session")?.optBoolean("connected") != true) {
            updateNotice("Printer offline · waiting to reconnect", "printer_offline"); return
        }
        val phase = state.optString("phase")
        val job = state.optJSONObject("job")
        val errors = mutableSetOf<String>()
        state.optJSONObject("print_error")?.let { errors.add(it.optString("code", "print_error")) }
        state.optJSONArray("hms")?.let { a -> for (i in 0 until a.length()) {
            val e = a.optJSONObject(i) ?: continue
            if (!e.optBoolean("stale") && e.optString("severity") == "error") errors.add(e.optString("code", "hms"))
        } }
        val jobId = job?.optString("subtask_name", "")?.takeUnless { it == "null" } ?: ""
        val events = tracker.updatePersisted(PrintState(true, phase, jobId, errors)) { candidate ->
            // A failed save must leave the old in-memory transition available for retry.
            val trackerState = JSONObject().put("phase", candidate.phase).put("job", candidate.job)
                .put("armed", candidate.armed).put("errors", JSONArray(candidate.errors.sorted()))
            val encoded = trackerState.toString()
            if (encoded != savedTracker) {
                if (!MonitorStorage.updateTracker(this, storageSession, trackerState)) return@updatePersisted false
                if (!MonitorStorage.isActive(this, storageSession)) return@updatePersisted false
                savedTracker = encoded
            }
            true
        }
        for (event in events) alert(when(event) {
            "finished" -> "Print finished"
            "paused" -> "Print paused · attention needed"
            "failed" -> "Print failed · check your printer"
            else -> "Printer reported an error"
        })
        val percent = job?.optDouble("percent", Double.NaN)?.takeIf { it.isFinite() }?.coerceIn(0.0,100.0)?.toInt()
        val label = when(phase) {
            "printing" -> "Printing${percent?.let { " · $it%" } ?: ""}"
            "preparing" -> "Preparing print"
            "paused" -> "Print paused"
            "finished", "completed" -> "Print finished · watching for the next job"
            "failed" -> "Print failed"
            else -> "Printer connected · watching for prints"
        }
        updateNotice(label, "connected")
    }
    private fun fail(state: String, retry: Boolean) {
        ++generation; socket?.cancel(); socket = null
        reconnect?.cancel(false); reconnect = null
        updateNotice(if (retry) "Connection lost · reconnecting; alerts delayed" else "Print monitor stopped · check connection", state)
        if (!retry) {
            alert(when(state) {
                "auth" -> "Print alerts stopped · access was rejected"
                "identity" -> "Print alerts stopped · bridge identity changed"
                else -> "Print alerts stopped · check the app"
            })
            stopMonitoring(); return
        }
        useAlternate = !useAlternate
        val delay = minOf(30L, 1L shl minOf(attempts++, 5))
        reconnect = worker.schedule({ connect() }, delay, TimeUnit.SECONDS)
    }
    private fun notice(text: String, ongoing: Boolean): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val builder = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, if (ongoing) "print-monitor" else "print-alerts")
            else Notification.Builder(this)
        builder.setSmallIcon(android.R.drawable.stat_notify_sync).setContentTitle("Bambu Bridge")
            .setContentText(text).setStyle(Notification.BigTextStyle().bigText(text))
            .setContentIntent(open).setVisibility(Notification.VISIBILITY_PRIVATE)
            .setOngoing(ongoing).setOnlyAlertOnce(ongoing).setAutoCancel(!ongoing)
        if (ongoing) {
            val stop = PendingIntent.getService(this, 1, Intent(this, PrintMonitorService::class.java).setAction(STOP),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            builder.addAction(Notification.Action.Builder(null, "Stop monitoring", stop).build())
        } else if (Build.VERSION.SDK_INT < 26) builder.setDefaults(Notification.DEFAULT_ALL).setPriority(Notification.PRIORITY_HIGH)
        return builder.build()
    }
    private fun updateNotice(text: String, state: String) {
        status = state
        if (lastNotice != text) { lastNotice = text; getSystemService(NotificationManager::class.java).notify(ONGOING, notice(text,true)) }
    }
    private fun alert(text: String) { getSystemService(NotificationManager::class.java).notify(ALERT, notice(text,false)) }
    private fun stopMonitoring() {
        MonitorStorage.clear(this); running=false
        stopForeground(STOP_FOREGROUND_REMOVE); stopSelf()
    }
    override fun onDestroy() {
        running=false; printer=""; ++generation
        socket?.cancel(); worker.shutdownNow(); transport?.close()
        networkCallback?.let { runCatching { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) } }
        super.onDestroy()
    }
    private fun merge(target: JSONObject, patch: JSONObject) {
        for (key in patch.keys()) {
            val value = patch.get(key)
            if (value is JSONObject && target.opt(key) is JSONObject) merge(target.getJSONObject(key), value)
            else target.put(key, value)
        }
    }
}
