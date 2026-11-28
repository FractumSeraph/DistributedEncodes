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
import org.json.JSONObject
import java.io.File

/**
 * Keeps the worker running in the background (a foreground service with a
 * notification), and pauses it while the device is not charging, not on an
 * unmetered network, or too warm, as configured. The worker itself runs in
 * Python (android_worker.py) in this process.
 */
class WorkerService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var settings: Settings
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var started = false          // the Python worker has been started
    private var pausedReason: String? = null
        set(v) { field = v; lastPauseReason = v }
    private var stopRequested = false

    // Device conditions, updated by the receivers below.
    private var plugged = false
    private var tempC = 0f
    private var unmetered = false

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
            // Android 12+ refuses a foreground start from the background (e.g.
            // a sticky restart after the system killed the app). The user
            // starts it again from the app.
            settings.wantRunning = false
            stopSelf()
            return START_NOT_STICKY
        }
        if (!started && !stopRequested) beginMonitoring()
        return START_STICKY
    }

    private fun beginMonitoring() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "FractumWorker::encode").apply {
            setReferenceCounted(false); acquire()
        }
        @Suppress("DEPRECATION")
        wifiLock = (applicationContext.getSystemService(WIFI_SERVICE) as WifiManager)
            .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "FractumWorker::net").apply {
                setReferenceCounted(false); acquire()
            }
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

    private fun evaluate() {
        if (stopRequested) return
        val reason = blockingReason()
        val py = WorkerApp.py
        if (!started) {
            if (reason == null) startWorker() else pausedReason = reason
            return
        }
        if (reason != null && pausedReason == null) {
            py.callAttr("pause"); pausedReason = reason
        } else if (reason == null && pausedReason != null) {
            py.callAttr("resume"); pausedReason = null
        } else if (reason != null) {
            pausedReason = reason
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
        WorkerApp.py.callAttr("start", cfg.toString())
        started = true
        pausedReason = null
    }

    private fun requestStop(abort: Boolean) {
        stopRequested = true
        settings.wantRunning = false
        if (started && WorkerApp.py.callAttr("is_running").toBoolean()) {
            WorkerApp.py.callAttr("stop", abort)
            updateNotification(if (abort) "Stopping..." else "Finishing the current work, then stopping")
        } else {
            shutdown()
        }
    }

    private val tick = object : Runnable {
        override fun run() {
            if (started && !WorkerApp.py.callAttr("is_running").toBoolean()) {
                // The worker ended (stopped, or failed: the app shows why).
                shutdown(); return
            }
            val state = JSONObject(WorkerApp.py.callAttr("get_state").toString())
            val text = when {
                stopRequested -> "Stopping: finishing the current work"
                pausedReason != null -> "Paused: $pausedReason"
                state.optString("status").isNotEmpty() -> state.optString("status")
                else -> "Running"
            }
            updateNotification(text)
            handler.postDelayed(this, 5000)
        }
    }

    private fun shutdown() {
        handler.removeCallbacksAndMessages(null)
        try { unregisterReceiver(batteryReceiver) } catch (_: Exception) {}
        try {
            (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).unregisterNetworkCallback(netCallback)
        } catch (_: Exception) {}
        wakeLock?.let { if (it.isHeld) it.release() }
        wifiLock?.let { if (it.isHeld) it.release() }
        started = false
        pausedReason = null
        settings.wantRunning = false
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    // ---- notification ----------------------------------------------------

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            val ch = NotificationChannel(CHANNEL, "Encoding", NotificationManager.IMPORTANCE_LOW)
            ch.description = "Shows while the worker is running"
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

        fun start(c: Context) {
            val i = Intent(c, WorkerService::class.java)
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i) else c.startService(i)
        }

        fun send(c: Context, action: String) {
            c.startService(Intent(c, WorkerService::class.java).setAction(action))
        }

        /** Shared with the screen: why the worker is paused, if it is. */
        @Volatile var lastPauseReason: String? = null
    }
}
