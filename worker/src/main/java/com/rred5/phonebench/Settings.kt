package com.rred5.phonebench

import android.content.Context

/** Worker settings in app-private storage; uninstalling removes them. */
class Settings(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("phonebench", Context.MODE_PRIVATE)

    var serverUrl: String?
        get() = prefs.getString(KEY_SERVER_URL, null)
        set(value) = prefs.edit().putString(KEY_SERVER_URL, value).apply()

    var deviceKey: String?
        get() = prefs.getString(KEY_DEVICE_KEY, null)
        set(value) = prefs.edit().putString(KEY_DEVICE_KEY, value).apply()

    /** The worker should run, including after a reboot. */
    var enabled: Boolean
        get() = prefs.getBoolean(KEY_ENABLED, false)
        set(value) = prefs.edit().putBoolean(KEY_ENABLED, value).apply()

    /** Stop taking work; survives reboots. */
    var paused: Boolean
        get() = prefs.getBoolean(KEY_PAUSED, false)
        set(value) = prefs.edit().putBoolean(KEY_PAUSED, value).apply()

    val configured get() = !serverUrl.isNullOrBlank() && !deviceKey.isNullOrBlank()

    companion object {
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_DEVICE_KEY = "device_key"
        private const val KEY_ENABLED = "enabled"
        private const val KEY_PAUSED = "paused"
    }
}
