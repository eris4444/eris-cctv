package ir.eris.cctv

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import ir.eris.cctv.core.Auth
import ir.eris.cctv.core.ServerConfig

/** Everything the user sets on the phone, kept in SharedPreferences. */
class Settings(context: Context) {
    private val p: SharedPreferences = context.getSharedPreferences("eris-cctv", Context.MODE_PRIVATE)

    val agentId: String
        get() = p.getString("agent_id", null) ?: Auth.randomHex(8).also { p.edit().putString("agent_id", it).apply() }

    var systemName: String
        get() = p.getString("system_name", null) ?: "Android - " + (Build.MODEL ?: "phone")
        set(v) = p.edit().putString("system_name", v.trim().take(60)).apply()
    var cameraName: String
        get() = p.getString("camera_name", null) ?: "Phone camera"
        set(v) = p.edit().putString("camera_name", v.trim().take(60)).apply()

    var address: String
        get() = p.getString("address", "") ?: ""
        set(v) = p.edit().putString("address", cleanAddress(v)).apply()
    var port: Int
        get() = p.getInt("port", 8800)
        set(v) = p.edit().putInt("port", v).apply()
    var username: String
        get() = p.getString("username", "") ?: ""
        set(v) = p.edit().putString("username", v.trim()).apply()
    var password: String
        get() = p.getString("password", "") ?: ""
        set(v) = p.edit().putString("password", v).apply()
    var tlsMode: String
        get() = p.getString("tls_mode", "auto") ?: "auto"
        set(v) = p.edit().putString("tls_mode", v).apply()
    var tlsPin: String
        get() = p.getString("tls_pin", "") ?: ""
        set(v) = p.edit().putString("tls_pin", v).apply()

    var backCamera: Boolean
        get() = p.getBoolean("back_camera", true)
        set(v) = p.edit().putBoolean("back_camera", v).apply()
    var width: Int
        get() = p.getInt("width", 1280)
        set(v) = p.edit().putInt("width", v).apply()
    var height: Int
        get() = p.getInt("height", 720)
        set(v) = p.edit().putInt("height", v).apply()
    var fps: Int
        get() = p.getInt("fps", 15)
        set(v) = p.edit().putInt("fps", v).apply()
    var quality: String
        get() = p.getString("quality", "medium") ?: "medium"
        set(v) = p.edit().putString("quality", v).apply()
    var audio: Boolean
        get() = p.getBoolean("audio", true)
        set(v) = p.edit().putBoolean("audio", v).apply()
    /** -1 = automatic (from how the phone was held when the camera was started). */
    var rotation: Int
        get() = p.getInt("rotation", -1)
        set(v) = p.edit().putInt("rotation", v).apply()
    var displayRotation: Int
        get() = p.getInt("display_rotation", 0)
        set(v) = p.edit().putInt("display_rotation", v).apply()
    var keepCameraOn: Boolean
        get() = p.getBoolean("keep_camera_on", false)
        set(v) = p.edit().putBoolean("keep_camera_on", v).apply()
    var startOnBoot: Boolean
        get() = p.getBoolean("start_on_boot", true)
        set(v) = p.edit().putBoolean("start_on_boot", v).apply()
    var running: Boolean
        get() = p.getBoolean("running", false)
        set(v) = p.edit().putBoolean("running", v).apply()
    var lastCodec: String
        get() = p.getString("last_codec", "") ?: ""
        set(v) = p.edit().putString("last_codec", v).apply()

    val configured: Boolean get() = address.isNotEmpty() && username.isNotEmpty() && password.isNotEmpty()

    /** Video bitrate for the chosen size and quality (like the desktop agent's table, per 720p). */
    val bitrate: Int
        get() {
            val per720 = when (quality) { "low" -> 450_000; "high" -> 2_800_000; else -> 1_100_000 }
            val scale = (width * height) / (1280.0 * 720.0)
            return (per720 * scale.coerceIn(0.35, 2.5)).toInt()
        }

    fun server(): ServerConfig = ServerConfig(address, port, username, password, tlsMode, "pin", tlsPin)

    companion object {
        /** "https://host:8800/x" -> "host" (the port is taken out separately by the UI). */
        fun cleanAddress(text: String): String {
            var a = text.trim()
            for (prefix in listOf("http://", "https://", "ws://", "wss://")) {
                if (a.lowercase().startsWith(prefix)) a = a.substring(prefix.length)
            }
            return a.substringBefore('/')
        }
    }
}
