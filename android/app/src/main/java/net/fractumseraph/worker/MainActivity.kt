package net.fractumseraph.worker

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings as SysSettings
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.text.NumberFormat

class MainActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var settings: Settings
    private lateinit var status: TextView
    private lateinit var job: TextView
    private lateinit var progress: ProgressBar
    private lateinit var statusDetail: TextView
    private lateinit var updateInfo: TextView
    private lateinit var log: TextView
    private lateinit var btnStart: Button
    private lateinit var btnFinish: Button
    private lateinit var btnAbort: Button
    private val fields = mutableListOf<View>()
    private var logSeq = 0
    private val logLines = ArrayDeque<String>()
    private var lastSiteFetch = 0L
    private val pct = Regex("""(\d{1,3})%""")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        settings = Settings(this)
        applyFont(findViewById(R.id.content))
        fitSystemBars()

        status = findViewById(R.id.status)
        job = findViewById(R.id.job)
        progress = findViewById(R.id.progress)
        statusDetail = findViewById(R.id.statusDetail)
        updateInfo = findViewById(R.id.updateInfo)
        log = findViewById(R.id.log)
        btnStart = findViewById(R.id.btnStart)
        btnFinish = findViewById(R.id.btnFinish)
        btnAbort = findViewById(R.id.btnAbort)
        findViewById<TextView>(R.id.subtitle).text = "Android worker · app ${Updater.currentName(this)}"

        bindText(R.id.manager, settings.manager) { settings.manager = it }
        bindText(R.id.username, settings.username) { settings.username = it }
        bindText(R.id.workerName, settings.workerName) { settings.workerName = it }
        bindText(R.id.wallet, settings.wallet) { settings.wallet = it }
        bindText(R.id.secret, settings.secret) { settings.secret = it }
        bindText(R.id.jobs, settings.jobs.toString()) { settings.jobs = it.toIntOrNull() ?: 1 }
        bindText(R.id.maxSize, settings.maxSizeMb.toString()) { settings.maxSizeMb = it.toIntOrNull() ?: 0 }
        bindText(R.id.maxTemp, settings.maxTempC.toString()) { settings.maxTempC = it.toIntOrNull() ?: 40 }
        bindCheck(R.id.chargingOnly, settings.chargingOnly) { settings.chargingOnly = it }
        bindCheck(R.id.wifiOnly, settings.wifiOnly) { settings.wifiOnly = it }
        findViewById<CheckBox>(R.id.autoUpdate).apply {
            isChecked = settings.autoUpdate
            setOnCheckedChangeListener { _, v -> settings.autoUpdate = v }
        }
        findViewById<CheckBox>(R.id.autoStart).apply {
            isChecked = settings.autoStart
            setOnCheckedChangeListener { _, v ->
                settings.autoStart = v
                // Turning it on starts waiting right away, not just after a restart.
                if (v && !settings.wantRunning) startWorker()
            }
        }

        btnStart.setOnClickListener { startWorker() }
        btnFinish.setOnClickListener { WorkerService.send(this, WorkerService.ACTION_FINISH) }
        btnAbort.setOnClickListener { WorkerService.send(this, WorkerService.ACTION_ABORT) }
        findViewById<Button>(R.id.btnBattery).setOnClickListener { askBatteryExemption() }
        findViewById<Button>(R.id.btnUpdate).setOnClickListener { Updater.maybeCheck(this, userAsked = true) }

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    override fun onResume() {
        super.onResume()
        lastSiteFetch = 0L
        handler.post(refresh)
        Updater.maybeCheck(this)
    }

    override fun onPause() {
        handler.removeCallbacks(refresh)
        saveFields()
        super.onPause()
    }

    /** The site's font (Roboto Mono, bundled) on every text in the screen. */
    private fun applyFont(root: View) {
        val regular = Typeface.createFromAsset(assets, "fonts/RobotoMono-Regular.ttf")
        val bold = Typeface.createFromAsset(assets, "fonts/RobotoMono-Bold.ttf")
        fun walk(v: View) {
            if (v is TextView) v.typeface = if (v.typeface?.isBold == true) bold else regular
            if (v is ViewGroup) for (i in 0 until v.childCount) walk(v.getChildAt(i))
        }
        walk(root)
    }

    /** Android 15 draws apps under the status and navigation bars: keep the
     *  content (and the end of the log) clear of them and of the keyboard. */
    private fun fitSystemBars() {
        val scroll = findViewById<View>(R.id.scroll)
        if (Build.VERSION.SDK_INT >= 30) {
            scroll.setOnApplyWindowInsetsListener { v, insets ->
                val b = insets.getInsets(WindowInsets.Type.systemBars() or
                    WindowInsets.Type.displayCutout() or WindowInsets.Type.ime())
                v.setPadding(b.left, b.top, b.right, b.bottom)
                insets
            }
        } else {
            scroll.fitsSystemWindows = true
        }
    }

    private fun bindText(id: Int, value: String, save: (String) -> Unit) {
        val e = findViewById<EditText>(id)
        e.setText(value)
        e.tag = save
        fields += e
    }

    private fun bindCheck(id: Int, value: Boolean, save: (Boolean) -> Unit) {
        val c = findViewById<CheckBox>(id)
        c.isChecked = value
        c.setOnCheckedChangeListener { _, v -> save(v) }
        fields += c
    }

    @Suppress("UNCHECKED_CAST")
    private fun saveFields() {
        for (v in fields) if (v is EditText) (v.tag as (String) -> Unit)(v.text.toString())
    }

    private fun startWorker() {
        saveFields()
        if (settings.username.isBlank()) {
            Toast.makeText(this, "Enter your name first", Toast.LENGTH_SHORT).show()
            findViewById<CheckBox>(R.id.autoStart).isChecked = false
            return
        }
        settings.wantRunning = true
        WorkerService.start(this)
    }

    @SuppressLint("BatteryLife")
    private fun askBatteryExemption() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, "Already allowed", Toast.LENGTH_SHORT).show()
            return
        }
        try {
            startActivity(Intent(SysSettings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName")))
        } catch (_: Exception) {
            startActivity(Intent(SysSettings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private val refresh = object : Runnable {
        override fun run() {
            renderWorker()
            if (System.currentTimeMillis() - lastSiteFetch > 30_000) {
                lastSiteFetch = System.currentTimeMillis()
                fetchSiteStats()
            }
            handler.postDelayed(this, 1000)
        }
    }

    private fun renderWorker() {
        val py = WorkerApp.py
        val state = JSONObject(py.callAttr("get_state").toString())
        val running = state.optBoolean("running")
        val pause = WorkerService.lastPauseReason
        status.text = when {
            state.optBoolean("stopping") && running -> "Stopping (finishing the current work)"
            running && pause != null -> "Paused: $pause"
            running -> "Running"
            settings.wantRunning && pause != null -> "Waiting: $pause"
            settings.autoStart -> "Stopped (auto-start is on)"
            else -> "Stopped"
        }
        val st = state.optString("status")
        job.text = if (running) state.optString("job") else ""
        val p = if (running) pct.find(st)?.groupValues?.get(1)?.toIntOrNull() else null
        progress.visibility = if (p != null) View.VISIBLE else View.GONE
        if (p != null) progress.progress = p.coerceIn(0, 100)

        val detail = StringBuilder()
        if (running && st.isNotEmpty()) detail.append(st)
        if (state.optString("version").isNotEmpty()) detail.append("\nWorker ${state.optString("version")}")
        if (state.optString("error").isNotEmpty()) detail.append("\n").append(state.optString("error"))
        if (settings.autoStart &&
            !(getSystemService(POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(packageName)) {
            detail.append("\nTap \"Let it keep running in the background\" so Android lets auto-start work.")
        }
        statusDetail.text = detail.toString().trim()
        updateInfo.text = Updater.status

        val busy = running || settings.wantRunning
        btnStart.isEnabled = !busy
        btnFinish.isEnabled = busy
        btnAbort.isEnabled = busy
        for (v in fields) v.isEnabled = !busy

        val res = JSONObject(py.callAttr("get_log", logSeq).toString())
        val lines = res.getJSONArray("lines")
        if (lines.length() > 0) {
            for (i in 0 until lines.length()) {
                logLines.addLast(lines.getString(i))
                while (logLines.size > 300) logLines.removeFirst()
            }
            logSeq = res.getInt("seq")
            log.text = logLines.joinToString("\n")
        }
        if (!running && !settings.wantRunning) WorkerService.lastPauseReason = null
    }

    /** The dashboard's numbers, from the manager's public /api/stats. */
    private fun fetchSiteStats() {
        val base = settings.manager.trimEnd('/')
        val me = settings.username.trim()
        Thread {
            try {
                val conn = URL("$base/api/stats").openConnection() as HttpURLConnection
                conn.connectTimeout = 15000; conn.readTimeout = 15000
                val j = try { JSONObject(conn.inputStream.bufferedReader().readText()) } finally { conn.disconnect() }
                handler.post { showSiteStats(j, me) }
            } catch (_: Exception) {
                handler.post { findViewById<TextView>(R.id.you).text = "Could not reach the manager" }
            }
        }.start()
    }

    private fun showSiteStats(j: JSONObject, me: String) {
        val nf = NumberFormat.getIntegerInstance()
        findViewById<TextView>(R.id.kpiDone).text = nf.format(j.optLong("total_completed"))
        findViewById<TextView>(R.id.kpiQueue).text = nf.format(j.optLong("queue_depth"))
        findViewById<TextView>(R.id.kpiActive).text = nf.format(j.optLong("active_jobs"))
        findViewById<TextView>(R.id.kpi24h).text =
            nf.format(j.optJSONObject("throughput")?.optLong("completed_24h") ?: 0)

        val sb = j.optJSONArray("scoreboard")
        val top = StringBuilder()
        var mine = ""
        if (sb != null) {
            for (i in 0 until sb.length()) {
                val r = sb.getJSONObject(i)
                val name = r.optString("worker_id")
                val hours = r.optLong("total_minutes") / 60.0
                val line = "%2d. %-16s %7s h  %5s files".format(i + 1, name.take(16),
                    nf.format(hours.toLong()), nf.format(r.optLong("files_count")))
                if (i < 8) top.append(line).append('\n')
                if (me.isNotEmpty() && name.equals(me, ignoreCase = true)) {
                    mine = "YOU: #${i + 1} of ${sb.length()} · ${nf.format(hours.toLong())} h of video · " +
                        "${nf.format(r.optLong("files_count"))} files"
                }
            }
        }
        findViewById<TextView>(R.id.leaderboard).text = top.toString().trimEnd()
        findViewById<TextView>(R.id.you).text = mine.ifEmpty {
            if (me.isEmpty()) "Enter your name below to see your place on the scoreboard"
            else "$me is not on the scoreboard yet: your first finished encode puts you there"
        }

        val act = j.optJSONArray("active")
        val now = StringBuilder()
        if (act != null) {
            for (i in 0 until minOf(act.length(), 8)) {
                val a = act.getJSONObject(i)
                now.append("${a.optString("username")}/${a.optString("workername")}  ${a.optInt("progress")}%\n")
                now.append("  ").append(a.optString("filename").take(60)).append('\n')
            }
            if (act.length() > 8) now.append("… and ${act.length() - 8} more")
        }
        findViewById<TextView>(R.id.activeList).text = now.toString().trimEnd().ifEmpty { "Nothing right now" }
    }
}
