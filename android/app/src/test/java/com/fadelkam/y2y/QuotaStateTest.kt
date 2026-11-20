package com.fadelkam.y2y

import org.junit.Assert.assertEquals
import org.junit.Test

/** The never-backwards rule (QuotaState.newest): seen 2026-11-20 14:26, a cached old /state replaced a newer push. */
class QuotaStateTest {
    private fun reading(mb: Double, at: String, alive: Long? = null, state: String = "ok") =
        QuotaState(remainingMb = mb, totalMb = 6250.0, fetchedAt = at, state = state, heartbeatAtMs = alive)

    private val newer = reading(3048.73, "2026-11-20T12:23:52+00:00", alive = 1_000)
    private val older = reading(3065.43, "2026-11-20T12:21:37+00:00", alive = 2_000)

    @Test fun anOlderCachedReadingDoesNotReplaceANewerOne() {
        val got = QuotaState.newest(newer, older)
        assertEquals(3048.73, got.remainingMb!!, 0.0)
        assertEquals(2_000L, got.heartbeatAtMs)          // but the latest sign of life is kept
    }

    @Test fun aNewerReadingReplacesAnOlderOne() =
        assertEquals(3048.73, QuotaState.newest(older, newer).remainingMb!!, 0.0)

    @Test fun sameReadingKeepsTheFresherStatus() {
        val current = reading(3048.73, "2026-11-20T12:23:52+00:00", alive = 5_000, state = "ok")
        val stale = reading(3048.73, "2026-11-20T12:23:52+00:00", alive = 4_000, state = "starting")
        assertEquals("ok", QuotaState.newest(current, stale).state)
        val fresher = reading(3048.73, "2026-11-20T12:23:52+00:00", alive = 6_000, state = "stopped")
        assertEquals("stopped", QuotaState.newest(current, fresher).state)
    }

    @Test fun aStatusPushWithoutReadingTimeIsAccepted() {
        val push = newer.withPush(mapOf("type" to "status", "state" to "needs_human", "error" to "x"), sentAtMs = 9_000)
        val got = QuotaState.newest(newer, push)
        assertEquals("needs_human", got.state)
        assertEquals(9_000L, got.heartbeatAtMs)
    }

    @Test fun nothingStoredYetTakesAnything() =
        assertEquals(3065.43, QuotaState.newest(QuotaState(), older).remainingMb!!, 0.0)
}
