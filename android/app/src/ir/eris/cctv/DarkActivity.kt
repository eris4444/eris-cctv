package ir.eris.cctv

import android.app.Activity
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.TextView

/**
 * Black, lowest-brightness screen while the camera keeps streaming (an OLED screen is practically off).
 * Double-tap to leave. The app stays in the foreground, which some phones need to keep the camera.
 */
class DarkActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private var lastTap = 0L
    private lateinit var hint: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.attributes = window.attributes.apply { screenBrightness = 0.01f }
        window.addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN)
        if (Build.VERSION.SDK_INT >= 27) setShowWhenLocked(true)
        hint = TextView(this).apply {
            text = "Camera is running\nDouble-tap to leave the dark screen"
            setTextColor(Color.parseColor("#303840"))
            textSize = 13f
            gravity = Gravity.CENTER
        }
        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            addView(hint, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
            setOnClickListener {
                val now = System.currentTimeMillis()
                if (now - lastTap < 400) finish()
                lastTap = now
                hint.visibility = View.VISIBLE
                ui.removeCallbacksAndMessages(null)
                ui.postDelayed({ hint.visibility = View.INVISIBLE }, 3000)
            }
        }
        setContentView(root)
        @Suppress("DEPRECATION")
        root.systemUiVisibility = View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_FULLSCREEN or
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
        ui.postDelayed({ hint.visibility = View.INVISIBLE }, 3000)
    }
}
