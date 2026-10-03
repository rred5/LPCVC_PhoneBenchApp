package com.rred5.phonebench

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restarts the worker after a reboot if it was running. A pause stays in effect. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED && Settings(context).enabled) {
            WorkerService.send(context, WorkerService.ACTION_START)
        }
    }
}
