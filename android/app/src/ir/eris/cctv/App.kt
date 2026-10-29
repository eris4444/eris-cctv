package ir.eris.cctv

import android.app.Application
import android.os.Build
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Saves the reason when the app stops unexpectedly (files/last-crash.txt); the main screen shows it on the
 * next start with a Copy button, so a crash on a phone can be reported without a cable or adb.
 */
class App : Application() {
    override fun onCreate() {
        super.onCreate()
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            try {
                val trace = StringWriter().also { error.printStackTrace(PrintWriter(it)) }.toString()
                val head = "Eris CCTV camera ${BuildInfo.VERSION} - Android ${Build.VERSION.RELEASE} " +
                    "(API ${Build.VERSION.SDK_INT}) - ${Build.MANUFACTURER} ${Build.MODEL}\n" +
                    SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date()) + " - thread " + thread.name
                val log = synchronized(CameraService.logLines) { CameraService.logLines.toList() }.takeLast(30)
                File(filesDir, CRASH_FILE).writeText(head + "\n\n" + trace + "\nlast log lines:\n" + log.joinToString("\n"))
            } catch (_: Throwable) {
            }
            previous?.uncaughtException(thread, error)
        }
    }

    companion object {
        const val CRASH_FILE = "last-crash.txt"
        const val SEEN_FILE = "last-crash-seen.txt"
    }
}
