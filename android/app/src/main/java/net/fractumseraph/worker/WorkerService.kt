package net.fractumseraph.worker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import org.json.JSONObject
import java.io.File

/**
 * Keeps the worker going in the background (a foreground service with a
 * notification). The worker (Python, android_worker.py, in this process) runs
 * only while the device is charging, on an unmetered network and cool enough,
 * as configured: until then the service waits, and when a condition stops
 * holding it pauses the worker (FFmpeg is frozen). A pause longer than
 * [HAND_BACK_AFTER_MS] hands the work back to the server, so nothing sits
 * half-done for hours; fresh work is taken when the conditions return.
 *
 * With auto-start on, the service is started at boot (BootReceiver) and
 * simply keeps waiting for the conditions, so the device contributes every
 * time it is plugged in on Wi-Fi. While waiting it holds no wake lock.
 */
class WorkerService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var settings: Settings
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var monitoring = false
    private var started = false          // the Python worker was started and may be running
    private var stopRequested = false    // the user pressed Finish & stop / Stop now
    private var handingBack = false      // stopping the worker after a long pause
    private var retryAt = 0L             // after the worker failed: do not restart before this
    private var pausedSince = 0L
    private var pausedReason: String? = null
        set(v) { field = v; lastPauseReason = v }

    // Device conditions, updated by the receivers below.
    private var plugged = false
    private var tempC = 0f
    private var unmetered = false

    private val py get() = WorkerApp.py
    private fun now() = SystemClock.elapsedRealtime()   // counts time asleep too

    private val batteryReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            plugged = i.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0
            tempC = i.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) / 10f
            evaluate()
        }
    }
    private val netCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onCapabilitiesChanged(n: Network, caps: NetworkCapabilities) {
            handler.post { refreshNetwork() }
        }
        override fun onLost(n: Network) {
            handler.post { refreshNetwork() }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_FINISH -> { requestStop(abort = false); return START_NOT_STICKY }
            ACTION_ABORT -> { requestStop(abort = true); return START_NOT_STICKY }
        }
        try {
            goForeground("Starting...")
        } catch (e: Exception) {
            // Android 12+ refuses a foreground start from the background
            // unless the app is exempt (boot, an app update, or battery
            // optimisation turned off for it). Start again from the app.
            settings.wantRunning = false
            stopSelf()
            return START_NOT_STICKY
        }
        settings.wantRunning = true
        if (!monitoring && !stopRequested) beginMonitoring()
        return START_STICKY
    }

    private fun beginMonitoring() {
        monitoring = true
        registerReceiver(batteryReceiver, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).registerDefaultNetworkCallback(netCallback)
        refreshNetwork()
        handler.post(tick)
    }

    private fun refreshNetwork() {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        unmetered = caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
        evaluate()
    }

    /** Why the worker should not run right now, or null when it may. */
    private fun blockingReason(): String? {
        val maxT = settings.maxTempC.toFloat()
        // Hysteresis: once paused for heat, wait until 3 °C below the limit.
        val heatPaused = pausedReason?.startsWith("Too warm") == true
        return when {
            settings.chargingOnly && !plugged -> "Not charging"
            settings.wifiOnly && !unmetered -> "Not on Wi-Fi"
            tempC >= maxT || (heatPaused && tempC > maxT - 3) -> "Too warm (%.1f °C)".format(tempC)
            else -> null
        }
    }

    private fun workerRunning() = started && py.callAttr("is_running").toBoolean()

    private fun evaluate() {
        if (stopRequested || handingBack || !monitoring) return
        val reason = blockingReason()
        if (!workerRunning()) {
            started = false
            when {
                reason != null -> pausedReason = reason
                now() < retryAt -> pausedReason = "Retrying soon after a problem (see the log)"
                else -> startWorker()
            }
            return
        }
        when {
            reason != null && pausedReason == null -> {
                py.callAttr("pause"); pausedReason = reason; pausedSince = now(); releaseLocks()
            }
            reason == null && pausedReason != null -> {
                if (now() - pausedSince > HAND_BACK_AFTER_MS) {
                    handBack()            // too long: the server has likely moved on
                } else {
                    py.callAttr("resume"); pausedReason = null; acquireLocks()
                }
            }
            reason != null -> pausedReason = reason
        }
    }

    private fun startWorker() {
        val bundled = File(filesDir, "worker_bundled.py")
        assets.open("worker.py").use { input -> bundled.outputStream().use { input.copyTo(it) } }
        val lib = applicationInfo.nativeLibraryDir
        val cfg = JSONObject()
            .put("files_dir", filesDir.absolutePath)
            .put("bundled", bundled.absolutePath)
            .put("ffmpeg", "$lib/libffmpeg.so")
            .put("ffprobe", "$lib/libffprobe.so")
            .put("manager", settings.manager)
            .put("username", settings.username.ifEmpty { "Anonymous" })
            .put("workername", settings.workerName.ifEmpty { Settings.defaultWorkerName() })
            .put("jobs", settings.jobs)
            .put("max_size_mb", settings.maxSizeMb)
            .put("wallet", settings.wallet)
            .put("secret", settings.secret)
        py.callAttr("start", cfg.toString())
        started = true
        pausedReason = null
        acquireLocks()
    }

    private fun handBack() {
        handingBack = true
        py.callAttr("stop", true)
        updateNotification("Handing the paused work back to the server")
    }

    private fun requestStop(abort: Boolean) {
        stopRequested = true
        settings.wantRunning = false
        if (workerRunning()) {
            py.callAttr("stop", abort)
            updateNotification(if (abort) "Stopping..." else "Finishing the current work, then stopping")
        } else {
            shutdown()
        }
    }

    private val tick = object : Runnable {
        override fun run() {
            if (started && !py.callAttr("is_running").toBoolean()) {
                started = false
                val error = JSONObject(py.callAttr("get_state").toString()).optString("error")
                when {
                    stopRequested -> { shutdown(); return }
                    handingBack -> { handingBack = false; pausedReason = null }
                    settings.autoStart -> { retryAt = now() + RETRY_AFTER_ERROR_MS }   // keep trying
                    else -> { shutdown(); return }   // ended on its own: the app shows why
                }
                if (error.isEmpty()) retryAt = 0L
                releaseLocks()
            }
            if (workerRunning() && pausedReason != null && !handingBack &&
                now() - pausedSince > HAND_BACK_AFTER_MS) {
                handBack()
            }
            evaluate()
            val state = JSONObject(py.callAttr("get_state").toString())
            val text = when {
                stopRequested -> "Stopping: finishing the current work"
                handingBack -> "Handing the paused work back to the server"
                !workerRunning() && pausedReason != null -> "Waiting: $pausedReason"
                pausedReason != null -> "Paused: $pausedReason"
                state.optString("status").isNotEmpty() -> state.optString("status")
                else -> "Running"
            }
            updateNotification(text)
            Updater.maybeCheck(this@WorkerService)
            handler.postDelayed(this, 5000)
        }
    }

    private fun acquireLocks() {
        if (wakeLock == null) {
            wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "FractumWorker::encode")
                .apply { setReferenceCounted(false) }
        }
        if (wifiLock == null) {
            @Suppress("DEPRECATION")
            wifiLock = (applicationContext.getSystemService(WIFI_SERVICE) as WifiManager)
                .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "FractumWorker::net")
                .apply { setReferenceCounted(false) }
        }
        @Suppress("WakelockTimeout")   // held only while encoding; released on pause or stop
        if (wakeLock?.isHeld == false) wakeLock?.acquire()
        if (wifiLock?.isHeld == false) wifiLock?.acquire()
    }

    private fun releaseLocks() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wifiLock?.let { if (it.isHeld) it.release() }
    }

    private fun shutdown() {
        handler.removeCallbacksAndMessages(null)
        if (monitoring) {
            try { unregisterReceiver(batteryReceiver) } catch (_: Exception) {}
            try {
                (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).unregisterNetworkCallback(netCallback)
            } catch (_: Exception) {}
        }
        monitoring = false
        releaseLocks()
        started = false
        pausedReason = null
        settings.wantRunning = false
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        releaseLocks()
        super.onDestroy()
    }

    // ---- notification ----------------------------------------------------

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            val ch = NotificationChannel(CHANNEL, "Encoding", NotificationManager.IMPORTANCE_LOW)
            ch.description = "Shows while the worker is running or waiting to run"
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).createNotificationChannel(ch)
        }
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val finish = PendingIntent.getService(this, 1,
            Intent(this, WorkerService::class.java).setAction(ACTION_FINISH),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL)
                else @Suppress("DEPRECATION") Notification.Builder(this)
        return b.setSmallIcon(R.drawable.ic_stat_encode)
            .setContentTitle("Fractum Worker")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Finish & stop", finish).build())
            .build()
    }

    private fun goForeground(text: String) {
        val n = buildNotification(text)
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun updateNotification(text: String) {
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).notify(NOTIF_ID, buildNotification(text))
    }

    companion object {
        const val CHANNEL = "encoding"
        const val NOTIF_ID = 1
        const val ACTION_FINISH = "net.fractumseraph.worker.FINISH"
        const val ACTION_ABORT = "net.fractumseraph.worker.ABORT"
        const val HAND_BACK_AFTER_MS = 30 * 60 * 1000L
        const val RETRY_AFTER_ERROR_MS = 5 * 60 * 1000L

        fun start(c: Context) {
            val i = Intent(c, WorkerService::class.java)
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i) else c.startService(i)
        }

        fun send(c: Context, action: String) {
            c.startService(Intent(c, WorkerService::class.java).setAction(action))
        }

        /** Shared with the screen: why the worker is paused or waiting, if it is. */
        @Volatile var lastPauseReason: String? = null
    }
}
