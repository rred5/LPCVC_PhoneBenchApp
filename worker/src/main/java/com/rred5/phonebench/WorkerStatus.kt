package com.rred5.phonebench

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update

/** What the worker is doing, as reported to the server. */
enum class Phase(val apiState: String) {
    STOPPED("idle"),
    IDLE("idle"),
    DOWNLOADING("downloading"),
    RUNNING("running"),
    PAUSED("paused"),
    ERROR("error"),
}

/** The worker's live state; the notification and app screen show it. */
data class Status(
    val phase: Phase = Phase.STOPPED,
    val message: String = "Not started",
    val unitId: String? = null,
    val jobName: String? = null,
    val submittedBy: String? = null,
    val model: String? = null,
    val backend: String? = null,
    val runIndex: Int = 0,
    val runTotal: Int = 0,
    val downloadPercent: Int? = null,
    val serverReachable: Boolean = false,
    val lastError: String? = null,
)

object WorkerStatus {
    private val state = MutableStateFlow(Status())
    val flow: StateFlow<Status> = state
    val current: Status get() = state.value

    fun set(transform: (Status) -> Status) = state.update(transform)
}
