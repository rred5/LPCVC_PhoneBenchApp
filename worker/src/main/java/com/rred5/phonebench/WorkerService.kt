package com.rred5.phonebench

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.rred5.phonebench.api.ApiClient
import com.rred5.phonebench.api.ApiException
import com.rred5.phonebench.api.WorkUnit
import com.rred5.phonebench.inference.GenieXEngine
import com.rred5.phonebench.inference.InferenceEngine
import com.rred5.phonebench.inference.ModelCatalog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException

/**
 * The worker: a foreground service that polls the server and runs units one at
 * a time, with the screen off and the app closed.
 */
class WorkerService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val controls = Controls()
    private lateinit var settings: Settings
    private lateinit var catalog: ModelCatalog
    private lateinit var engine: InferenceEngine
    private var loop: Job? = null
    @Volatile private var pollSeconds = 10
    @Volatile private var downloaded: List<String> = emptyList()

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        catalog = ModelCatalog.load(this)
        engine = GenieXEngine(this, catalog)
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Worker", NotificationManager.IMPORTANCE_LOW),
        )
        scope.launch {
            WorkerStatus.flow.collect { status ->
                getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(status))
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ServiceCompat.startForeground(
            this, NOTIFICATION_ID, notification(WorkerStatus.current), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
        )
        when (intent?.action) {
            ACTION_PAUSE -> {
                settings.paused = true
                controls.pauseRequested = true
                WorkerStatus.set { it.copy(message = if (it.unitId != null) "Pausing after this run" else "Paused") }
            }
            ACTION_RESUME -> {
                settings.paused = false
                controls.pauseRequested = false
            }
            ACTION_STOP_JOB -> {
                controls.stopRequested = true
                scope.launch { engine.stop() }
            }
            ACTION_QUIT -> {
                settings.enabled = false
                if (WorkerStatus.current.unitId == null) {
                    stopSelf()
                } else {
                    // Hand the unit back first; the loop stops the service afterwards.
                    controls.quitRequested = true
                    scope.launch { engine.stop() }
                }
                return START_NOT_STICKY
            }
            else -> settings.enabled = true
        }
        if (loop == null) loop = scope.launch { runLoop() }
        return START_STICKY
    }

    private suspend fun runLoop() {
        var backoff = 2_000L
        while (currentCoroutineContext().isActive) {
            if (controls.quitRequested) {
                stopSelf()
                return
            }
            if (!settings.configured) {
                WorkerStatus.set { it.copy(phase = Phase.ERROR, message = "Not set up: send the server address and device key over adb") }
                delay(5_000)
                continue
            }
            val paused = settings.paused
            controls.pauseRequested = paused
            controls.stopRequested = false
            val api = ApiClient(settings.serverUrl!!, settings.deviceKey!!, BuildConfig.VERSION_NAME)
            try {
                downloaded = engine.downloadedModels()
                val reply = withContext(Dispatchers.IO) { api.poll(statusBody(if (paused) "paused" else "idle", null)) }
                backoff = 2_000L
                pollSeconds = reply.optInt("poll_after_s", 10).coerceIn(2, 300)
                WorkerStatus.set {
                    it.copy(
                        phase = if (paused) Phase.PAUSED else Phase.IDLE,
                        message = if (paused) "Paused" else "Waiting for work",
                        serverReachable = true,
                    )
                }
                val unitJson = reply.optJSONObject("unit")
                if (unitJson != null) {
                    val unit =
                        try {
                            WorkUnit.parse(unitJson)
                        } catch (error: JSONException) {
                            Log.e(TAG, "unreadable unit", error)
                            withContext(Dispatchers.IO) { api.finish(unitJson.optString("unit_id"), "failed", "Unreadable unit: ${error.message}") }
                            null
                        }
                    if (unit != null) {
                        UnitRunner(this, api, engine, catalog, controls, { pollSeconds }, ::statusBody).run(unit)
                        continue // ask for more work right away
                    }
                }
            } catch (error: ApiException) {
                val (message, wait) =
                    when (error.status) {
                        401 -> "Device key rejected; set a new one over adb" to 60_000L
                        426 -> "Update the app: ${error.message}" to 300_000L
                        else -> "Server error: ${error.message}" to backoff
                    }
                WorkerStatus.set { it.copy(phase = Phase.ERROR, message = message, lastError = message) }
                delay(wait)
                backoff = minOf(backoff * 2, 60_000L)
                continue
            } catch (error: IOException) {
                WorkerStatus.set { it.copy(phase = Phase.ERROR, message = "Can't reach the server", serverReachable = false) }
                delay(backoff)
                backoff = minOf(backoff * 2, 60_000L)
                continue
            }
            delay(pollSeconds * 1_000L)
        }
    }

    private fun statusBody(state: String, unitId: String?): JSONObject {
        val stats = DeviceStats.read(this)
        return JSONObject()
            .put("state", state)
            .put("unit_id", unitId ?: JSONObject.NULL)
            .put("app_version", BuildConfig.VERSION_NAME)
            .put("framework", engine.framework)
            .put("framework_version", engine.frameworkVersion)
            .put("models_downloaded", JSONArray(downloaded))
            .put("battery_c", stats.batteryC ?: JSONObject.NULL)
            .put("thermal_status", stats.thermalStatus)
            .put("free_memory_mb", stats.freeMemoryMb)
    }

    private fun notification(status: Status): Notification {
        val title =
            when (status.phase) {
                Phase.RUNNING -> "Running ${status.runIndex}/${status.runTotal} · ${status.model} on ${status.backend}"
                Phase.DOWNLOADING -> "Downloading ${status.model} · ${status.downloadPercent ?: 0}%"
                Phase.PAUSED -> "Paused"
                Phase.ERROR -> "Needs attention"
                Phase.IDLE, Phase.STOPPED -> "Waiting for work"
            }
        val builder =
            NotificationCompat
                .Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("PhoneBench · $title")
                .setContentText(status.message)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(
                    PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE),
                )
        if (settings.paused) {
            builder.addAction(0, "Resume", action(ACTION_RESUME))
        } else {
            builder.addAction(0, "Pause", action(ACTION_PAUSE))
        }
        if (status.unitId != null) builder.addAction(0, "Stop job", action(ACTION_STOP_JOB))
        return builder.build()
    }

    private fun action(name: String): PendingIntent =
        PendingIntent.getService(
            this, name.hashCode(), Intent(this, WorkerService::class.java).setAction(name), PendingIntent.FLAG_IMMUTABLE,
        )

    override fun onDestroy() {
        loop?.cancel()
        engine.unload()
        scope.cancel()
        WorkerStatus.set { Status(phase = Phase.STOPPED, message = "Stopped") }
        Log.i(TAG, "worker stopped")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val TAG = "PhoneBench"
        private const val CHANNEL_ID = "worker"
        private const val NOTIFICATION_ID = 1
        const val ACTION_START = "com.rred5.phonebench.START"
        const val ACTION_PAUSE = "com.rred5.phonebench.PAUSE"
        const val ACTION_RESUME = "com.rred5.phonebench.RESUME"
        const val ACTION_STOP_JOB = "com.rred5.phonebench.STOP_JOB"
        const val ACTION_QUIT = "com.rred5.phonebench.QUIT"

        fun send(context: Context, action: String) {
            ContextCompat.startForegroundService(context, Intent(context, WorkerService::class.java).setAction(action))
        }
    }
}
