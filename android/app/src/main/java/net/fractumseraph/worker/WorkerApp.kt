package net.fractumseraph.worker

import android.app.Application
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/** Starts the embedded Python once per process; [py] is the worker driver
 *  (app/src/main/python/android_worker.py). */
class WorkerApp : Application() {
    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
    }

    companion object {
        val py: PyObject by lazy { Python.getInstance().getModule("android_worker") }
    }
}
