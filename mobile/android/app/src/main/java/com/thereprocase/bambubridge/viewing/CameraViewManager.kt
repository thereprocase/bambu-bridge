package com.thereprocase.bambubridge.viewing

import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.os.SystemClock
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.widget.FrameLayout
import android.widget.ImageView
import com.facebook.react.bridge.*
import com.facebook.react.uimanager.*
import com.facebook.react.uimanager.annotations.ReactProp
import com.facebook.react.uimanager.events.Event
import okhttp3.Call
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class CameraViewManager : SimpleViewManager<CameraView>() {
    override fun getName() = "BridgeCameraView"
    override fun createViewInstance(context: ThemedReactContext) = CameraView(context)
    @ReactProp(name="source") fun source(view: CameraView, value: String?) = view.configure(value)
    @ReactProp(name="zoomEnabled") fun zoom(view: CameraView, value: Boolean) { view.zoomEnabled = value }
    override fun getExportedCustomDirectEventTypeConstants(): MutableMap<String, Any> = mutableMapOf(
        "topCameraState" to mapOf("registrationName" to "onState"))
    override fun onDropViewInstance(view: CameraView) { view.dispose(); super.onDropViewInstance(view) }
}

class CameraView(private val react: ThemedReactContext) : FrameLayout(react), LifecycleEventListener {
    private val picture = ImageView(react).apply { scaleType = ImageView.ScaleType.FIT_CENTER }
    private val worker = Executors.newSingleThreadExecutor()
    private var source: String? = null
    @Volatile private var call: Call? = null
    @Volatile private var generation = 0
    private var active = true
    var zoomEnabled = false
    private var zoom = 1f
    private val scale = ScaleGestureDetector(react, object: ScaleGestureDetector.SimpleOnScaleGestureListener() {
        override fun onScale(detector: ScaleGestureDetector): Boolean {
            zoom = (zoom * detector.scaleFactor).coerceIn(1f, 5f); transform(); return true
        }
    })
    private val gesture = GestureDetector(react, object: GestureDetector.SimpleOnGestureListener() {
        override fun onDown(e: MotionEvent) = true
        override fun onDoubleTap(e: MotionEvent): Boolean {
            zoom = if (zoom > 1f) 1f else 2f; transform(); return true
        }
        override fun onScroll(a: MotionEvent?, b: MotionEvent, dx: Float, dy: Float): Boolean {
            picture.translationX -= dx; picture.translationY -= dy; transform(); return true
        }
    })
    init {
        setBackgroundColor(0xff111113.toInt()); clipChildren = true
        addView(picture, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
        contentDescription = "Live printer camera. Pinch to zoom; double tap to reset."
        react.addLifecycleEventListener(this)
    }
    private fun transform() {
        picture.scaleX = zoom; picture.scaleY = zoom
        picture.translationX = picture.translationX.coerceIn(-width*(zoom-1)/2, width*(zoom-1)/2)
        picture.translationY = picture.translationY.coerceIn(-height*(zoom-1)/2, height*(zoom-1)/2)
    }
    override fun dispatchDraw(canvas: Canvas) {
        val saved = canvas.save()
        canvas.clipRect(0, 0, width, height)
        super.dispatchDraw(canvas)
        canvas.restoreToCount(saved)
    }
    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (!zoomEnabled) return super.onTouchEvent(event)
        parent?.requestDisallowInterceptTouchEvent(true)
        scale.onTouchEvent(event); gesture.onTouchEvent(event)
        if (event.action == MotionEvent.ACTION_UP) performClick()
        return true
    }
    override fun performClick(): Boolean { super.performClick(); return true }
    fun configure(value: String?) { if (source == value) return; source = value; restart() }
    private fun restart() {
        val current = ++generation
        call?.cancel(); call = null
        val encoded = source ?: return
        if (!active) return
        emit("connecting")
        worker.execute {
            var transport: ViewingTransport? = null
            try {
                transport = ViewingTransport(JSONObject(encoded))
                val url = transport.url(transport.primary, "camera/stream.mjpeg")
                val next = transport.client(url).newCall(transport.request(url))
                if (generation != current) return@execute
                call = next
                next.execute().use { response ->
                    if (generation != current) return@use
                    if (!response.isSuccessful) {
                        emit(if (response.code in listOf(401,403)) "auth" else "unavailable", current)
                        return@use
                    }
                    require(response.header("Content-Type")?.startsWith("multipart/x-mixed-replace", true) == true)
                    val reader = MjpegReader(response.body!!.source())
                    val pending = AtomicBoolean(false)
                    val arrivals = ArrayDeque<Long>()
                    while (generation == current) {
                        val bytes = reader.next()
                        val now = SystemClock.elapsedRealtime()
                        arrivals.addLast(now); while (arrivals.size > 12) arrivals.removeFirst()
                        val fps = if (arrivals.size > 1 && now > arrivals.first())
                            (arrivals.size - 1) * 1000.0 / (now - arrivals.first()) else 0.0
                        // Bound UI work at one decoded pending frame; drop old frames if the device is busy.
                        if (!pending.compareAndSet(false, true)) continue
                        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
                        require(bounds.outWidth in 1..8192 && bounds.outHeight in 1..8192
                            && bounds.outWidth.toLong()*bounds.outHeight <= 16_000_000)
                        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                            ?: throw IllegalArgumentException("Invalid JPEG")
                        post {
                            if (generation == current) {
                                picture.setImageBitmap(bitmap)
                                emit("frame", current, fps, bitmap.width, bitmap.height)
                            } else bitmap.recycle()
                            pending.set(false)
                        }
                    }
                }
            } catch (e: Exception) { if (generation == current) emit(ViewingTransport.failure(e), current) }
            finally { transport?.close() }
        }
    }
    private fun emit(state: String, current: Int = generation, fps: Double = 0.0, width: Int = 0, height: Int = 0) {
        post {
            if (current != generation) return@post
            UIManagerHelper.getEventDispatcherForReactTag(react, id)?.dispatchEvent(CameraEvent(
                UIManagerHelper.getSurfaceId(this), id, Arguments.createMap().apply {
                    putString("state",state); putDouble("fps",fps); putInt("width",width); putInt("height",height)
                }))
        }
    }
    override fun onHostPause() { active = false; ++generation; call?.cancel() }
    override fun onHostResume() { active = true; restart() }
    override fun onHostDestroy() { dispose() }
    fun dispose() {
        ++generation; call?.cancel(); worker.shutdownNow(); react.removeLifecycleEventListener(this)
        picture.setImageDrawable(null)
    }
}
private class CameraEvent(surface: Int, tag: Int, private val data: WritableMap): Event<CameraEvent>(surface, tag) {
    override fun getEventName() = "topCameraState"
    override fun getEventData() = data
}
