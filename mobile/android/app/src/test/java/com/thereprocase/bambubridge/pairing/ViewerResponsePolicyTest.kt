package com.thereprocase.bambubridge.pairing

import org.junit.Assert.*
import org.junit.Test

class ViewerResponsePolicyTest {
    @Test fun expectedToolpathFailureCanFallBackToMesh() {
        assertNull(ViewerResponsePolicy.errorReason(true, 200))
        assertNull(ViewerResponsePolicy.errorReason(false, 422))
        assertNull(ViewerResponsePolicy.errorReason(false, 200))
    }

    @Test fun pageFailureAndResourceAuthenticationStillFailClosed() {
        assertEquals("unavailable", ViewerResponsePolicy.errorReason(true, 503))
        assertEquals("unavailable", ViewerResponsePolicy.errorReason(true, 422))
        for (main in listOf(false, true)) for (status in listOf(401, 403))
            assertEquals("auth", ViewerResponsePolicy.errorReason(main, status))
    }
}
