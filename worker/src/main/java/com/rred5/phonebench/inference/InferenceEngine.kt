package com.rred5.phonebench.inference

/** A compute unit a job can ask for. */
enum class Backend { CPU, GPU, NPU }

data class LoadResult(
    /** The runtime that loaded the model, e.g. `llama_cpp` or `qairt`. */
    val runtime: String,
    val loadMs: Long,
)

/** One finished generation and the framework's own profile of it. */
data class RunOutput(
    val response: String,
    val ttftMs: Double,
    val promptTokens: Long,
    val generatedTokens: Long,
    val prefillTokS: Double,
    val decodeTokS: Double,
    val stopReason: String,
)

/**
 * The seam between the worker and whatever runs the models (docs/DESIGN.md,
 * "Inference seam"). Polling, the job protocol and the status screen only use
 * this interface, so a later framework is a second implementation.
 *
 * Not thread-safe: the worker runs one unit at a time. [stop] is the exception
 * and may be called from another coroutine to end a generation early.
 */
interface InferenceEngine {
    val framework: String
    val frameworkVersion: String

    /** The model and compute unit currently loaded, if any. */
    val loaded: Pair<String, Backend>?

    /** Catalog ids of the models already on the phone. */
    suspend fun downloadedModels(): List<String>

    /** Download [modelId] unless it is already on the phone. */
    suspend fun ensureDownloaded(modelId: String, onProgress: (percent: Int) -> Unit)

    /** Install [modelId] from a bundle already on the phone, replacing any copy. */
    suspend fun importModel(modelId: String, archive: java.io.File)

    /** Load [modelId] on [backend], replacing whatever is loaded. */
    suspend fun load(modelId: String, backend: Backend): LoadResult

    /** Clear the context so the next run starts fresh. */
    suspend fun reset()

    /**
     * Run one conversation about an image: each turn in order, the image with the first.
     * Returns one output per turn. Throws if any turn fails.
     */
    suspend fun run(imagePath: String, turns: List<String>, maxTokens: Int): List<RunOutput>

    /** End an in-flight generation; safe to call from another coroutine. */
    suspend fun stop()

    fun unload()
}
