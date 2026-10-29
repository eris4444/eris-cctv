package ir.eris.cctv

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.view.Surface
import ir.eris.cctv.core.LinkHost
import ir.eris.cctv.core.ServerLink
import java.text.SimpleDateFormat
import java.util.ArrayDeque
import java.util.Date
import java.util.Locale

/**
 * Foreground service: keeps the server link up and runs the camera while somebody watches (or all the
 * time with "keep camera on"). A partial wake lock and a Wi-Fi lock keep it working with the screen off.
 */
class CameraService : Service(), LinkHost, Capture.Listener {

    private lateinit var settings: Settings
    private val main = Handler(Looper.getMainLooper())
    private var link: ServerLink? = null
    private var capture: Capture? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private val watchers = HashSet<String>()
    @Volatile private var init: ByteArray? = null
    @Volatile private var codec = ""
    @Volatile private var captureState = "idle"
    @Volatile private var captureError = ""
    private var stopCaptureAt = 0L
    private var retryAt = 0L
    private var preview: Surface? = null

    override val agentId: String get() = settings.agentId
    override val agentName: String get() = settings.systemName
    override val version: String get() = "android-" + BuildInfo.VERSION

    override fun onBind(intent: android.content.Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        codec = settings.lastCodec
        instance = this
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            settings.running = false
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            goForeground()
        } catch (e: Exception) {
            // e.g. Android 14+ refusing a camera service started from the background (after a reboot)
            log("could not start in the background: ${e.message} - open the app once")
            BootReceiver.askToStart(this)
            stopSelf()
            return START_NOT_STICKY
        }
        acquireLocks()
        if (link == null && settings.configured) {
            link = ServerLink(settings.server(), this).also { it.start() }
        }
        settings.running = true
        if (settings.keepCameraOn) ensureCapture()
        main.removeCallbacks(tick)
        main.post(tick)
        log("service started")
        return START_STICKY
    }

    override fun onDestroy() {
        main.removeCallbacks(tick)
        link?.stop()
        link = null
        stopCapture()
        try { wakeLock?.release() } catch (_: Exception) { }
        try { wifiLock?.release() } catch (_: Exception) { }
        instance = null
        log("service stopped")
        super.onDestroy()
    }

    // ---------------------------------------------------------------------------- notification
    private fun goForeground() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Camera service", NotificationManager.IMPORTANCE_LOW))
        }
        val n = notification()
        if (Build.VERSION.SDK_INT >= 29) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
            if (settings.audio && granted(Manifest.permission.RECORD_AUDIO)) type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            startForeground(NOTIFICATION_ID, n, type)
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
    }

    private fun notification(): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 1, Intent(this, CameraService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        @Suppress("DEPRECATION")
        val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL) else Notification.Builder(this)
        return b.setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Eris CCTV camera")
            .setContentText(statusLine())
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }

    private fun statusLine(): String {
        val l = link
        val server = when {
            l == null -> "no server set up"
            l.state == "connected" -> (if (l.secure) "connected (encrypted)" else "connected")
            l.state == "error" -> "server: " + l.error.take(60)
            else -> "connecting to the server..."
        }
        val cam = when {
            watchers.isNotEmpty() -> "streaming to ${watchers.size} viewer(s)"
            captureState == "running" -> "camera on"
            else -> "camera ready"
        }
        return "$server - $cam"
    }

    private var shownLine = ""

    private fun refreshNotification() {
        val line = statusLine()
        if (line == shownLine) return
        shownLine = line
        try {
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).notify(NOTIFICATION_ID, notification())
        } catch (_: Exception) { }
    }

    @Suppress("DEPRECATION")
    private fun acquireLocks() {
        if (wakeLock == null) {
            wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ErisCCTV:camera").apply { setReferenceCounted(false); acquire() }
        }
        if (wifiLock == null) {
            try {
                wifiLock = (applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
                    .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "ErisCCTV:wifi").apply { setReferenceCounted(false); acquire() }
            } catch (_: Exception) { }
        }
    }

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    // ---------------------------------------------------------------------------- camera control
    private val tick = object : Runnable {
        override fun run() {
            // anything thrown here would stop the whole app, camera included: log it and keep going
            try {
                val now = System.currentTimeMillis()
                val wanted = watchers.isNotEmpty() || settings.keepCameraOn || preview != null
                if (wanted && capture == null && now >= retryAt) ensureCapture()
                if (!wanted && capture != null && stopCaptureAt in 1..now) {
                    log("nobody is watching - camera off")
                    stopCapture()
                }
                if (wanted) stopCaptureAt = 0
                else if (capture != null && stopCaptureAt == 0L) stopCaptureAt = now + IDLE_STOP_MS
                refreshNotification()
            } catch (e: Throwable) {
                log("service: $e")
                try { capture?.stop() } catch (_: Throwable) { }
                capture = null
                retryAt = System.currentTimeMillis() + 10_000
            }
            main.postDelayed(this, 1000)
        }
    }

    /** Runs on the main thread; an error is logged instead of stopping the app (and the camera). */
    private fun safe(block: () -> Unit) {
        main.post(Runnable {
            try { block() } catch (e: Throwable) { log("service: $e") }
        })
    }

    private fun ensureCapture() {
        if (capture != null) return
        if (!granted(Manifest.permission.CAMERA)) {
            captureState = "error"
            captureError = "camera permission is missing - open the app"
            return
        }
        val c = Capture(this, Capture.Options(settings.backCamera, settings.width, settings.height, settings.fps,
            settings.bitrate, settings.audio && granted(Manifest.permission.RECORD_AUDIO), settings.rotation,
            settings.displayRotation), this)
        capture = c
        captureState = "starting"
        captureError = ""
        c.start()
        preview?.let { c.setPreview(it) }
        log("camera on (${settings.width}x${settings.height} @ ${settings.fps} fps)")
    }

    private fun stopCapture() {
        capture?.stop()
        capture = null
        init = null
        captureState = "idle"
        stopCaptureAt = 0
        link?.camerasChanged()
    }

    /** Rotation of the running camera's picture (0 until it runs). */
    val pictureRotation: Int get() = capture?.rotation ?: 0

    fun setPreview(surface: Surface?) {
        safe {
            preview = surface
            if (surface != null) ensureCapture()
            capture?.setPreview(surface)
        }
    }

    // Capture.Listener (camera threads)
    override fun onInit(data: ByteArray, codec: String, width: Int, height: Int) {
        this.codec = codec
        if (settings.lastCodec != codec) settings.lastCodec = codec
        init = data
        val l = link ?: return
        l.camerasChangedNow()   // the server must know the codec before the viewers get the init segment
        l.onInit(SLOT, data)
    }

    override fun onFragment(data: ByteArray, key: Boolean) {
        link?.onFrag(SLOT, data, key)
    }

    override fun onCaptureState(state: String, error: String) {
        safe {
            captureState = state
            captureError = error
            if (state == "error") {
                log("camera: $error")
                capture?.stop()
                capture = null
                init = null
                retryAt = System.currentTimeMillis() + 10_000
            }
            link?.camerasChanged()
        }
    }

    // ---------------------------------------------------------------------------- LinkHost (link thread)
    override fun camerasJson(): List<Map<String, Any?>> = listOf(mapOf(
        "id" to "phone", "slot" to SLOT, "name" to settings.cameraName,
        "online" to (captureState != "error" && granted(Manifest.permission.CAMERA)),
        "state" to (if (captureState == "error") "error" else "running"), "error" to captureError,
        "codec" to codec, "audio" to codec.contains("mp4a"), "width" to (capture?.width ?: settings.width),
        "height" to (capture?.height ?: settings.height), "fps" to settings.fps, "enabled" to true, "recording" to false))

    override fun initFor(slot: Int): ByteArray? = init

    override fun onSubscribe(link: ServerLink, slot: Int) {
        safe {
            watchers.add("${link.cfg.address}#$slot")
            ensureCapture()
            capture?.requestKeyFrame()
            log("a viewer opened the camera")
        }
    }

    override fun onUnsubscribe(link: ServerLink, slot: Int) {
        safe { watchers.remove("${link.cfg.address}#$slot") }
    }

    override fun onPinChanged(link: ServerLink, pin: String) {
        settings.tlsPin = pin
    }

    override fun onLinkState(link: ServerLink) {
        safe { refreshNotification() }
    }

    override fun log(message: String) = Companion.log(message)

    fun status(): Map<String, Any?> {
        val l = link
        return mapOf("server" to (l?.state ?: "none"), "error" to (l?.error ?: ""), "secure" to (l?.secure == true),
            "server_name" to (l?.serverName ?: ""), "sent" to (l?.bytesSent ?: 0L), "dropped" to (l?.dropped ?: 0),
            "watchers" to watchers.size, "camera" to captureState, "camera_error" to captureError,
            "size" to (capture?.let { "${it.width}x${it.height}" } ?: ""), "frames" to (capture?.frames ?: 0L))
    }

    companion object {
        const val ACTION_STOP = "ir.eris.cctv.STOP"
        private const val CHANNEL = "camera"
        private const val NOTIFICATION_ID = 7
        private const val SLOT = 0
        private const val IDLE_STOP_MS = 20_000L
        @Volatile var instance: CameraService? = null
            private set
        val logLines = ArrayDeque<String>()

        fun log(message: String) {
            val line = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date()) + "  " + message
            synchronized(logLines) {
                logLines.addLast(line)
                while (logLines.size > 200) logLines.removeFirst()
            }
        }

        fun start(context: Context) {
            val i = Intent(context, CameraService::class.java)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(i) else context.startService(i)
        }

        fun stop(context: Context) {
            Settings(context).running = false
            context.stopService(Intent(context, CameraService::class.java))
        }
    }
}
