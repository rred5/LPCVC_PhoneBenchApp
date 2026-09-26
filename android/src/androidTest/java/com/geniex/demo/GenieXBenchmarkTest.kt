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

/** Cold cases share one Activity; each case unloads and reloads the model. */
@RunWith(AndroidJUnit4::class)
class GenieXBenchmarkTest {
    @Test
    fun testL106() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val args = InstrumentationRegistry.getArguments()
        val imageName = args.getString("image", "L106-768.png")
        val model = decode(args, "modelBase64", "model", MODEL)
        val prompt = decode(args, "promptBase64", "prompt", PROMPT)
        val cases = args.getString("cases", "NPU:1").split(',').map { entry ->
            val parts = entry.split(':')
            require(parts.size == 2 && parts[0] in BACKENDS && parts[1].matches(Regex("[0-9]+"))) { "Invalid case: $entry" }
            Case(parts[0], parts[1])
        }
        val restMs = args.getString("restMs", "0").toLong()
        require(restMs >= 0 && imageName.matches(Regex("[A-Za-z0-9_.-]+\\.png"))) { "Invalid image or rest duration" }

        val context = instrumentation.targetContext
        val device = UiDevice.getInstance(instrumentation)
        val image = File(context.filesDir, "benchmark/$imageName")
        assertTrue("Push $imageName first", image.isFile)
        val hash = sha256(image)

        device.wakeUp()
        device.executeShellCommand("wm dismiss-keyguard")
        context.startActivity(context.packageManager.getLaunchIntentForPackage(PACKAGE)!!.apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        })
        assertTrue("App did not launch", device.wait(Until.hasObject(By.pkg(PACKAGE).depth(0)), 15_000))
        val driver = GenieXTestDriver(device)
        driver.selectModel(model, args.getString("downloadModel", "true").toBoolean())

        cases.forEachIndexed { index, case ->
            driver.load("rb_${case.backend.lowercase()}")
            resetContext(context, device, "cold-${case.backend}-${case.run}")
            val runId = "cold-${case.backend}-${case.run}"
            val resultFile = File(context.filesDir, "benchmark/results/$runId.json")
            resultFile.delete()
            sendAndAwait(context, device, image, runId, case, model, imageName, prompt)
            val result = awaitResult(resultFile)
            assertEquals("Benchmark run failed: ${result.optString("error")}", "completed", result.getString("status"))
            assertTrue("Assistant response was empty", result.getString("response").isNotBlank())
            assertEquals("Inference modified the PNG", hash, sha256(image))
            device.wait(Until.findObject(By.res(PACKAGE, "btn_unload_model")), 10_000).click()
            assertTrue("Model did not unload", device.wait(Until.gone(By.res(PACKAGE, "btn_unload_model")), 10_000))
            if (index < cases.lastIndex) android.os.SystemClock.sleep(restMs)
        }
    }

    private fun resetContext(context: android.content.Context, device: UiDevice, resetId: String) {
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
        context: android.content.Context, device: UiDevice, image: File, runId: String, case: Case,
        model: String, imageName: String, prompt: String,
    ) {
        context.sendBroadcast(Intent("$PACKAGE.BENCHMARK_IMAGE").setPackage(PACKAGE).putExtra("path", image.absolutePath))
        assertTrue("Benchmark image was not attached", device.wait(Until.hasObject(By.res(PACKAGE, "btn_remove")), 10_000))
        context.sendBroadcast(Intent("$PACKAGE.BENCHMARK_RUN_START").setPackage(PACKAGE)
            .putExtra("runId", runId).putExtra("mode", "cold")
            .putExtra("backend", case.backend).putExtra("model", model).putExtra("image", imageName).putExtra("prompt", prompt))
        device.wait(Until.findObject(By.res(PACKAGE, "et_input")), 10_000).text = prompt
        device.wait(Until.findObject(By.res(PACKAGE, "btn_send").enabled(true)), 10_000).click()
    }

    private fun awaitResult(file: File): JSONObject {
        val deadline = System.currentTimeMillis() + RESULT_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            if (file.isFile) return JSONObject(file.readText())
            android.os.SystemClock.sleep(100)
        }
        throw AssertionError("Inference did not produce a benchmark result within 600 seconds")
    }

    private fun decode(args: android.os.Bundle, encoded: String, plain: String, fallback: String): String =
        args.getString(encoded)?.let { String(android.util.Base64.decode(it, android.util.Base64.NO_WRAP), Charsets.UTF_8) }
            ?: args.getString(plain, fallback)

    private fun sha256(file: File): String = MessageDigest.getInstance("SHA-256")
        .digest(file.readBytes()).joinToString("") { "%02x".format(it) }

    private data class Case(val backend: String, val run: String)

    companion object {
        private const val PACKAGE = "com.geniex.demo"
        private const val MODEL = "Qwen3-VL-2B-Instruct (GGUF)"
        private const val PROMPT = "Can you identify if this image is real, full synthetic, or tampered? Please mask the tampered object/part if it is tampered."
        private const val RESET_TIMEOUT_MS = 15_000L
        private const val RESULT_TIMEOUT_MS = 600_000L
        private val BACKENDS = setOf("CPU", "GPU", "NPU")
    }
}
