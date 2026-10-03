package com.rred5.phonebench

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.PowerManager

/** Readings sent with every poll and run report. */
data class DeviceStats(
    val batteryC: Double?,
    val thermalStatus: String,
    val freeMemoryMb: Long,
) {
    companion object {
        fun read(context: Context): DeviceStats {
            val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val tenths = battery?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE) ?: Int.MIN_VALUE
            val power = context.getSystemService(PowerManager::class.java)
            val memory = ActivityManager.MemoryInfo().also { context.getSystemService(ActivityManager::class.java).getMemoryInfo(it) }
            return DeviceStats(
                batteryC = if (tenths == Int.MIN_VALUE) null else tenths / 10.0,
                thermalStatus = thermalName(power.currentThermalStatus),
                freeMemoryMb = memory.availMem / (1024 * 1024),
            )
        }

        private fun thermalName(status: Int) =
            when (status) {
                PowerManager.THERMAL_STATUS_NONE -> "none"
                PowerManager.THERMAL_STATUS_LIGHT -> "light"
                PowerManager.THERMAL_STATUS_MODERATE -> "moderate"
                PowerManager.THERMAL_STATUS_SEVERE -> "severe"
                PowerManager.THERMAL_STATUS_CRITICAL -> "critical"
                PowerManager.THERMAL_STATUS_EMERGENCY -> "emergency"
                PowerManager.THERMAL_STATUS_SHUTDOWN -> "shutdown"
                else -> "unknown"
            }
    }
}
