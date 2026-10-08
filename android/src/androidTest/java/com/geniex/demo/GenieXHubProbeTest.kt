package com.geniex.demo

import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import com.geniex.sdk.GenieXSdk
import com.geniex.sdk.ModelManagerWrapper
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.runBlocking
import org.junit.Test

/**
 * Logs what the GenieX SDK says about this phone and the AI Hub: the chipset names it
 * accepts, the one it detects, and which Qwen models the hub lists. Read the result with
 * `adb logcat -s HubProbe`. It changes nothing on the phone.
 */
class GenieXHubProbeTest {
    @Test
    fun logHubAndChipsets() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val ready = CountDownLatch(1)
        GenieXSdk.getInstance().init(
            context,
            object : GenieXSdk.InitCallback {
                override fun onSuccess() = ready.countDown()

                override fun onFailure(reason: String) {
                    Log.e(TAG, "init failed: $reason")
                    ready.countDown()
                }
            },
        )
        ready.await(60, TimeUnit.SECONDS)
        runBlocking {
            val chipsets = runCatching { ModelManagerWrapper.listChipsets() }
            Log.i(TAG, "chipsets: " + chipsets.map { list -> list.joinToString("; ") { "${it.name} aliases=${it.aliases.joinToString("/")}" } })
            Log.i(TAG, "detected(true): " + runCatching { ModelManagerWrapper.detectChipset(true) })
            Log.i(TAG, "detected(false): " + runCatching { ModelManagerWrapper.detectChipset(false) })
            for (query in listOf("", "aihub", "AIHUB", "Qwen3-VL")) {
                val models = runCatching { ModelManagerWrapper.listHubModels(query) }
                val lines = models.map { list -> list.map { it.toString() } }
                Log.i(TAG, "hub[$query]: " + lines.map { "${it.size} models" } + " " + lines.map { all -> all.filter { it.contains("VL", ignoreCase = true) }.take(12) })
            }
        }
    }

    private companion object {
        const val TAG = "HubProbe"
    }
}
