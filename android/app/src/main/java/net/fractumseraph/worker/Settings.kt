package net.fractumseraph.worker

import android.content.Context
import android.os.Build

/** Everything the user can configure, kept in SharedPreferences. */
class Settings(context: Context) {
    private val p = context.getSharedPreferences("worker", Context.MODE_PRIVATE)

    var manager: String
        get() = p.getString("manager", DEFAULT_MANAGER)!!
        set(v) = p.edit().putString("manager", v.trim()).apply()
    var username: String
        get() = p.getString("username", "")!!
        set(v) = p.edit().putString("username", v.trim()).apply()
    var workerName: String
        get() = p.getString("workerName", defaultWorkerName())!!
        set(v) = p.edit().putString("workerName", v.trim()).apply()
    var jobs: Int
        get() = p.getInt("jobs", 1)
        set(v) = p.edit().putInt("jobs", v.coerceIn(1, 8)).apply()
    var maxSizeMb: Int
        get() = p.getInt("maxSizeMb", 0)
        set(v) = p.edit().putInt("maxSizeMb", v.coerceAtLeast(0)).apply()
    var wallet: String
        get() = p.getString("wallet", "")!!
        set(v) = p.edit().putString("wallet", v.trim()).apply()
    var secret: String
        get() = p.getString("secret", "")!!
        set(v) = p.edit().putString("secret", v.trim()).apply()
    var chargingOnly: Boolean
        get() = p.getBoolean("chargingOnly", true)
        set(v) = p.edit().putBoolean("chargingOnly", v).apply()
    var wifiOnly: Boolean
        get() = p.getBoolean("wifiOnly", true)
        set(v) = p.edit().putBoolean("wifiOnly", v).apply()
    var maxTempC: Int
        get() = p.getInt("maxTempC", 40)
        set(v) = p.edit().putInt("maxTempC", v.coerceIn(30, 50)).apply()
    /** Start the service at boot (and after app updates); it then waits for
     *  the charging / Wi-Fi / temperature conditions by itself. */
    var autoStart: Boolean
        get() = p.getBoolean("autoStart", false)
        set(v) = p.edit().putBoolean("autoStart", v).apply()
    /** The user pressed Start and has not stopped it since. */
    var wantRunning: Boolean
        get() = p.getBoolean("wantRunning", false)
        set(v) = p.edit().putBoolean("wantRunning", v).apply()

    companion object {
        const val DEFAULT_MANAGER = "https://encode.fractumseraph.net"

        fun defaultWorkerName(): String {
            val model = "${Build.MANUFACTURER} ${Build.MODEL}".replace(Regex("[^A-Za-z0-9 _-]"), "").trim()
            return model.replace(' ', '-').take(40).ifEmpty { "Android" }
        }
    }
}
