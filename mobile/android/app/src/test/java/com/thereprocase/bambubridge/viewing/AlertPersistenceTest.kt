package com.thereprocase.bambubridge.viewing

import java.io.IOException
import org.junit.Assert.*
import org.junit.Test

class AlertPersistenceTest {
    @Test fun terminalAlertRetriesAfterATemporarySaveFailure() {
        for (terminal in listOf("completed", "failed")) {
            val tracker = AlertTracker()
            tracker.updatePersisted(PrintState(true, "printing", "job")) { true }
            val state = PrintState(true, terminal, "job")
            assertThrows(IOException::class.java) {
                tracker.updatePersisted(state) { throw IOException("temporary fixture failure") }
            }
            assertEquals("printing", tracker.phase)
            assertTrue(tracker.armed)
            val expected = if (terminal == "completed") "finished" else "failed"
            assertEquals(listOf(expected), tracker.updatePersisted(state) { true })
            assertEquals(emptyList<String>(), tracker.updatePersisted(state) { true })
        }
    }

    @Test fun revokedSessionNeverCommitsOrPostsTheOldTransition() {
        val tracker = AlertTracker()
        tracker.updatePersisted(PrintState(true, "printing", "job")) { true }
        repeat(3) {
            assertEquals(emptyList<String>(), tracker.updatePersisted(PrintState(true, "completed", "job")) { false })
        }
        assertEquals("printing", tracker.phase)
        assertTrue(tracker.armed)
    }

    @Test fun recoveredSavePreservesPauseAndErrorEvents() {
        val tracker = AlertTracker()
        tracker.updatePersisted(PrintState(true, "printing", "job")) { true }
        val paused = PrintState(true, "paused", "job", setOf("fixture"))
        assertThrows(IOException::class.java) { tracker.updatePersisted(paused) { throw IOException() } }
        assertEquals(listOf("paused", "error"), tracker.updatePersisted(paused) { true })
        assertEquals(emptyList<String>(), tracker.updatePersisted(paused) { true })
    }
}
