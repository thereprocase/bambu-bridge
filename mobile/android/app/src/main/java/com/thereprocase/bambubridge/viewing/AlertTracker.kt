package com.thereprocase.bambubridge.viewing

data class PrintState(val connected: Boolean, val phase: String, val job: String, val errors: Set<String> = emptySet())

/** State survives reconnects; initial historical completion never fires an alert. */
class AlertTracker {
    var phase = ""
    var job = ""
    var armed = false
    var errors = emptySet<String>()

    /** Commit a transition only after its restart state has been saved. */
    fun updatePersisted(input: PrintState, persist: (AlertTracker) -> Boolean): List<String> {
        val candidate = AlertTracker().also {
            it.phase = phase; it.job = job; it.armed = armed; it.errors = errors
        }
        val events = candidate.update(input)
        if (!persist(candidate)) return emptyList()
        phase = candidate.phase; job = candidate.job
        armed = candidate.armed; errors = candidate.errors
        return events
    }

    fun update(input: PrintState): List<String> {
        val next = if (input.phase == "completed") input.copy(phase = "finished") else input
        if (!next.connected) return emptyList()
        val alerts = mutableListOf<String>()
        val active = next.phase in listOf("printing", "preparing", "paused")
        if (next.job.isNotEmpty() && next.job != job) {
            if (job.isNotEmpty()) { armed = false; phase = ""; errors = emptySet() }
            job = next.job
        }
        if (active) armed = true
        if (next.phase == "paused" && phase != "paused") alerts.add("paused")
        if (next.phase == "finished" && phase != "finished" && armed) {
            alerts.add("finished"); armed = false
        }
        if (next.phase == "failed" && phase != "failed" && armed) {
            alerts.add("failed"); armed = false
        }
        if ((next.errors - errors).isNotEmpty() && "failed" !in alerts) alerts.add("error")
        errors = next.errors; phase = next.phase
        return alerts
    }
}
