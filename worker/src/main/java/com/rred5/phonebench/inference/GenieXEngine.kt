package com.rred5.phonebench.inference

import android.content.Context
import android.os.SystemClock
import android.system.Os
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
        // Without this the SDK pulls AI Hub's latest release, whose bundles can need a newer
        // QAIRT than the SDK ships. Set before init, which loads the native library.
        Os.setenv("GENIEX_AIHUBVERSION", AIHUB_RELEASE, true)
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

    override suspend fun importModel(modelId: String, archive: File) {
        val model = model(modelId)
        if (ModelManagerWrapper.getPaths(model.modelName) != null) ModelManagerWrapper.remove(model.modelName)
        var failure: String? = null
        val input =
            ModelPullInput(
                model_name = model.modelName,
                precision = model.quant,
                hub = HubSource.LOCALFS,
                local_path = archive.absolutePath,
                chipset = model.chipset,
            )
        ModelManagerWrapper.pullFlow(input).collect { event ->
            if (event is ModelManagerWrapper.PullEvent.Error) failure = "Import failed (${event.code}): ${event.message}"
        }
        failure?.let { error(it) }
        checkNotNull(ModelManagerWrapper.getPaths(model.modelName)) { "Import of $modelId did not register the model" }
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
                ModelConfig(nCtx = 0, nGpuLayers = 0, nThreads = 8)
            } else {
                ModelConfig(
                    nCtx = vlmContextSize(vision),
                    nThreads = 4,
                    nBatch = 1,
                    nUBatch = 1,
                    nGpuLayers = if (backend == Backend.CPU) 0 else 999,
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

    override suspend fun run(imagePath: String, turns: List<String>, maxTokens: Int): List<RunOutput> {
        val vlm = checkNotNull(wrapper) { "No model loaded" }
        // Follow-up turns continue the conversation as the demo app's chat does: the template
        // covers the whole history, and the assistant's replies are part of it.
        val history = mutableListOf<VlmChatMessage>()
        return turns.mapIndexed { index, text ->
            // Same content order as the demo app: image first, then text. Only the first turn has the image.
            val contents = if (index == 0) listOf(VlmContent("image", imagePath), VlmContent("text", text)) else listOf(VlmContent("text", text))
            val message = VlmChatMessage(role = "user", contents = contents)
            history += message
            val output = generate(vlm, history, message, maxTokens)
            history += VlmChatMessage(role = "assistant", contents = listOf(VlmContent("text", output.response)))
            output
        }
    }

    private suspend fun generate(vlm: VlmWrapper, history: List<VlmChatMessage>, message: VlmChatMessage, maxTokens: Int): RunOutput {
        val template = vlm.applyChatTemplate(history.toTypedArray(), null, false).getOrThrow()
        // Only the new message's media: the SDK tokenizes the conversation incrementally.
        val config =
            vlm.injectMediaPathsToConfig(
                arrayOf(message),
                GenerationConfig(
                    maxTokens = maxTokens,
                    stopWords = null,
                    stopCount = 0,
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
        const val SDK_VERSION = "0.8.0"

        /**
         * The AI Hub release qairt models are pulled from. 0.64.0 bundles are built with
         * QAIRT 2.45, the runtime SDK 0.8.0 ships; it is also the last release with SM8850
         * bundles for Qwen3-VL-8B and Gemma-4-E4B. Changing it changes which bundle a
         * model downloads, so already-downloaded models keep the old one.
         */
        const val AIHUB_RELEASE = "v0.64.0"

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
