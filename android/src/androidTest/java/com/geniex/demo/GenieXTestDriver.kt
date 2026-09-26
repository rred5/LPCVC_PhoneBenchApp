package com.geniex.demo

import androidx.test.uiautomator.By
import androidx.test.uiautomator.StaleObjectException
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.UiScrollable
import androidx.test.uiautomator.UiSelector
import androidx.test.uiautomator.Until
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue

/** Shared model selection/loading steps used by the chat and benchmark tests. */
internal class GenieXTestDriver(private val device: UiDevice) {
    fun selectModel(modelDisplayName: String, downloadModel: Boolean = true) {
        // Preserve an already-selected model; otherwise find it in the catalog.
        val spinner = device.wait(Until.findObject(By.res(PACKAGE, "sp_model_list")), LAUNCH_TIMEOUT_MS)
        assertNotNull("Model spinner not found", spinner)
        // The selected model's displayName is rendered in a tv_model_id child.
        // If it is not already showing the target, open the dropdown and pick it.
        if (!device.hasObject(By.text(modelDisplayName))) {
            selectModelFromDropdown(modelDisplayName)
        }
        // Confirm the intended model is the active selection before proceeding.
        assertTrue(
            "Model '$modelDisplayName' is not the active selection",
            device.wait(Until.hasObject(By.text(modelDisplayName)), UI_TIMEOUT_MS),
        )

        if (!downloadModel) return
        // Download only when this benchmark cycle needs it. The download panel appears
        //    while the pull runs and is hidden on completion. Wait for it to
        //    appear, then to disappear, with a generous network timeout.
        device.wait(Until.findObject(By.res(PACKAGE, "btn_download")), UI_TIMEOUT_MS).click()
        // It may already be cached from a prior run — only wait for the panel
        // to clear if it actually showed up.
        device.wait(Until.hasObject(By.res(PACKAGE, "ll_downloading")), UI_TIMEOUT_MS)
        assertTrue(
            "Model download did not complete within ${DOWNLOAD_TIMEOUT_MS / 1000}s",
            device.wait(Until.gone(By.res(PACKAGE, "ll_downloading")), DOWNLOAD_TIMEOUT_MS),
        )
    }

    fun load(radioId: String) {
        device.wait(Until.findObject(By.res(PACKAGE, "btn_load_model")), UI_TIMEOUT_MS).click()

        // The compute-unit picker appears for multi-unit (GGUF) models. Select
        // the radio for this unit and confirm. GPU reveals an n-layers field
        // that defaults to 999, so no extra input is needed.
        val radio = device.wait(Until.findObject(By.res(PACKAGE, radioId)), UI_TIMEOUT_MS)
        assertNotNull("Compute-unit radio '${radioId}' not found in load dialog", radio)
        radio.click()
        val okButton = device.wait(Until.findObject(By.text("OK")), UI_TIMEOUT_MS)
        assertNotNull("Load dialog OK button not found", okButton)
        okButton.click()

        // Load success reveals the Unload button (gone until a model loads).
        assertTrue(
            "Model did not load on ${radioId} within ${LOAD_TIMEOUT_MS / 1000}s",
            device.wait(Until.hasObject(By.res(PACKAGE, "btn_unload_model")), LOAD_TIMEOUT_MS),
        )
    }

    private fun selectModelFromDropdown(modelDisplayName: String) {
        repeat(DROPDOWN_RETRIES) { attempt ->
            try {
                device.wait(Until.findObject(By.res(PACKAGE, "sp_model_list")), UI_TIMEOUT_MS).click()
                UiScrollable(
                    UiSelector().scrollable(true),
                ).scrollIntoView(UiSelector().text(modelDisplayName))
                val option = device.wait(Until.findObject(By.text(modelDisplayName)), UI_TIMEOUT_MS)
                assertNotNull("Model '$modelDisplayName' not in catalog dropdown", option)
                option.click()
                return
            } catch (e: StaleObjectException) {
                if (attempt == DROPDOWN_RETRIES - 1) throw e
                device.waitForIdle(POLL_INTERVAL_MS)
            }
        }
    }

    companion object {
        private const val PACKAGE = "com.geniex.demo"
        private const val LAUNCH_TIMEOUT_MS = 15_000L
        private const val UI_TIMEOUT_MS = 10_000L
        private const val DOWNLOAD_TIMEOUT_MS = 600_000L
        private const val LOAD_TIMEOUT_MS = 120_000L
        private const val POLL_INTERVAL_MS = 2_000L
        private const val DROPDOWN_RETRIES = 3
    }
}
