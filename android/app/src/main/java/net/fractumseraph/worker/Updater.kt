package net.fractumseraph.worker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * Updates the app from the GitHub release the "Android app" workflow
 * publishes (.github/workflows/android.yml): it reads update.json from the
 * latest release, downloads FractumWorker.apk, checks its SHA-256 and installs
 * it with PackageInstaller. Android asks the user to confirm the first update
 * (the app was installed by a browser); after that the app is its own
 * installer and, on Android 12+, later updates can install without asking.
 * An update is never installed while the worker is encoding: installing
 * restarts the app, so it waits until the worker is idle, waiting or paused.
 */
object Updater {
    private const val RELEASES = "https://github.com/FractumSeraph/DistributedEncodes/releases/latest/download"
    const val META_URL = "$RELEASES/update.json"
    private const val AUTO_CHECK_EVERY_MS = 6 * 3600 * 1000L
    private const val CHANNEL = "updates"
    private const val NOTIF_ID = 2

    data class Info(val code: Long, val name: String, val sha256: String, val apkUrl: String)

    /** One line for the screen. */
    @Volatile var status: String = ""
    @Volatile private var busy = false

    fun currentCode(c: Context): Long {
        val pi = c.packageManager.getPackageInfo(c.packageName, 0)
        return if (Build.VERSION.SDK_INT >= 28) pi.longVersionCode else @Suppress("DEPRECATION") pi.versionCode.toLong()
    }

    fun currentName(c: Context): String =
        c.packageManager.getPackageInfo(c.packageName, 0).versionName ?: "?"

    private fun pending(c: Context) = File(c.cacheDir, "update.apk")
    private fun pendingInfo(c: Context) = File(c.cacheDir, "update.json")

    /** Called often (screen and service); does something at most every 6 hours,
     *  or right away when the user asks. Runs in the background. */
    fun maybeCheck(c: Context, userAsked: Boolean = false) {
        val s = Settings(c)
        if (!userAsked) {
            if (!s.autoUpdate) return
            if (System.currentTimeMillis() - s.lastUpdateCheck < AUTO_CHECK_EVERY_MS) {
                installIfIdle(c)   // a download may be waiting for the worker to be idle
                return
            }
            if (!unmetered(c)) return
        }
        if (busy) return
        busy = true
        val app = c.applicationContext
        Thread {
            try {
                s.lastUpdateCheck = System.currentTimeMillis()
                check(app)
            } catch (e: Exception) {
                status = "Update check failed: ${e.message ?: e.javaClass.simpleName}"
            } finally {
                busy = false
            }
        }.start()
    }

    private fun check(c: Context) {
        status = "Checking for an app update..."
        val info = fetchInfo() ?: run { status = "No app release published yet"; return }
        if (info.code <= currentCode(c)) {
            status = "App is up to date (${currentName(c)})"
            return
        }
        val apk = pending(c)
        if (!(apk.exists() && sha256(apk) == info.sha256)) {
            status = "Downloading app ${info.name}..."
            download(info.apkUrl, apk)
            if (sha256(apk) != info.sha256) {
                apk.delete()
                status = "The downloaded update did not match its checksum; not installing it"
                return
            }
        }
        pendingInfo(c).writeText(JSONObject().put("code", info.code).put("name", info.name).toString())
        status = "App ${info.name} downloaded"
        installIfIdle(c)
    }

    /** Install a downloaded update now if the worker is not encoding. */
    fun installIfIdle(c: Context) {
        val apk = pending(c)
        if (!apk.exists()) return
        val name = try { JSONObject(pendingInfo(c).readText()).optString("name") } catch (_: Exception) { "" }
        val code = try { JSONObject(pendingInfo(c).readText()).optLong("code") } catch (_: Exception) { 0L }
        if (code in 1..currentCode(c)) {         // already installed
            apk.delete(); pendingInfo(c).delete(); return
        }
        val encoding = try {
            val st = JSONObject(WorkerApp.py.callAttr("get_state").toString())
            st.optBoolean("running") && WorkerService.lastPauseReason == null
        } catch (_: Exception) { false }
        if (encoding) {
            status = "App $name is ready; it installs when the current encode is finished or paused"
            return
        }
        install(c, apk, name)
    }

    private fun install(c: Context, apk: File, name: String) {
        status = "Installing app $name..."
        val pi = c.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        params.setAppPackageName(c.packageName)
        if (Build.VERSION.SDK_INT >= 31) {
            params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val id = pi.createSession(params)
        pi.openSession(id).use { session ->
            session.openWrite("app.apk", 0, apk.length()).use { out ->
                apk.inputStream().use { it.copyTo(out) }
                session.fsync(out)
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
            val result = PendingIntent.getBroadcast(c, id, Intent(c, InstallReceiver::class.java), flags)
            session.commit(result.intentSender)
        }
    }

    private fun fetchInfo(): Info? {
        val conn = URL(META_URL).openConnection() as HttpURLConnection
        conn.connectTimeout = 20000; conn.readTimeout = 20000
        conn.instanceFollowRedirects = true
        try {
            if (conn.responseCode == 404) return null
            if (conn.responseCode != 200) throw RuntimeException("HTTP ${conn.responseCode}")
            val j = JSONObject(conn.inputStream.bufferedReader().readText())
            return Info(j.getLong("versionCode"), j.getString("versionName"),
                j.getString("sha256").lowercase(), j.optString("apk").ifEmpty { "$RELEASES/FractumWorker.apk" })
        } finally {
            conn.disconnect()
        }
    }

    private fun download(url: String, dest: File) {
        var u = URL(url)
        repeat(5) {   // follow GitHub's redirect to its download host
            val conn = u.openConnection() as HttpURLConnection
            conn.connectTimeout = 20000; conn.readTimeout = 60000
            conn.instanceFollowRedirects = false
            try {
                when (conn.responseCode) {
                    in 300..399 -> { u = URL(u, conn.getHeaderField("Location")); return@repeat }
                    200 -> {
                        val tmp = File(dest.path + ".part")
                        conn.inputStream.use { inp -> tmp.outputStream().use { inp.copyTo(it) } }
                        if (!tmp.renameTo(dest)) throw RuntimeException("could not save the download")
                        return
                    }
                    else -> throw RuntimeException("HTTP ${conn.responseCode}")
                }
            } finally {
                conn.disconnect()
            }
        }
        throw RuntimeException("too many redirects")
    }

    private fun sha256(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        f.inputStream().use { inp ->
            val buf = ByteArray(1 shl 16)
            while (true) { val n = inp.read(buf); if (n < 0) break; md.update(buf, 0, n) }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    private fun unmetered(c: Context): Boolean {
        val cm = c.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
    }

    /** Android needs the user to confirm (first update, or no silent updates
     *  on this version): show it now and leave a notification as well. */
    fun askUser(c: Context, confirm: Intent) {
        confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        try { c.startActivity(confirm) } catch (_: Exception) {}
        val nm = c.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "App updates", NotificationManager.IMPORTANCE_DEFAULT))
        }
        val pi = PendingIntent.getActivity(c, 3, confirm, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(c, CHANNEL)
                else @Suppress("DEPRECATION") Notification.Builder(c)
        nm.notify(NOTIF_ID, b.setSmallIcon(R.drawable.ic_stat_encode)
            .setContentTitle("Fractum Worker update ready")
            .setContentText("Tap to install the new version")
            .setContentIntent(pi).setAutoCancel(true).build())
        status = "Waiting for you to confirm the update"
    }
}

/** Result of an update install session. */
class InstallReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, intent: Intent) {
        when (val st = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                if (confirm != null) Updater.askUser(c, confirm)
            }
            PackageInstaller.STATUS_SUCCESS -> Updater.status = "Update installed"
            else -> {
                val msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "status $st"
                Updater.status = if (st == PackageInstaller.STATUS_FAILURE_CONFLICT ||
                                     st == PackageInstaller.STATUS_FAILURE_INCOMPATIBLE)
                    "Update could not replace this copy ($msg). If this is a test build, uninstall it once and install the app from the GitHub releases."
                else "Update failed: $msg"
            }
        }
    }
}
