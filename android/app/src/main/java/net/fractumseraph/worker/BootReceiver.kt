package net.fractumseraph.worker

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * With auto-start on, starts the worker service after the device boots and
 * after the app is updated. Android allows starting a foreground service from
 * these broadcasts. The service then waits until the device is charging on
 * Wi-Fi (as configured) before it takes any work.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> {
                val settings = Settings(context)
                // After an app update, also resume a worker the user had started by hand.
                val resume = intent.action == Intent.ACTION_MY_PACKAGE_REPLACED && settings.wantRunning
                if ((settings.autoStart || resume) && settings.username.isNotBlank()) {
                    try {
                        WorkerService.start(context)
                    } catch (_: Exception) {
                        // Not allowed right now (rare); the user can start it from the app.
                    }
                }
            }
        }
    }
}
