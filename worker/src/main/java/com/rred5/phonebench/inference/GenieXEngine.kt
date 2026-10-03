package com.rred5.phonebench.inference

import android.content.Context
import android.os.SystemClock
import android.util.Log
import com.geniex.sdk.GenieXSdk
import com.geniex.sdk.ModelManagerWrapper
import com.geniex.sdk.VlmWrapper
import com.geniex.sdk.bean.ComputeUnitValue
import com.geniex.sdk.bean.GenerationConfig
import com.geniex.sdk.bean.HubSource
import com.geniex.sdk.bean.LlmStreamResult
import com.geniex.sdk.bean.ModelConfig
import com.geniex.sdk.bean.ModelPullInput
import com.geniex.sdk.bean.VlmChatMessage
import com.geniex.sdk.bean.VlmContent
import com.geniex.sdk.bean.VlmCreateInput
import java.io.File

/**
 * Runs vision models with the Qualcomm GenieX SDK.
 *
 * Load settings and the generation call follow the GenieX demo app's VLM path
 * (android/ in this repo), which produced the 2026-09-18 baselines.
 */
class GenieXEngine(
    context: Context,
    private val catalog: ModelCatalog,
) : InferenceEngine {
    override val framework = "geniex"
    override val frameworkVersion = SDK_VERSION

    // Read by stop() from another coroutine while run() is generating.
    @Volatile private var wrapper: VlmWrapper? = null

    @Volatile override var loaded: Pair<String, Backend>? = null
        private set

    init {
        // Idempotent: the SDK guards its own plugin registration.
        GenieXSdk.getInstance().init(context.applicationContext)
    }

    private fun model(id: String): CatalogModel = catalog.find(id) ?: error("Model $id is not in this app's catalog")

    override suspend fun downloadedModels(): List<String> =
        catalog.models.filter { ModelManagerWrapper.getPaths(it.modelName) != null }.map { it.id }

    override suspend fun ensureDownloaded(modelId: String, onProgress: (percent: Int) -> Unit) {
        val model = model(modelId)
        if (ModelManagerWrapper.getPaths(model.modelName) != null) return
        val input =
            ModelPullInput(
                model_name = model.modelName,
                precision = model.quant,
                hub = runCatching { HubSource.valueOf(model.hub) }.getOrDefault(HubSource.AUTO),
                chipset = model.chipset,
            )
        var failure: String? = null
        ModelManagerWrapper.pullFlow(input).collect { event ->
            when (event) {
                is ModelManagerWrapper.PullEvent.Progress -> {
                    val total = event.files.sumOf { if (it.total_bytes > 0) it.total_bytes else 0L }
                    val done = event.files.sumOf { it.downloaded_bytes }
                    onProgress(if (total > 0) ((done * 100) / total).toInt() else 0)
                }
                is ModelManagerWrapper.PullEvent.Completed -> onProgress(100)
                is ModelManagerWrapper.PullEvent.Error -> failure = "Download failed (${event.code}): ${event.message}"
            }
        }
        failure?.let { error(it) }
        checkNotNull(ModelManagerWrapper.getPaths(model.modelName)) { "Download of $modelId did not complete" }
    }

    override suspend fun load(modelId: String, backend: Backend): LoadResult {
        val model = model(modelId)
        require(backend in model.backends) { "$modelId does not run on $backend" }
        unload()
        val started = SystemClock.elapsedRealtime()
        val paths = ModelManagerWrapper.getPaths(model.modelName) ?: error("$modelId is not downloaded")
        // A manifest-written runtime wins; GGUF models without one use the catalog's.
        val runtime = paths.runtime_id.ifEmpty { model.runtime }
        val vision = paths.mmproj_path?.takeIf { it.isNotEmpty() }?.let { GgufVisionReader.read(File(it)) }
        val config =
            if (runtime == "qairt") {
                // QAIRT rejects non-zero n_ctx / n_gpu_layers; both are fixed in the bundle.
                ModelConfig(nCtx = 0, nGpuLayers = 0, nThreads = 8, enable_thinking = false)
            } else {
                ModelConfig(
                    nCtx = vlmContextSize(vision),
                    nThreads = 4,
                    nBatch = 1,
                    nUBatch = 1,
                    nGpuLayers = if (backend == Backend.CPU) 0 else 999,
                    enable_thinking = false,
                )
            }
        val computeUnit =
            when {
                runtime == "qairt" -> "HTP0"
                backend == Backend.CPU -> ComputeUnitValue.CPU.value
                backend == Backend.GPU -> ComputeUnitValue.GPU.value
                else -> ComputeUnitValue.NPU.value
            }
        wrapper =
            VlmWrapper
                .builder()
                .vlmCreateInput(
                    VlmCreateInput(
                        model_name = paths.model_name,
                        model_path = paths.model_path,
                        mmproj_path = paths.mmproj_path,
                        config = config,
                        runtime_id = runtime,
                        compute_unit = computeUnit,
                    ),
                ).build()
                .getOrThrow()
        loaded = modelId to backend
        val elapsed = SystemClock.elapsedRealtime() - started
        Log.i(TAG, "loaded $modelId on $backend ($runtime) in ${elapsed}ms")
        return LoadResult(runtime, elapsed)
    }

    override suspend fun reset() {
        val code = checkNotNull(wrapper) { "No model loaded" }.reset()
        check(code == 0) { "Context reset failed with code $code" }
    }

    override suspend fun run(imagePath: String, prompt: String, maxTokens: Int): RunOutput {
        val vlm = checkNotNull(wrapper) { "No model loaded" }
        // Same content order as the demo app: image first, then text.
        val message = VlmChatMessage(role = "user", contents = listOf(VlmContent("image", imagePath), VlmContent("text", prompt)))
        val template = vlm.applyChatTemplate(arrayOf(message), null, false).getOrThrow()
        val config =
            vlm.injectMediaPathsToConfig(
                arrayOf(message),
                GenerationConfig(
                    maxTokens = maxTokens,
                    stopWords = null,
                    stopCount = 0,
                    nPast = 0,
                    imagePaths = null,
                    imageCount = 0,
                    audioPaths = null,
                    audioCount = 0,
                ),
            )
        val response = StringBuilder()
        var output: RunOutput? = null
        vlm.generateStreamFlow(template.formattedText, config).collect { result ->
            when (result) {
                is LlmStreamResult.Token -> response.append(result.text)
                is LlmStreamResult.Completed -> {
                    val profile = result.profile
                    output =
                        RunOutput(
                            response = response.toString(),
                            ttftMs = profile.ttftMs,
                            promptTokens = profile.promptTokens,
                            generatedTokens = profile.generatedTokens,
                            prefillTokS = profile.prefillSpeed,
                            decodeTokS = profile.decodingSpeed,
                            stopReason = profile.stopReason,
                        )
                }
                is LlmStreamResult.Error -> throw result.throwable
            }
        }
        return output ?: error("Generation ended without a result")
    }

    override suspend fun stop() {
        wrapper?.stopStream()
    }

    override fun unload() {
        wrapper?.let {
            it.destroy()
            Log.i(TAG, "unloaded ${loaded?.first} from ${loaded?.second}")
        }
        wrapper = null
        loaded = null
    }

    companion object {
        private const val TAG = "PhoneBench"
        const val SDK_VERSION = "0.3.5"

        /** Room for an image, its answer, and a follow-up turn, over the image cost. */
        private const val VLM_CTX_HEADROOM = 2048

        /** nCtx must be at least this regardless of image cost. */
        private const val VLM_MIN_CTX = 4096

        /** The demo app's context sizing: one image's tokens plus headroom, as a power of two. */
        private fun vlmContextSize(vision: GgufVisionConfig?): Int {
            val needed = (vision?.tokenCount ?: 0) + VLM_CTX_HEADROOM
            var ctx = VLM_MIN_CTX
            while (ctx < needed) ctx *= 2
            return ctx
        }
    }
}
