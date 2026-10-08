package com.geniex.demo

import android.content.Intent
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * Loads the model once, then runs each image of a series as two turns of one conversation:
 * the image and stage 1 prompt, then the stage 2 prompt with no image and no reset in between.
 * The series file (files/benchmark/series.json) is written by run_two_stage_series.py.
 */
@RunWith(AndroidJUnit4::class)
class GenieXTwoStageSeriesTest {
    @Test
    fun testSeries() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val args = InstrumentationRegistry.getArguments()
        val context = instrumentation.targetContext
        val device = UiDevice.getInstance(instrumentation)

        val seriesName = args.getString("series", "series.json")
        require(seriesName.matches(Regex("[A-Za-z0-9_.-]+\\.json"))) { "series must be a JSON filename" }
        val seriesFile = File(context.filesDir, "benchmark/$seriesName")
        assertTrue("Push $seriesName first", seriesFile.isFile)
        val series = JSONObject(seriesFile.readText())
        val backend = series.getString("backend").uppercase()
        require(backend in listOf("CPU", "GPU", "NPU")) { "Unsupported backend: $backend" }
        val model = series.getString("model")
        val stage1Prompt = series.getString("stage1")
        val restMs = series.optLong("rest_ms", 0L)
        val maxTokens = series.getJSONObject("max_tokens")
        val items = series.getJSONArray("items")
        val start = args.getString("start", "1").toInt()
        require(items.length() > 0 && restMs >= 0 && start >= 1) { "Invalid series" }
        for (i in 0 until items.length()) {
            val item = items.getJSONObject(i)
            require(item.getString("image").matches(Regex("[A-Za-z0-9_.-]+\\.png"))) { "image must be a PNG filename" }
            assertTrue("Push ${item.getString("image")} first", File(context.filesDir, "benchmark/${item.getString("image")}").isFile)
        }

        device.wakeUp()
        device.executeShellCommand("wm dismiss-keyguard")
        context.startActivity(context.packageManager.getLaunchIntentForPackage(PACKAGE)!!.apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        })
        assertTrue("App did not launch", device.wait(Until.hasObject(By.pkg(PACKAGE).depth(0)), 15_000))
        GenieXTestDriver(device).apply {
            selectModel(model, args.getString("downloadModel", "true").toBoolean())
            load("rb_${backend.lowercase()}")
        }

        // Excluded warm-up: both turns on the first image, so the second-turn path is warm too.
        val first = items.getJSONObject(0)
        runConversation(context, device, "warmup", first, backend, model, stage1Prompt, maxTokens)

        for (i in 0 until items.length()) {
            val item = items.getJSONObject(i)
            val index = item.getInt("index")
            if (index < start) continue
            runConversation(context, device, "s%04d".format(index), item, backend, model, stage1Prompt, maxTokens)
            if (i < items.length() - 1) android.os.SystemClock.sleep(restMs)
        }
        device.wait(Until.findObject(By.res(PACKAGE, "btn_unload_model")), 10_000).click()
        assertTrue("Model did not unload", device.wait(Until.gone(By.res(PACKAGE, "btn_unload_model")), 10_000))
    }

    /** One image: reset, stage 1 with the image, then stage 2 as a follow-up. Results land as <id>-1/-2.json. */
    private fun runConversation(
        context: android.content.Context, device: UiDevice, id: String, item: JSONObject,
        backend: String, model: String, stage1Prompt: String, maxTokens: JSONObject,
    ) {
        val imageName = item.getString("image")
        resetContext(context, device, id)
        context.sendBroadcast(Intent("$PACKAGE.BENCHMARK_IMAGE").setPackage(PACKAGE)
            .putExtra("path", File(context.filesDir, "benchmark/$imageName").absolutePath))
        assertTrue("Benchmark image was not attached", device.wait(Until.hasObject(By.res(PACKAGE, "btn_remove")), 10_000))
        val first = sendAndAwait(context, device, "$id-1", imageName, backend, model, stage1Prompt, maxTokens.getInt("stage1"))
        // Stage 2 always runs. If stage 1 failed the conversation is unreliable, so skip it and let
        // the runner record the missing stage rather than a misleading answer.
        if (first.getString("status") != "completed") return
        sendAndAwait(context, device, "$id-2", imageName, backend, model, item.getString("stage2"), maxTokens.getInt("stage2"))
    }

    private fun sendAndAwait(
        context: android.content.Context, device: UiDevice, runId: String, imageName: String,
        backend: String, model: String, prompt: String, maxTokens: Int,
    ): JSONObject {
        val resultFile = File(context.filesDir, "benchmark/results/$runId.json")
        resultFile.delete()
        context.sendBroadcast(Intent("$PACKAGE.BENCHMARK_RUN_START").setPackage(PACKAGE)
            .putExtra("runId", runId).putExtra("mode", "two-stage-same-conversation")
            .putExtra("backend", backend).putExtra("model", model).putExtra("image", imageName)
            .putExtra("prompt", prompt).putExtra("maxTokens", maxTokens))
        device.wait(Until.findObject(By.res(PACKAGE, "et_input")), 10_000).text = prompt
        device.wait(Until.findObject(By.res(PACKAGE, "btn_send").enabled(true)), 30_000).click()
        val deadline = System.currentTimeMillis() + RESULT_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            if (resultFile.isFile) return JSONObject(resultFile.readText())
            android.os.SystemClock.sleep(100)
        }
        throw AssertionError("Inference did not produce a benchmark result within ${RESULT_TIMEOUT_MS / 1000} seconds: $runId")
    }

    private fun resetContext(context: android.content.Context, device: UiDevice, resetId: String) {
        // Marker confirms reset finished before the next prompt begins.
        device.executeShellCommand("run-as $PACKAGE rm -f files/benchmark/reset-$resetId.done")
        context.sendBroadcast(Intent("$PACKAGE.BENCHMARK_RESET").setPackage(PACKAGE).putExtra("resetId", resetId))
        val marker = File(context.filesDir, "benchmark/reset-$resetId.done")
        val deadline = System.currentTimeMillis() + RESET_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            if (marker.isFile) return
            android.os.SystemClock.sleep(100)
        }
        throw AssertionError("Native VLM context did not confirm reset for $resetId")
    }

    companion object {
        private const val PACKAGE = "com.geniex.demo"
        private const val RESET_TIMEOUT_MS = 15_000L
        private const val RESULT_TIMEOUT_MS = 600_000L
    }
}
