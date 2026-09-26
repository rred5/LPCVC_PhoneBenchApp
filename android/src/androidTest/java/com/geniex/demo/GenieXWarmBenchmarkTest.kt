package com.geniex.demo

import android.content.Intent
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.security.MessageDigest

/** Loads once, warms up once, then resets VLM context before each measured prompt. */
@RunWith(AndroidJUnit4::class)
class GenieXWarmBenchmarkTest {
    @Test
    fun testWarmBackend() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val args = InstrumentationRegistry.getArguments()
        val backend = args.getString("backend", "NPU").uppercase()
        val repetitions = args.getString("repetitions", "1").toInt()
        val restMs = args.getString("restMs", "0").toLong()
        val imageName = args.getString("image", "L106-768.png")
        val model = args.getString("modelBase64")?.let {
            String(android.util.Base64.decode(it, android.util.Base64.NO_WRAP), Charsets.UTF_8)
        } ?: args.getString("model", MODEL)
        val prompt = args.getString("promptBase64")?.let {
            String(android.util.Base64.decode(it, android.util.Base64.NO_WRAP), Charsets.UTF_8)
        } ?: args.getString("prompt", PROMPT)
        require(backend in listOf("CPU", "GPU", "NPU")) { "Unsupported backend: $backend" }
        require(repetitions > 0 && restMs >= 0) { "Invalid repetitions or rest duration" }
        require(imageName.matches(Regex("[A-Za-z0-9_.-]+\\.png"))) { "image must be a PNG filename" }

        val context = instrumentation.targetContext
        val device = UiDevice.getInstance(instrumentation)
        val image = File(context.filesDir, "benchmark/$imageName")
        assertTrue("Push $imageName first", image.isFile)
        val hash = sha256(image)

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

        // Excluded warm-up fills model and runtime caches.
        resetContext(context, device, "warmup-$backend")
        sendAndAwait(context, device, image, null, backend, model, imageName, prompt)
        for (run in 1..repetitions) {
            resetContext(context, device, "$backend-$run")
            val runId = "warm-$backend-$run"
            val resultFile = File(context.filesDir, "benchmark/results/$runId.json")
            resultFile.delete()
            sendAndAwait(context, device, image, runId, backend, model, imageName, prompt)
            val result = awaitResult(resultFile)
            assertEquals("Benchmark run failed: ${result.optString("error")}", "completed", result.getString("status"))
            assertTrue("Assistant response was empty", result.getString("response").isNotBlank())
            assertTrue("Profile was missing", result.getDouble("ttft_ms") >= 0)
            assertEquals("Inference modified the PNG", hash, sha256(image))
            println(result.toString())
            if (run < repetitions) android.os.SystemClock.sleep(restMs)
        }
        device.wait(Until.findObject(By.res(PACKAGE, "btn_unload_model")), 10_000).click()
        assertTrue("Model did not unload", device.wait(Until.gone(By.res(PACKAGE, "btn_unload_model")), 10_000))
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

    private fun sendAndAwait(
        context: android.content.Context, device: UiDevice, image: File, runId: String?, backend: String, model: String, imageName: String, prompt: String,
    ) {
        context.sendBroadcast(Intent("$PACKAGE.BENCHMARK_IMAGE").setPackage(PACKAGE).putExtra("path", image.absolutePath))
        assertTrue("Benchmark image was not attached", device.wait(Until.hasObject(By.res(PACKAGE, "btn_remove")), 10_000))
        if (runId != null) {
            // Only measured prompts receive a persisted result ID.
            context.sendBroadcast(Intent("$PACKAGE.BENCHMARK_RUN_START").setPackage(PACKAGE)
                .putExtra("runId", runId).putExtra("mode", "warm-context-reset")
                .putExtra("backend", backend).putExtra("model", model).putExtra("image", imageName).putExtra("prompt", prompt))
        }
        device.wait(Until.findObject(By.res(PACKAGE, "et_input")), 10_000).text = prompt
        device.wait(Until.findObject(By.res(PACKAGE, "btn_send").enabled(true)), 10_000).click()
        if (runId == null) {
            assertTrue("Warm-up did not finish", device.wait(Until.hasObject(By.text(java.util.regex.Pattern.compile("(?s)^(TTFT:|Error:).*"))), 600_000))
            assertTrue("Warm-up failed", device.findObject(By.textStartsWith("Error:")) == null)
        }
    }

    private fun awaitResult(file: File): JSONObject {
        val deadline = System.currentTimeMillis() + RESULT_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            if (file.isFile) return JSONObject(file.readText())
            android.os.SystemClock.sleep(100)
        }
        throw AssertionError("Inference did not produce a benchmark result within 600 seconds")
    }

    private fun sha256(file: File): String = MessageDigest.getInstance("SHA-256")
        .digest(file.readBytes()).joinToString("") { "%02x".format(it) }

    companion object {
        private const val PACKAGE = "com.geniex.demo"
        private const val MODEL = "Qwen3-VL-2B-Instruct (GGUF)"
        private const val PROMPT = "Can you identify if this image is real, full synthetic, or tampered? Please mask the tampered object/part if it is tampered."
        private const val RESET_TIMEOUT_MS = 15_000L
        private const val RESULT_TIMEOUT_MS = 600_000L
    }
}
