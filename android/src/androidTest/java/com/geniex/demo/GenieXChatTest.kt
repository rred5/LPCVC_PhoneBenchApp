// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------
package com.geniex.demo

import android.content.Context
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.UiObject2
import androidx.test.uiautomator.Until
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * On-device test for the GenieX Demo chat flow.
 *
 * The GenieX demo downloads its model at runtime, so this test drives the full
 * path on a real device: select the smallest catalog model, download it, load
 * it onto each available compute unit (NPU, GPU, CPU), send a prompt, and
 * assert a non-empty assistant response renders for each. Running on every
 * compute unit catches a model that loads on one but fails on another (e.g.
 * NPU).
 *
 * The test asserts on response presence and non-emptiness. The device must
 * have network egress to download the model; the smallest catalog entry
 * (Qwen3-0.6B GGUF, ~0.4 GB) is used to keep the download within the timeouts
 * below.
 */
@RunWith(AndroidJUnit4::class)
class GenieXChatTest {
    private val device: UiDevice =
        UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())

    private var savedHeadsUp: String? = null
    private var savedZenMode: String? = null

    @Before
    fun setUp() {
        device.wakeUp()
        device.executeShellCommand("wm dismiss-keyguard")

        // Suppress notifications so a heads-up popup can't steal a tap; restored in tearDown().
        savedHeadsUp =
            device.executeShellCommand("settings get global heads_up_notifications_enabled").trim()
        savedZenMode = device.executeShellCommand("settings get global zen_mode").trim()
        device.executeShellCommand("settings put global heads_up_notifications_enabled 0")
        device.executeShellCommand("cmd notification set_dnd priority")
        device.executeShellCommand("cmd statusbar collapse")

        val context = ApplicationProvider.getApplicationContext<Context>()
        val intent = context.packageManager.getLaunchIntentForPackage(PACKAGE)!!
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TASK)
        context.startActivity(intent)
        assertTrue(
            "App did not launch",
            device.wait(Until.hasObject(By.pkg(PACKAGE).depth(0)), LAUNCH_TIMEOUT_MS),
        )
    }

    @After
    fun tearDown() {
        // Restore the notification settings captured in setUp() so the device is left as found.
        savedHeadsUp?.takeIf { it != "null" }?.let {
            device.executeShellCommand("settings put global heads_up_notifications_enabled $it")
        }
        savedZenMode?.takeIf { it != "null" }?.let {
            device.executeShellCommand("settings put global zen_mode $it")
        }
    }

    @Test
    fun selectModelDownloadLoadAndChat() {
        GenieXTestDriver(device).selectModel(MODEL_DISPLAY_NAME)

        // 3. Load and chat on each compute unit the model offers. A GGUF model
        //    exposes NPU, GPU and CPU; exercising all three catches a model that
        //    loads on one unit but fails on another. NPU runs first since it is
        //    the most likely to surface a problem.
        for (unit in COMPUTE_UNITS) {
            loadChatAndAssertOnUnit(unit)
            unloadModel()
        }
    }

    /**
     * Loads the selected model onto [unit], sends a prompt, and asserts a
     * non-empty assistant response renders. Assumes the model is downloaded and
     * no model is currently loaded.
     */
    private fun loadChatAndAssertOnUnit(unit: ComputeUnit) {
        GenieXTestDriver(device).load(unit.radioId)

        // Send a prompt.
        val input = device.wait(Until.findObject(By.res(PACKAGE, "et_input")), UI_TIMEOUT_MS)
        assertNotNull("Input field not found", input)
        input.text = TEST_PROMPT
        device.wait(Until.findObject(By.res(PACKAGE, "btn_send")), UI_TIMEOUT_MS).click()

        // Wait for inference and assert a non-empty assistant response. After
        // sending, the chat list holds [USER prompt, ASSISTANT reply], both
        // rendered in `tv_message`; the assistant reply is the last one and must
        // differ from the prompt and be non-empty.
        var responseText: String? = null
        var waited = 0L
        while (waited < RESPONSE_TIMEOUT_MS) {
            val messages: List<UiObject2> = device.findObjects(By.res(PACKAGE, "tv_message"))
            if (messages.isNotEmpty()) {
                val last = messages.last().text
                if (!last.isNullOrBlank() && last != TEST_PROMPT) {
                    responseText = last
                    break
                }
            }
            device.waitForIdle(POLL_INTERVAL_MS)
            waited += POLL_INTERVAL_MS
        }
        assertNotNull("No assistant response on ${unit.label} within ${RESPONSE_TIMEOUT_MS / 1000}s", responseText)
        assertFalse("Assistant response on ${unit.label} is empty", responseText!!.trim().isEmpty())
    }

    /** Unloads the current model and waits for the Unload button to disappear. */
    private fun unloadModel() {
        device.wait(Until.findObject(By.res(PACKAGE, "btn_unload_model")), UI_TIMEOUT_MS).click()
        assertTrue(
            "Model did not unload within ${UI_TIMEOUT_MS / 1000}s",
            device.wait(Until.gone(By.res(PACKAGE, "btn_unload_model")), UI_TIMEOUT_MS),
        )
    }

    /** A compute unit offered in the load dialog, identified by its radio id. */
    private data class ComputeUnit(
        val label: String,
        val radioId: String,
    )

    companion object {
        private const val PACKAGE = "com.geniex.demo"

        // Smallest entry in src/main/assets/model_list.json — keeps the runtime
        // download small enough for CI timeouts.
        private const val MODEL_DISPLAY_NAME = "Qwen3-0.6B (GGUF)"
        private const val TEST_PROMPT = "What is gravity? Answer in under 30 words."

        // Compute units the GGUF model offers, NPU first (most likely to fail).
        private val COMPUTE_UNITS =
            listOf(
                ComputeUnit("NPU", "rb_npu"),
                ComputeUnit("GPU", "rb_gpu"),
                ComputeUnit("CPU", "rb_cpu"),
            )

        private const val LAUNCH_TIMEOUT_MS = 15_000L
        private const val UI_TIMEOUT_MS = 10_000L
        private const val RESPONSE_TIMEOUT_MS = 120_000L // first non-empty generation
        private const val POLL_INTERVAL_MS = 2_000L
    }
}
