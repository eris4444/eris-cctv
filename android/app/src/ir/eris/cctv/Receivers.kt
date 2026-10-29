package ir.eris.cctv

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.admin.DeviceAdminReceiver
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

/** Starts the camera again after the phone restarts (when it was running and "start after restart" is on). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        if (action != Intent.ACTION_BOOT_COMPLETED && action != "android.intent.action.QUICKBOOT_POWERON" &&
            action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val s = Settings(context)
        if (!s.startOnBoot || !s.running || !s.configured) return
        try {
            CameraService.start(context)
            CameraService.log("started after a restart")
        } catch (e: Exception) {
            askToStart(context)
        }
    }

    companion object {
        /** Newer Android versions may refuse a camera service from the background: ask for one tap. */
        fun askToStart(context: Context) {
            try {
                val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
                if (Build.VERSION.SDK_INT >= 26) {
                    nm.createNotificationChannel(NotificationChannel("restart", "Restart", NotificationManager.IMPORTANCE_HIGH))
                }
                val open = PendingIntent.getActivity(context, 0, Intent(context, MainActivity::class.java),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                @Suppress("DEPRECATION")
                val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(context, "restart") else Notification.Builder(context)
                nm.notify(8, b.setSmallIcon(R.drawable.ic_stat).setContentTitle("Eris CCTV camera")
                    .setContentText("Tap to start the camera again").setContentIntent(open).setAutoCancel(true).build())
            } catch (_: Exception) { }
        }
    }
}

/** Only used for "Turn screen off" (DevicePolicyManager.lockNow). */
class AdminReceiver : DeviceAdminReceiver()

object BuildInfo {
    const val VERSION = "1.0.1"
}
