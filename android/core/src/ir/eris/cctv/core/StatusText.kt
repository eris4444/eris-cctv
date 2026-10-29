package ir.eris.cctv.core

import java.util.Locale

/**
 * The status lines of the app's main screen, as plain Kotlin so they are tested on the JVM.
 *
 * Never run String.format over text that contains other text: 1.0.0 did
 * "Battery 86%, charging, %.0f C".format(temp) - the "%," of the percentage is a format flag, the
 * formatter threw, and because that line only shows while the camera service runs, the app crashed
 * on every start after the first one. Numbers are formatted on their own, with Locale.US.
 */
object StatusText {
    fun number(value: Double, decimals: Int): String = String.format(Locale.US, "%." + decimals + "f", value)

    fun megabytes(bytes: Long): String = number(bytes / 1024.0 / 1024.0, 1) + " MB"

    fun battery(levelPercent: Int, charging: Boolean, celsius: Double): String {
        val sb = StringBuilder()
        sb.append("Battery ").append(levelPercent).append("%, ")
        sb.append(if (charging) "charging" else "NOT charging").append(", ")
        sb.append(number(celsius, 0)).append(" C")
        if (celsius >= 45) sb.append("  - hot! give the phone some air or lower the quality")
        return sb.toString()
    }

    /** s = CameraService.status(); battery = the battery line or "". */
    fun service(s: Map<String, Any?>, address: String, port: Int, fps: Int, battery: String): String {
        val server = when (s["server"]) {
            "connected" -> "Connected to " + text(s["server_name"]) +
                (if (s["secure"] == true) " (encrypted)" else " (NOT encrypted)")
            "error" -> "Server: " + text(s["error"])
            "none" -> "No server set up"
            else -> "Connecting to $address:$port..."
        }
        val camera = when (s["camera"]) {
            "running" -> "Camera on " + text(s["size"]) + " @ " + fps + " fps, " + long(s["frames"]) + " frames"
            "starting" -> "Camera starting..."
            "error" -> "Camera problem: " + text(s["camera_error"])
            else -> "Camera ready - it turns on when somebody watches"
        }
        val sb = StringBuilder()
        sb.append(server).append('\n').append(camera).append('\n')
        sb.append("Watching now: ").append(long(s["watchers"])).append('\n')
        sb.append("Sent: ").append(megabytes(long(s["sent"])))
        val dropped = long(s["dropped"])
        if (dropped > 0) sb.append("  (skipped ").append(dropped).append(" fragments - slow upload)")
        if (battery.isNotEmpty()) sb.append('\n').append(battery)
        return sb.toString()
    }

    private fun text(v: Any?): String = v?.toString() ?: ""

    private fun long(v: Any?): Long = (v as? Number)?.toLong() ?: 0L
}
