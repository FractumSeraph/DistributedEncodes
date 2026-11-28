package net.fractumseraph.worker

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings as SysSettings
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject

class MainActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var settings: Settings
    private lateinit var status: TextView
    private lateinit var statusDetail: TextView
    private lateinit var log: TextView
    private lateinit var btnStart: Button
    private lateinit var btnFinish: Button
    private lateinit var btnAbort: Button
    private val fields = mutableListOf<android.view.View>()
    private var logSeq = 0
    private val logLines = ArrayDeque<String>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        settings = Settings(this)
        status = findViewById(R.id.status)
        statusDetail = findViewById(R.id.statusDetail)
        log = findViewById(R.id.log)
        btnStart = findViewById(R.id.btnStart)
        btnFinish = findViewById(R.id.btnFinish)
        btnAbort = findViewById(R.id.btnAbort)

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

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    override fun onResume() {
        super.onResume()
        handler.post(refresh)
    }

    override fun onPause() {
        handler.removeCallbacks(refresh)
        saveFields()
        super.onPause()
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
            val py = WorkerApp.py
            val state = JSONObject(py.callAttr("get_state").toString())
            val running = state.optBoolean("running")
            val pause = WorkerService.lastPauseReason
            status.text = when {
                state.optBoolean("stopping") && running -> "Stopping (finishing the current work)"
                running && pause != null -> "Paused: $pause"
                running -> "Running"
                settings.wantRunning && pause != null -> "Waiting: $pause"
                settings.autoStart -> "Stopped (auto-start is on: it starts again after a restart, or press Start)"
                else -> "Stopped"
            }
            val detail = StringBuilder()
            if (state.optString("version").isNotEmpty()) detail.append("Worker ${state.optString("version")}  ")
            if (running && state.optString("job").isNotEmpty()) detail.append("\n").append(state.optString("job"))
            if (running && state.optString("status").isNotEmpty()) detail.append("\n").append(state.optString("status"))
            if (state.optString("error").isNotEmpty()) detail.append("\n").append(state.optString("error"))
            if (settings.autoStart &&
                !(getSystemService(POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(packageName)) {
                detail.append("\nTap \"Let it keep running in the background\" so Android lets auto-start work.")
            }
            statusDetail.text = detail.toString().trim()
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
            handler.postDelayed(this, 1000)
        }
    }
}
