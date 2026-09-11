package com.thereprocase.bambubridge.viewing

import okio.Buffer
import org.junit.Assert.*
import org.junit.Test

class ViewingTest {
    private fun state(phase: String, job: String = "one", errors: Set<String> = emptySet(), connected: Boolean = true) =
        PrintState(connected, phase, job, errors)
    @Test fun historicalCompletionDoesNotAlertButObservedPrintDoes() {
        val t = AlertTracker()
        assertEquals(emptyList<String>(), t.update(state("finished")))
        assertEquals(emptyList<String>(), t.update(state("printing")))
        assertEquals(listOf("finished"), t.update(state("completed")))
        assertEquals(emptyList<String>(), t.update(state("finished")))
    }
    @Test fun pausesDeduplicateAcrossReconnectAndRearmAfterResume() {
        val t = AlertTracker()
        assertEquals(listOf("paused"), t.update(state("paused")))
        assertEquals(emptyList<String>(), t.update(state("paused", connected=false)))
        assertEquals(emptyList<String>(), t.update(state("paused")))
        t.update(state("printing"))
        assertEquals(listOf("paused"), t.update(state("paused")))
    }
    @Test fun errorsAndFailedPrintsAreDistinctAndRepeatOnlyAfterRecovery() {
        val t = AlertTracker()
        assertEquals(listOf("error"), t.update(state("printing", errors=setOf("x"))))
        assertEquals(emptyList<String>(), t.update(state("printing", errors=setOf("x"))))
        assertEquals(listOf("failed"), t.update(state("failed", errors=setOf("x", "y"))))
        t.update(state("printing", "two"))
        assertEquals(listOf("error"), t.update(state("printing", "two", setOf("x"))))
    }
    @Test fun replacementJobDoesNotInheritCompletion() {
        val t = AlertTracker(); t.update(state("printing"))
        assertEquals(emptyList<String>(), t.update(state("finished", "unobserved")))
    }
    @Test fun lateJobNameDoesNotRepeatPauseAlert() {
        val t = AlertTracker()
        assertEquals(listOf("paused"), t.update(state("paused", "")))
        assertEquals(emptyList<String>(), t.update(state("paused", "late name")))
    }
    @Test fun readsCoalescedFramesWithoutWaitingForEndOfStream() {
        val b = Buffer()
        repeat(3) { b.writeUtf8("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n")
            .write(byteArrayOf(-1,-40,-1,-39)).writeUtf8("\r\n") }
        val reader = MjpegReader(b)
        repeat(3) { assertArrayEquals(byteArrayOf(-1,-40,-1,-39), reader.next()) }
    }
    @Test fun rejectsMalformedOversizedTruncatedAndNonJpegParts() {
        for (part in listOf("--x\r\nContent-Length: 9000000\r\n\r\n", "--x\r\nContent-Length: 4\r\n\r\nnope",
                "--x\r\nContent-Length: 4\r\nContent-Length: 4\r\n\r\n", "--x\r\nContent-Length: 5\r\n\r\nx",
                "--x\r\n" + "x".repeat(5000))) {
            assertThrows(Exception::class.java) { MjpegReader(Buffer().writeUtf8(part)).next() }
        }
    }
}
