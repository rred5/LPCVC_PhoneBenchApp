package com.rred5.phonebench

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/** Minimal controls for phase 1, part 4; part 5 replaces this with the status screen. */
class MainActivity : Activity() {
    private lateinit var statusText: TextView
    private val handler = Handler(Looper.getMainLooper())
    private val refresh =
        object : Runnable {
            override fun run() {
                render()
                handler.postDelayed(this, 1_000)
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val padding = (24 * resources.displayMetrics.density).toInt()
        statusText = TextView(this).apply { textSize = 16f }
        val layout =
            LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(padding, padding, padding, padding)
                addView(TextView(context).apply { text = "PhoneBench"; textSize = 24f })
                addView(statusText)
                addView(button("Start worker") { WorkerService.send(context, WorkerService.ACTION_START) })
                addView(button("Pause") { WorkerService.send(context, WorkerService.ACTION_PAUSE) })
                addView(button("Resume") { WorkerService.send(context, WorkerService.ACTION_RESUME) })
                addView(button("Stop current job") { WorkerService.send(context, WorkerService.ACTION_STOP_JOB) })
                addView(button("Quit worker") { WorkerService.send(context, WorkerService.ACTION_QUIT) })
            }
        setContentView(layout)
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 0)
        }
        handleStartExtra(intent)
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        handleStartExtra(intent)
    }

    /** `adb shell am start -n com.rred5.phonebench/.MainActivity --ez start_worker true` starts the worker. */
    private fun handleStartExtra(intent: android.content.Intent?) {
        if (intent?.getBooleanExtra("start_worker", false) == true) {
            WorkerService.send(this, WorkerService.ACTION_START)
        }
    }

    private fun button(label: String, onClick: () -> Unit) =
        Button(this).apply {
            text = label
            setOnClickListener { onClick() }
        }

    private fun render() {
        val status = WorkerStatus.current
        val settings = Settings(this)
        statusText.text =
            buildString {
                appendLine("State: ${status.phase.name.lowercase()}${if (settings.paused) " (paused)" else ""}")
                appendLine(status.message)
                appendLine("Server: ${settings.serverUrl ?: "not set"} ${if (status.serverReachable) "· reachable" else ""}")
                status.jobName?.let { appendLine("Job: $it by ${status.submittedBy}") }
                status.model?.let { appendLine("Model: $it on ${status.backend}") }
                if (status.runTotal > 0) appendLine("Run ${status.runIndex} of ${status.runTotal}")
                status.downloadPercent?.let { appendLine("Download: $it%") }
                status.lastError?.let { appendLine("Last error: $it") }
            }
    }

    override fun onResume() {
        super.onResume()
        handler.post(refresh)
    }

    override fun onPause() {
        handler.removeCallbacks(refresh)
        super.onPause()
    }
}
