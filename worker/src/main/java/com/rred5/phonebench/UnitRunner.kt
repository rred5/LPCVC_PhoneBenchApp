package com.rred5.phonebench

import android.content.Context
import android.graphics.BitmapFactory
import android.os.PowerManager
import android.util.Log
import com.rred5.phonebench.api.ApiClient
import com.rred5.phonebench.api.ApiException
import com.rred5.phonebench.api.UnitChecks
import com.rred5.phonebench.api.UnitPrompt
import com.rred5.phonebench.api.WorkUnit
import com.rred5.phonebench.inference.Backend
import com.rred5.phonebench.inference.InferenceEngine
import com.rred5.phonebench.inference.ModelCatalog
import com.rred5.phonebench.inference.RunOutput
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.time.Instant

/** Requests from the person holding the phone; checked between runs. */
class Controls {
    /** Finish the current run, then hand the rest of the unit back. */
    @Volatile var pauseRequested = false

    /** Cancel the current job now. */
    @Volatile var stopRequested = false

    /** Hand the unit back now and shut the worker down. */
    @Volatile var quitRequested = false
}

private class LeaseLostException : Exception("The server reassigned this unit")

/** A downloaded, checked image and its size, for the {width} and {height} prompt placeholders. */
private class UnitImageFile(val file: File, val width: Int, val height: Int)

/** Runs one unit end to end (docs/API.md, "Phone behavior"). */
class UnitRunner(
    private val context: Context,
    private val api: ApiClient,
    private val engine: InferenceEngine,
    private val catalog: ModelCatalog,
    private val controls: Controls,
    private val pollSeconds: () -> Int,
    private val statusBody: (state: String, unitId: String?) -> JSONObject,
) {
    // Set by the heartbeat coroutine.
    @Volatile private var cancelRequested = false
    @Volatile private var leaseLost = false

    suspend fun run(unit: WorkUnit) {
        cancelRequested = false
        leaseLost = false
        val dir = File(context.cacheDir, "units/${safe(unit.unitId)}")
        val wakeLock =
            context.getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "phonebench:unit")
        wakeLock.acquire(WAKE_LOCK_MS)
        WorkerStatus.set {
            it.copy(
                phase = Phase.RUNNING, message = "Preparing", unitId = unit.unitId, jobName = unit.jobName,
                submittedBy = unit.submittedBy, model = unit.model, backend = unit.backend,
                runIndex = 0, runTotal = unit.runs.size, downloadPercent = null,
            )
        }
        try {
            coroutineScope {
                val heartbeat = launch { heartbeat(unit) }
                try {
                    val problem = UnitChecks.problem(unit, catalog, engine.framework)
                    if (problem != null) {
                        finish(unit, "failed", problem)
                    } else {
                        val images = downloadImages(unit, dir)
                        prepareModel(unit)
                        execute(unit, images)
                    }
                } finally {
                    heartbeat.cancel()
                }
            }
        } catch (error: CancellationException) {
            throw error
        } catch (error: LeaseLostException) {
            Log.w(TAG, "unit ${unit.unitId}: lease lost")
        } catch (error: Exception) {
            Log.e(TAG, "unit ${unit.unitId} failed", error)
            WorkerStatus.set { it.copy(lastError = error.message ?: error.toString()) }
            if (!leaseLost) finish(unit, "failed", error.message ?: error.toString())
        } finally {
            // A failed run may leave native state unusable; idle phones should not hold model memory.
            engine.unload()
            dir.deleteRecursively()
            if (wakeLock.isHeld) wakeLock.release()
            WorkerStatus.set {
                it.copy(unitId = null, jobName = null, submittedBy = null, model = null, backend = null, runIndex = 0, runTotal = 0, downloadPercent = null)
            }
        }
    }

    private suspend fun execute(unit: WorkUnit, images: Map<String, UnitImageFile>) {
        val backend = Backend.valueOf(unit.backend)
        val prompts = unit.prompts.associateBy { it.promptId }
        var runtime = ""
        var pendingLoadMs: Long? = null

        signal()?.let { return finish(unit, it) }
        if (unit.isWarm) {
            status("Loading ${unit.model} on ${unit.backend}")
            engine.load(unit.model, backend).also { runtime = it.runtime; pendingLoadMs = it.loadMs }
            if (unit.warmup) {
                // Excluded warm-up fills model and runtime caches.
                status("Warming up")
                val first = unit.runs.first()
                engine.reset()
                generate(unit, images.getValue(first.imageId), prompts.getValue(first.promptId))
            }
        }

        for ((index, run) in unit.runs.withIndex()) {
            signal()?.let { return finish(unit, it) }
            WorkerStatus.set { it.copy(phase = Phase.RUNNING, runIndex = index + 1, message = "Run ${index + 1} of ${unit.runs.size}") }
            if (unit.isWarm) {
                engine.reset()
            } else {
                engine.load(unit.model, backend).also { runtime = it.runtime; pendingLoadMs = it.loadMs }
            }
            val started = Instant.now()
            var outputs: List<RunOutput>? = null
            var failure: String? = null
            try {
                outputs = generate(unit, images.getValue(run.imageId), prompts.getValue(run.promptId))
            } catch (error: CancellationException) {
                throw error
            } catch (error: RunTimeoutException) {
                failure = error.message
            } catch (error: Exception) {
                failure = error.message ?: error.toString()
            }
            val finished = Instant.now()
            if (!unit.isWarm) engine.unload()
            // A run cut short by Stop, Quit or a cancel is not a measurement.
            signal()?.let { return finish(unit, it) }

            val stats = DeviceStats.read(context)
            // The run's response and metrics are the first turn's; every turn is in "turns".
            val output = outputs?.first()
            val report =
                JSONObject()
                    .put("unit_id", unit.unitId)
                    .put("status", if (output != null) "completed" else "error")
                    .put("started_at", started.toString())
                    .put("finished_at", finished.toString())
                    .put("response", output?.response ?: JSONObject.NULL)
                    .put("error", failure ?: JSONObject.NULL)
                    .put("metrics", output?.let(::metrics) ?: JSONObject.NULL)
                    .put("turns", outputs?.takeIf { it.size > 1 }?.let(::turnReports) ?: JSONObject.NULL)
                    .put("load_ms", pendingLoadMs ?: JSONObject.NULL)
                    .put("warm", unit.isWarm)
                    .put("framework", engine.framework)
                    .put("framework_version", engine.frameworkVersion)
                    .put("runtime", runtime)
                    .put("compute_unit", unit.backend)
                    .put("battery_c", stats.batteryC ?: JSONObject.NULL)
                    .put("thermal_status", stats.thermalStatus)
                    .put("free_memory_mb", stats.freeMemoryMb)
            pendingLoadMs = null
            val reply = withRetry("report run") { api.reportRun(run.runId, report) }
            if (!reply.optBoolean("continue", true)) cancelRequested = true

            if (index < unit.runs.lastIndex) rest(unit.restSeconds)
        }
        finish(unit, signal() ?: "done")
    }

    private class RunTimeoutException(seconds: Int) : Exception("Run exceeded $seconds s")

    private suspend fun generate(unit: WorkUnit, image: UnitImageFile, prompt: UnitPrompt): List<RunOutput> =
        try {
            // Not String.format: prompts often contain JSON braces.
            val turns = prompt.turns.map { it.replace("{width}", "${image.width}").replace("{height}", "${image.height}") }
            withTimeout(unit.runTimeoutS * 1000L) { engine.run(image.file.absolutePath, turns, unit.maxTokens) }
        } catch (error: TimeoutCancellationException) {
            engine.stop()
            throw RunTimeoutException(unit.runTimeoutS)
        }

    /** Why the unit should stop now, as a Finish status; null to keep going. */
    private fun signal(): String? {
        if (leaseLost) throw LeaseLostException()
        return when {
            cancelRequested || controls.stopRequested -> "cancelled"
            controls.quitRequested || controls.pauseRequested -> "released"
            else -> null
        }
    }

    /** Rest between runs, waking early for Pause, Stop or a cancel. */
    private suspend fun rest(seconds: Double) {
        if (seconds <= 0) return
        status("Resting ${seconds.toInt()} s")
        val until = System.nanoTime() + (seconds * 1e9).toLong()
        while (System.nanoTime() < until && signal() == null) delay(250)
    }

    private suspend fun prepareModel(unit: WorkUnit) {
        if (unit.model in engine.downloadedModels()) return
        WorkerStatus.set { it.copy(phase = Phase.DOWNLOADING, message = "Downloading ${unit.model}", downloadPercent = 0) }
        engine.ensureDownloaded(unit.model) { percent -> WorkerStatus.set { it.copy(downloadPercent = percent) } }
        WorkerStatus.set { it.copy(phase = Phase.RUNNING, downloadPercent = null) }
    }

    private suspend fun downloadImages(unit: WorkUnit, dir: File): Map<String, UnitImageFile> {
        status("Downloading ${unit.images.size} image(s)")
        dir.mkdirs()
        return unit.images.associate { image ->
            val file = File(dir, "${safe(image.imageId)}.png")
            withRetry("download image", attempts = 3) { api.downloadImage(image.url, file, UnitChecks.MAX_IMAGE_BYTES) }
            check(sha256(file) == image.sha256) { "Image ${image.imageId} failed its SHA-256 check" }
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(file.absolutePath, bounds)
            check(bounds.outMimeType == "image/png" && maxOf(bounds.outWidth, bounds.outHeight) in 1..UnitChecks.MAX_IMAGE_EDGE) {
                "Image ${image.imageId} is not a PNG within ${UnitChecks.MAX_IMAGE_EDGE} px"
            }
            image.imageId to UnitImageFile(file, bounds.outWidth, bounds.outHeight)
        }
    }

    private suspend fun heartbeat(unit: WorkUnit) {
        while (true) {
            delay(pollSeconds() * 1000L)
            try {
                val reply = withContext(Dispatchers.IO) { api.poll(statusBody(WorkerStatus.current.phase.apiState, unit.unitId)) }
                WorkerStatus.set { it.copy(serverReachable = true) }
                if (reply.optBoolean("cancel_unit")) {
                    cancelRequested = true
                    engine.stop()
                }
            } catch (error: ApiException) {
                if (error.status == 409) {
                    leaseLost = true
                    engine.stop()
                }
            } catch (error: IOException) {
                WorkerStatus.set { it.copy(serverReachable = false) }
            }
        }
    }

    private suspend fun finish(unit: WorkUnit, status: String, error: String? = null) {
        try {
            withRetry("finish unit") { api.finish(unit.unitId, status, error) }
        } catch (lost: LeaseLostException) {
            Log.w(TAG, "unit ${unit.unitId}: lease lost before finish")
        }
        Log.i(TAG, "UNIT_FINISHED ${unit.unitId} $status${error?.let { ": $it" } ?: ""}")
    }

    /** Retry network failures and 5xx with backoff (2 s doubling to 60 s); 409 means the lease is gone. */
    private suspend fun <T> withRetry(what: String, attempts: Int = Int.MAX_VALUE, call: () -> T): T {
        var wait = 2_000L
        var tries = 0
        while (true) {
            try {
                val result = withContext(Dispatchers.IO) { call() }
                WorkerStatus.set { it.copy(serverReachable = true) }
                return result
            } catch (error: ApiException) {
                if (error.status == 409) {
                    leaseLost = true
                    throw LeaseLostException()
                }
                if (error.status < 500 || ++tries >= attempts) throw error
            } catch (error: IOException) {
                WorkerStatus.set { it.copy(serverReachable = false) }
                if (++tries >= attempts) throw error
            }
            Log.w(TAG, "$what failed; retrying in ${wait / 1000} s")
            delay(wait)
            wait = minOf(wait * 2, 60_000L)
        }
    }

    private fun status(message: String) = WorkerStatus.set { it.copy(message = message) }

    private fun metrics(output: RunOutput) =
        JSONObject()
            .put("ttft_ms", finite(output.ttftMs))
            .put("prompt_tokens", output.promptTokens)
            .put("generated_tokens", output.generatedTokens)
            .put("prefill_tok_s", finite(output.prefillTokS))
            .put("decode_tok_s", finite(output.decodeTokS))
            .put("stop_reason", output.stopReason)

    private fun turnReports(outputs: List<RunOutput>) =
        org.json.JSONArray(outputs.map { JSONObject().put("response", it.response).put("error", JSONObject.NULL).put("metrics", metrics(it)) })

    private fun finite(value: Double): Any = if (value.isFinite()) value else JSONObject.NULL

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    /** Server ids become file names; keep them to safe characters. */
    private fun safe(id: String) = id.replace(Regex("[^A-Za-z0-9_-]"), "_")

    companion object {
        private const val TAG = "PhoneBench"
        private const val WAKE_LOCK_MS = 6 * 60 * 60 * 1000L
    }
}
