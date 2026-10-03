package com.rred5.phonebench.api

import com.rred5.phonebench.inference.Backend
import com.rred5.phonebench.inference.ModelCatalog
import org.json.JSONArray
import org.json.JSONObject

data class UnitImage(val imageId: String, val url: String, val sha256: String, val bytes: Long)

data class UnitPrompt(val promptId: String, val text: String)

data class UnitRun(val runId: String, val imageId: String, val promptId: String, val repetition: Int)

/** A unit as the server sends it (docs/API.md, Poll). */
data class WorkUnit(
    val unitId: String,
    val jobId: String,
    val jobName: String,
    val submittedBy: String,
    val leaseS: Int,
    val framework: String,
    val model: String,
    val backend: String,
    val mode: String,
    val warmup: Boolean,
    val restSeconds: Double,
    val maxTokens: Int,
    val runTimeoutS: Int,
    val images: List<UnitImage>,
    val prompts: List<UnitPrompt>,
    val runs: List<UnitRun>,
) {
    val isWarm get() = mode == "warm"

    companion object {
        fun parse(json: JSONObject) =
            WorkUnit(
                unitId = json.getString("unit_id"),
                jobId = json.getString("job_id"),
                jobName = json.optString("job_name"),
                submittedBy = json.optString("submitted_by"),
                leaseS = json.getInt("lease_s"),
                framework = json.getString("framework"),
                model = json.getString("model"),
                backend = json.getString("backend"),
                mode = json.getString("mode"),
                warmup = json.getBoolean("warmup"),
                restSeconds = json.getDouble("rest_seconds"),
                maxTokens = json.getInt("max_tokens"),
                runTimeoutS = json.getInt("run_timeout_s"),
                images = json.getJSONArray("images").objects().map {
                    UnitImage(it.getString("image_id"), it.getString("url"), it.getString("sha256"), it.getLong("bytes"))
                },
                prompts = json.getJSONArray("prompts").objects().map { UnitPrompt(it.getString("prompt_id"), it.getString("text")) },
                runs = json.getJSONArray("runs").objects().map {
                    UnitRun(it.getString("run_id"), it.getString("image_id"), it.getString("prompt_id"), it.getInt("repetition"))
                },
            )

        private fun JSONArray.objects(): List<JSONObject> = (0 until length()).map { getJSONObject(it) }
    }
}

/** The phone re-checks every unit against the limits in docs/DESIGN.md before running it. */
object UnitChecks {
    const val MAX_IMAGES = 100
    const val MAX_PROMPTS = 5
    const val MAX_PROMPT_CHARS = 2_000
    const val MAX_RUNS = 500
    const val MAX_REST_SECONDS = 60.0
    const val MAX_TOKENS = 4_096
    const val MAX_RUN_TIMEOUT_S = 3_600
    const val MAX_IMAGE_BYTES = 10L * 1024 * 1024
    const val MAX_IMAGE_EDGE = 4_096
    private val SHA256 = Regex("[0-9a-f]{64}")

    /** Null when the unit is acceptable, else why not. */
    fun problem(unit: WorkUnit, catalog: ModelCatalog, framework: String): String? {
        val model = catalog.find(unit.model) ?: return "Model ${unit.model} is not in this app's catalog"
        val backend = runCatching { Backend.valueOf(unit.backend) }.getOrNull() ?: return "Unknown compute unit ${unit.backend}"
        if (backend !in model.backends) return "${unit.model} does not run on ${unit.backend}"
        if (unit.framework != framework) return "This app runs ${framework}, not ${unit.framework}"
        if (unit.mode !in setOf("warm", "cold")) return "Unknown mode ${unit.mode}"
        if (unit.runs.isEmpty() || unit.runs.size > MAX_RUNS) return "A unit needs 1-$MAX_RUNS runs"
        if (unit.images.isEmpty() || unit.images.size > MAX_IMAGES) return "A unit needs 1-$MAX_IMAGES images"
        if (unit.prompts.isEmpty() || unit.prompts.size > MAX_PROMPTS) return "A unit needs 1-$MAX_PROMPTS prompts"
        if (unit.prompts.any { it.text.isBlank() || it.text.length > MAX_PROMPT_CHARS }) return "A prompt is empty or too long"
        if (unit.restSeconds !in 0.0..MAX_REST_SECONDS) return "rest_seconds is out of range"
        if (unit.maxTokens !in 1..MAX_TOKENS) return "max_tokens is out of range"
        if (unit.runTimeoutS !in 1..MAX_RUN_TIMEOUT_S) return "run_timeout_s is out of range"
        val imageIds = unit.images.map { it.imageId }.toSet()
        val promptIds = unit.prompts.map { it.promptId }.toSet()
        if (unit.runs.any { it.imageId !in imageIds || it.promptId !in promptIds }) return "A run names an unknown image or prompt"
        unit.images.forEach { image ->
            if (!image.url.startsWith("/api/v1/device/images/")) return "Image ${image.imageId} is not on this server"
            if (!SHA256.matches(image.sha256)) return "Image ${image.imageId} has no valid SHA-256"
            if (image.bytes !in 1..MAX_IMAGE_BYTES) return "Image ${image.imageId} is too large"
        }
        return null
    }
}
