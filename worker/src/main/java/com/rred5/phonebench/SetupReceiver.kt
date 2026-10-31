package com.rred5.phonebench

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * One-time setup over adb. The manifest protects this receiver with DUMP,
 * which only the shell and the system hold, so other apps cannot change the
 * server or key:
 *
 *     adb shell am broadcast -n com.rred5.phonebench/.SetupReceiver \
 *         --es server_url http://127.0.0.1:8000 --es device_key pbd_...
 *
 * It also installs a model bundle pushed into the app's files/imports/ (for
 * bundles too large for the SDK's in-memory download); the worker runs the
 * import between units and logs PHONEBENCH_IMPORT:
 *
 *     adb shell am broadcast -n com.rred5.phonebench/.SetupReceiver  *         --es import_model Gemma-4-E4B-it --es import_file gemma.zip
 */
class SetupReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val settings = Settings(context)
        val notes = mutableListOf<String>()
        intent.getStringExtra("server_url")?.let { url ->
            val local = url.startsWith("http://127.0.0.1") || url.startsWith("http://localhost")
            if (url.startsWith("https://") || (BuildConfig.DEBUG && local)) {
                settings.serverUrl = url.trimEnd('/')
            } else {
                notes += "server_url rejected: use https://, or http://127.0.0.1 in debug builds"
            }
        }
        intent.getStringExtra("device_key")?.let { key ->
            if (key.startsWith("pbd_") && key.length in 20..200) settings.deviceKey = key else notes += "device_key rejected"
        }
        val importModel = intent.getStringExtra("import_model")
        val importFile = intent.getStringExtra("import_file")
        if (importModel != null || importFile != null) {
            // A bare file name keeps the import inside files/imports/.
            if (importModel != null && importFile != null && importFile.matches(Regex("[A-Za-z0-9._-]+")) && importFile != "." && importFile != "..") {
                WorkerService.requestImport(context, importModel, importFile)
                notes += "import of $importModel queued"
            } else {
                notes += "import rejected: give import_model and a bare import_file name"
            }
        }
        val summary = "server=${settings.serverUrl ?: "missing"} key=${if (settings.deviceKey != null) "set" else "missing"}"
        Log.i(TAG, "PHONEBENCH_SETUP $summary ${notes.joinToString("; ")}")
        resultData = (listOf(summary) + notes).joinToString("; ")
    }

    companion object {
        private const val TAG = "PhoneBench"
    }
}
