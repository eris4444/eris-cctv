package ir.eris.cctv

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.SurfaceTexture
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.text.InputType
import android.view.Gravity
import android.view.Surface
import android.view.TextureView
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import ir.eris.cctv.core.ServerLink
import ir.eris.cctv.core.StatusText
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var settings: Settings
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var statusView: TextView
    private lateinit var startButton: Button
    private lateinit var previewButton: Button
    private lateinit var previewView: TextureView
    private lateinit var logView: TextView
    private var previewOn = false

    // settings form
    private lateinit var address: EditText
    private lateinit var port: EditText
    private lateinit var username: EditText
    private lateinit var password: EditText
    private lateinit var encryption: Spinner
    private lateinit var systemName: EditText
    private lateinit var cameraName: EditText
    private lateinit var cameraSide: Spinner
    private lateinit var resolution: Spinner
    private lateinit var fps: Spinner
    private lateinit var quality: Spinner
    private lateinit var rotation: Spinner
    private lateinit var audio: CheckBox
    private lateinit var keepOn: CheckBox
    private lateinit var boot: CheckBox

    private val sizes = listOf(640 to 480, 1280 to 720, 1920 to 1080)
    private val fpsValues = listOf(10, 15, 20, 25, 30)
    private val qualities = listOf("low", "medium", "high")
    private val rotations = listOf(-1, 0, 90, 180, 270)
    private val encModes = listOf("auto", "on", "off")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        window.statusBarColor = BG
        window.navigationBarColor = BG
        setContentView(build())
        load()
        showLastCrash()
        if (settings.running && settings.configured && CameraService.instance == null && hasCamera()) {
            CameraService.start(this)
        }
    }

    override fun onResume() {
        super.onResume()
        ui.post(refresh)
    }

    override fun onPause() {
        ui.removeCallbacks(refresh)
        super.onPause()
    }

    override fun onDestroy() {
        CameraService.instance?.setPreview(null)
        super.onDestroy()
    }

    // ---------------------------------------------------------------------------- layout
    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun build(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(16), dp(16), dp(32))
            setBackgroundColor(BG)
        }
        root.addView(TextView(this).apply {
            text = "Eris CCTV camera"
            textSize = 22f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(FG)
        })
        root.addView(label("Turns this phone into a camera for your Eris CCTV server - version " + BuildInfo.VERSION))

        statusView = TextView(this).apply {
            setTextColor(FG)
            textSize = 14f
            setLineSpacing(0f, 1.25f)
        }
        root.addView(card(statusView))

        startButton = button("Start camera", primary = true) { toggleService() }
        root.addView(startButton, full())
        root.addView(row(
            button("Dark screen") { startActivity(Intent(this, DarkActivity::class.java)) },
            button("Turn screen off") { screenOff() }))
        previewButton = button("Show preview") { togglePreview() }
        root.addView(previewButton, full())
        previewView = TextureView(this).apply { visibility = View.GONE }
        root.addView(previewView, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(220)).apply { topMargin = dp(8) })

        root.addView(section("Server"))
        address = edit("IP or domain, e.g. panel.example.com")
        port = edit("8800", InputType.TYPE_CLASS_NUMBER)
        username = edit("camera account username")
        password = edit("password", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        encryption = spinner(listOf("Automatic (TLS when the server offers it)", "Always TLS", "Off"))
        root.addView(field("Server address", address))
        root.addView(field("Port", port))
        root.addView(field("Username", username))
        root.addView(field("Password", password))
        root.addView(field("Encryption", encryption))
        root.addView(row(button("Test connection") { test() }, button("Forget certificate") { forgetPin() }))

        root.addView(section("Camera"))
        systemName = edit("name shown on the server")
        cameraName = edit("camera name")
        cameraSide = spinner(listOf("Back camera", "Front camera"))
        resolution = spinner(sizes.map { "${it.first} x ${it.second}" })
        fps = spinner(fpsValues.map { "$it fps" })
        quality = spinner(listOf("Low (saves data)", "Medium", "High"))
        rotation = spinner(listOf("Automatic (as the phone is held)", "0", "90", "180", "270"))
        audio = check("Send the microphone's sound")
        keepOn = check("Keep the camera on all the time (faster start, more battery and heat)")
        boot = check("Start after the phone restarts")
        root.addView(field("This phone's name", systemName))
        root.addView(field("Camera name", cameraName))
        root.addView(field("Camera", cameraSide))
        root.addView(field("Resolution", resolution))
        root.addView(field("Frame rate", fps))
        root.addView(field("Quality", quality))
        root.addView(field("Rotate picture", rotation))
        root.addView(audio)
        root.addView(keepOn)
        root.addView(boot)
        root.addView(button("Save", primary = true) { save() }, full())

        root.addView(section("Running 24/7"))
        root.addView(label("Keep the phone on its charger. The camera keeps working with the screen off; " +
            "allow the app to run in the background so the phone does not stop it."))
        root.addView(button("Allow running in the background") { batteryExemption() }, full())

        root.addView(section("Log"))
        logView = TextView(this).apply {
            setTextColor(MUTED)
            textSize = 11f
            typeface = Typeface.MONOSPACE
        }
        root.addView(card(logView))
        root.addView(button("Copy log (to send it for help)") { copyLog() }, full())
        return ScrollView(this).apply {
            setBackgroundColor(BG)
            addView(root)
        }
    }

    private fun full() = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        .apply { topMargin = dp(8) }

    private fun label(t: String) = TextView(this).apply {
        text = t
        setTextColor(MUTED)
        textSize = 13f
        setPadding(0, dp(4), 0, dp(4))
    }

    private fun section(t: String) = TextView(this).apply {
        text = t.uppercase()
        setTextColor(ACCENT)
        textSize = 12f
        setTypeface(typeface, Typeface.BOLD)
        letterSpacing = 0.08f
        setPadding(0, dp(24), 0, dp(4))
    }

    private fun card(v: View) = LinearLayout(this).apply {
        background = GradientDrawable().apply { setColor(CARD); cornerRadius = dp(12).toFloat() }
        setPadding(dp(14), dp(12), dp(14), dp(12))
        addView(v)
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = dp(12) }
    }

    private fun button(t: String, primary: Boolean = false, onClick: () -> Unit) = Button(this).apply {
        text = t
        isAllCaps = false
        setTextColor(if (primary) Color.BLACK else FG)
        background = GradientDrawable().apply {
            setColor(if (primary) ACCENT else CARD)
            cornerRadius = dp(10).toFloat()
        }
        setOnClickListener { onClick() }
    }

    private fun row(vararg views: View) = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        for ((i, v) in views.withIndex()) {
            addView(v, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply {
                if (i > 0) leftMargin = dp(8)
            })
        }
        layoutParams = full()
    }

    private fun edit(hint: String, type: Int = InputType.TYPE_CLASS_TEXT) = EditText(this).apply {
        this.hint = hint
        inputType = type
        setTextColor(FG)
        setHintTextColor(MUTED)
        isSingleLine = true
    }

    private fun spinner(items: List<String>) = Spinner(this).apply {
        adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, items)
    }

    private fun check(t: String) = CheckBox(this).apply {
        text = t
        setTextColor(FG)
    }

    private fun field(name: String, v: View) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(0, dp(6), 0, 0)
        addView(TextView(this@MainActivity).apply { text = name; setTextColor(MUTED); textSize = 12f })
        addView(v, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
    }

    // ---------------------------------------------------------------------------- settings
    private fun load() {
        address.setText(settings.address)
        port.setText(settings.port.toString())
        username.setText(settings.username)
        password.setText(settings.password)
        encryption.setSelection(encModes.indexOf(settings.tlsMode).coerceAtLeast(0))
        systemName.setText(settings.systemName)
        cameraName.setText(settings.cameraName)
        cameraSide.setSelection(if (settings.backCamera) 0 else 1)
        resolution.setSelection(sizes.indexOfFirst { it.first == settings.width && it.second == settings.height }.let { if (it < 0) 1 else it })
        fps.setSelection(fpsValues.indexOf(settings.fps).let { if (it < 0) 1 else it })
        quality.setSelection(qualities.indexOf(settings.quality).coerceAtLeast(0))
        rotation.setSelection(rotations.indexOf(settings.rotation).coerceAtLeast(0))
        audio.isChecked = settings.audio
        keepOn.isChecked = settings.keepCameraOn
        boot.isChecked = settings.startOnBoot
    }

    /** Copies the form into Settings. Returns an error text, or null when all is fine. */
    private fun store(): String? {
        var addr = Settings.cleanAddress(address.text.toString())
        var p = port.text.toString().trim().toIntOrNull() ?: 0
        if (addr.count { it == ':' } == 1) {
            addr.substringAfter(':').toIntOrNull()?.let { p = it }
            addr = addr.substringBefore(':')
        }
        if (addr.isEmpty()) return "Enter the server address"
        if (p !in 1..65535) return "Enter the server port"
        if (username.text.isBlank() || password.text.isEmpty()) return "Enter the camera account username and password"
        val changedServer = addr != settings.address || p != settings.port
        settings.address = addr
        settings.port = p
        if (changedServer) settings.tlsPin = ""
        settings.username = username.text.toString()
        settings.password = password.text.toString()
        settings.tlsMode = encModes[encryption.selectedItemPosition]
        settings.systemName = systemName.text.toString().ifBlank { settings.systemName }
        settings.cameraName = cameraName.text.toString().ifBlank { "Phone camera" }
        settings.backCamera = cameraSide.selectedItemPosition == 0
        sizes[resolution.selectedItemPosition].let { settings.width = it.first; settings.height = it.second }
        settings.fps = fpsValues[fps.selectedItemPosition]
        settings.quality = qualities[quality.selectedItemPosition]
        settings.rotation = rotations[rotation.selectedItemPosition]
        settings.audio = audio.isChecked
        settings.keepCameraOn = keepOn.isChecked
        settings.startOnBoot = boot.isChecked
        settings.displayRotation = displayDegrees()
        address.setText(addr)
        port.setText(p.toString())
        return null
    }

    @Suppress("DEPRECATION")
    private fun displayDegrees(): Int = when (windowManager.defaultDisplay.rotation) {
        Surface.ROTATION_90 -> 90
        Surface.ROTATION_180 -> 180
        Surface.ROTATION_270 -> 270
        else -> 0
    }

    private fun save() {
        val err = store()
        if (err != null) { toast(err); return }
        toast("Saved")
        if (CameraService.instance != null) {
            CameraService.stop(this)
            ui.postDelayed({ startService() }, 800)
        }
    }

    // ---------------------------------------------------------------------------- actions
    private fun hasCamera() = checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    private fun toggleService() {
        if (CameraService.instance != null) {
            if (previewOn) togglePreview()
            CameraService.stop(this)
            toast("Camera stopped")
            return
        }
        val err = store()
        if (err != null) { toast(err); return }
        startService()
    }

    private fun startService() {
        val need = mutableListOf(Manifest.permission.CAMERA)
        if (settings.audio) need.add(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) need.add(Manifest.permission.POST_NOTIFICATIONS)
        val missing = need.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) {
            requestPermissions(missing.toTypedArray(), REQ_START)
            return
        }
        settings.displayRotation = displayDegrees()
        CameraService.start(this)
        toast("Camera started")
        maybeAskBattery()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_START) return
        if (!hasCamera()) {
            toast("The camera permission is needed")
            return
        }
        settings.displayRotation = displayDegrees()
        CameraService.start(this)
        maybeAskBattery()
    }

    private fun togglePreview() {
        val svc = CameraService.instance
        if (!previewOn) {
            if (svc == null) { toast("Start the camera first"); return }
            previewOn = true
            previewView.visibility = View.VISIBLE
            previewButton.text = "Hide preview"
            val attach = { st: SurfaceTexture ->
                st.setDefaultBufferSize(settings.width, settings.height)
                CameraService.instance?.setPreview(Surface(st))
                ui.postDelayed({ fitPreview() }, 1500)   // the rotation is known once the camera runs
            }
            val st = previewView.surfaceTexture
            if (st != null) attach(st) else previewView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
                override fun onSurfaceTextureAvailable(s: SurfaceTexture, w: Int, h: Int) { if (previewOn) attach(s) }
                override fun onSurfaceTextureSizeChanged(s: SurfaceTexture, w: Int, h: Int) {}
                override fun onSurfaceTextureDestroyed(s: SurfaceTexture): Boolean {
                    CameraService.instance?.setPreview(null)
                    return true
                }
                override fun onSurfaceTextureUpdated(s: SurfaceTexture) {}
            }
        } else {
            previewOn = false
            CameraService.instance?.setPreview(null)
            previewView.visibility = View.GONE
            previewButton.text = "Show preview"
        }
    }

    /** Shows the camera picture upright and undistorted inside the preview box (same rotation as the stream). */
    private fun fitPreview() {
        if (!previewOn) return
        val deg = CameraService.instance?.pictureRotation ?: 0
        val vw = previewView.width.toFloat()
        val vh = previewView.height.toFloat()
        if (vw <= 0 || vh <= 0) return
        val sideways = deg % 180 == 90
        val aspect = if (sideways) settings.height.toFloat() / settings.width else settings.width.toFloat() / settings.height
        var tw = vw
        var th = vw / aspect
        if (th > vh) { th = vh; tw = vh * aspect }
        val m = android.graphics.Matrix()
        if (sideways) m.setScale(th / vw, tw / vh, vw / 2, vh / 2) else m.setScale(tw / vw, th / vh, vw / 2, vh / 2)
        m.postRotate(deg.toFloat(), vw / 2, vh / 2)
        previewView.setTransform(m)
    }

    private fun screenOff() {
        val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
        val admin = ComponentName(this, AdminReceiver::class.java)
        if (dpm.isAdminActive(admin)) {
            if (previewOn) togglePreview()
            dpm.lockNow()
        } else {
            AlertDialog.Builder(this)
                .setTitle("Turn the screen off")
                .setMessage("To switch the screen off from this button, Android needs this app to be allowed to " +
                    "lock the screen (nothing else). You can also simply press the power button - the camera " +
                    "keeps working.")
                .setPositiveButton("Allow") { _, _ ->
                    startActivity(Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN)
                        .putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, admin)
                        .putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION, "Lets the camera app switch the screen off."))
                }
                .setNegativeButton("Use dark screen") { _, _ -> startActivity(Intent(this, DarkActivity::class.java)) }
                .show()
        }
    }

    private fun maybeAskBattery() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (!pm.isIgnoringBatteryOptimizations(packageName)) batteryExemption()
    }

    private fun batteryExemption() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            toast("Already allowed to run in the background")
            return
        }
        try {
            startActivity(Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName")))
        } catch (_: Exception) {
            try {
                startActivity(Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            } catch (_: Exception) {
                toast("Open the phone's battery settings and set Eris CCTV to \"No restrictions\"")
            }
        }
    }

    private fun test() {
        val err = store()
        if (err != null) { toast(err); return }
        toast("Testing...")
        val cfg = settings.server()
        val host = object : ir.eris.cctv.core.LinkHost {
            override val agentId = settings.agentId
            override val agentName = settings.systemName
            override val version = "android-" + BuildInfo.VERSION
            override fun camerasJson(): List<Map<String, Any?>> = emptyList()
            override fun initFor(slot: Int): ByteArray? = null
            override fun onSubscribe(link: ServerLink, slot: Int) {}
            override fun onUnsubscribe(link: ServerLink, slot: Int) {}
            override fun onPinChanged(link: ServerLink, pin: String) {}
            override fun onLinkState(link: ServerLink) {}
            override fun log(message: String) = CameraService.log(message)
        }
        thread {
            val r = ServerLink.test(cfg, host)
            ui.post {
                if (isFinishing || isDestroyed) return@post   // the screen was left meanwhile
                val msg = if (r["ok"] == true) {
                    "Connected to \"${r["server_name"]}\" - " + (if (r["secure"] == true) "encrypted (TLS)"
                    else if (r["server_tls"] == true) "NOT encrypted" else "NOT encrypted: the server has no TLS (older than 1.1)")
                } else "Failed: ${r["error"]}"
                AlertDialog.Builder(this).setTitle("Connection test").setMessage(msg).setPositiveButton("OK", null).show()
            }
        }
    }

    private fun forgetPin() {
        settings.tlsPin = ""
        toast("The next connection trusts the server's current certificate")
        if (CameraService.instance != null) {
            CameraService.stop(this)
            ui.postDelayed({ startService() }, 800)
        }
    }

    private fun toast(t: String) = Toast.makeText(this, t, Toast.LENGTH_SHORT).show()

    private fun copy(label: String, text: String) {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as android.content.ClipboardManager
        cm.setPrimaryClip(android.content.ClipData.newPlainText(label, text))
        toast("Copied")
    }

    /** If the app stopped unexpectedly last time, show why (saved by App) so it can be reported. */
    private fun showLastCrash() {
        try {
            val f = java.io.File(filesDir, App.CRASH_FILE)
            if (!f.exists()) return
            val text = f.readText()
            f.renameTo(java.io.File(filesDir, App.SEEN_FILE))
            CameraService.log("the app stopped unexpectedly last time - see the report")
            AlertDialog.Builder(this)
                .setTitle("The app stopped last time")
                .setMessage("Please send this report (Copy) so it can be fixed. The camera itself keeps working.\n\n" +
                    text.take(3000))
                .setPositiveButton("Copy") { _, _ -> copy("Eris CCTV crash", text) }
                .setNegativeButton("Close", null)
                .show()
        } catch (_: Exception) { }
    }

    private fun copyLog() {
        val lines = synchronized(CameraService.logLines) { CameraService.logLines.toList() }
        val status = CameraService.instance?.status()?.toString() ?: "service stopped"
        val crash = try { java.io.File(filesDir, App.SEEN_FILE).takeIf { it.exists() }?.readText() } catch (_: Exception) { null }
        copy("Eris CCTV log", "Eris CCTV camera ${BuildInfo.VERSION} - Android ${Build.VERSION.RELEASE} (API " +
            "${Build.VERSION.SDK_INT}) - ${Build.MANUFACTURER} ${Build.MODEL}\nstatus: $status\n\n" +
            lines.joinToString("\n") + (if (crash != null) "\n\nlast crash:\n$crash" else ""))
    }

    /** "Battery 86%, charging, 34 C" - worth watching on a phone that films all day. */
    private fun battery(): String {
        return try {
            val b = registerReceiver(null, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED)) ?: return ""
            val level = b.getIntExtra(android.os.BatteryManager.EXTRA_LEVEL, -1)
            val scale = b.getIntExtra(android.os.BatteryManager.EXTRA_SCALE, 100)
            val plugged = b.getIntExtra(android.os.BatteryManager.EXTRA_PLUGGED, 0) != 0
            val temp = b.getIntExtra(android.os.BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0
            StatusText.battery(level * 100 / maxOf(scale, 1), plugged, temp)
        } catch (e: Exception) {
            ""
        }
    }

    // ---------------------------------------------------------------------------- status
    private val refresh = object : Runnable {
        override fun run() {
            // a mistake in a status line must never take the app (and with it the camera service) down
            try {
                val svc = CameraService.instance
                if (svc == null) {
                    statusView.text = if (settings.configured) "Stopped - press Start camera." else
                        "Enter the server details below (the camera account from the server setup), then press Start camera."
                    startButton.text = "Start camera"
                } else {
                    statusView.text = StatusText.service(svc.status(), settings.address, settings.port, settings.fps, battery())
                    startButton.text = "Stop camera"
                }
                val lines = synchronized(CameraService.logLines) { CameraService.logLines.toList() }
                logView.text = lines.takeLast(12).reversed().joinToString("\n")
            } catch (e: Throwable) {
                statusView.text = "Could not show the status: $e"
                CameraService.log("status screen: $e")
            }
            ui.postDelayed(this, 1000)
        }
    }

    companion object {
        private const val REQ_START = 1
        val BG = Color.parseColor("#0f1216")
        val CARD = Color.parseColor("#1b2027")
        val FG = Color.parseColor("#e7ecf2")
        val MUTED = Color.parseColor("#8d97a5")
        val ACCENT = Color.parseColor("#36d399")
    }
}
