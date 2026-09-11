package com.thereprocase.bambubridge.viewing

import android.Manifest
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import android.view.WindowManager
import com.facebook.react.bridge.*
import org.json.JSONObject

class ViewingModule(private val context: ReactApplicationContext): ReactContextBaseJavaModule(context), LifecycleEventListener {
    private var awake = false
    init { context.addLifecycleEventListener(this) }
    override fun getName() = "BridgeViewing"
    @ReactMethod fun screenOptions(rotate: Boolean, keepAwake: Boolean) {
        awake = keepAwake
        context.currentActivity?.runOnUiThread {
            context.currentActivity?.requestedOrientation = if (rotate) ActivityInfo.SCREEN_ORIENTATION_FULL_USER else ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
            applyAwake(keepAwake)
        }
    }
    private fun applyAwake(value: Boolean) {
        context.currentActivity?.window?.let {
            if (value) it.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            else it.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }
    @ReactMethod fun startMonitor(encoded: String, promise: Promise) {
        try {
            require(context.currentActivity != null && encoded.length < 16384)
            require(context.getSystemService(NotificationManager::class.java).areNotificationsEnabled())
            if (Build.VERSION.SDK_INT >= 33) require(context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
            if (PrintMonitorService.running) { promise.reject("MONITOR_RUNNING", "Stop the current monitor before choosing another printer."); return }
            val config = JSONObject(encoded)
            ViewingTransport(config).close() // Validate all routes before persisting credentials.
            MonitorStorage.write(context, JSONObject().put("config", config).put("session", java.util.UUID.randomUUID().toString()))
            val intent = Intent(context, PrintMonitorService::class.java)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent) else context.startService(intent)
            promise.resolve(null)
        } catch (_: Exception) {
            MonitorStorage.clear(context)
            promise.reject("MONITOR_START", "Couldn't start print alerts. Allow notifications and check the bridge connection.")
        }
    }
    @ReactMethod fun stopMonitor(promise: Promise) {
        try {
            MonitorStorage.clear(context)
            context.stopService(Intent(context, PrintMonitorService::class.java))
            promise.resolve(null)
        } catch (_: Exception) { promise.reject("MONITOR_STOP", "Couldn't stop monitoring. Try again.") }
    }
    @ReactMethod fun monitorStatus(promise: Promise) {
        val power = context.getSystemService(PowerManager::class.java)
        promise.resolve(Arguments.createMap().apply {
            putBoolean("running", PrintMonitorService.running)
            putString("printer", PrintMonitorService.printer)
            putString("state", PrintMonitorService.status)
            putBoolean("alertsAllowed", context.getSystemService(NotificationManager::class.java).areNotificationsEnabled())
            putBoolean("batteryRestricted", !power.isIgnoringBatteryOptimizations(context.packageName))
        })
    }
    @ReactMethod fun batterySettings() {
        context.currentActivity?.let { activity ->
            runCatching { activity.startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:${context.packageName}"))) }
                .onFailure { runCatching { activity.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) } }
        }
    }
    override fun onHostResume() { context.currentActivity?.runOnUiThread { applyAwake(awake) } }
    override fun onHostPause() { context.currentActivity?.runOnUiThread { applyAwake(false) } }
    override fun onHostDestroy() {}
    override fun invalidate() { context.removeLifecycleEventListener(this); super.invalidate() }
}
