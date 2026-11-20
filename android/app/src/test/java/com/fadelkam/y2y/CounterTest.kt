package com.fadelkam.y2y

import com.fadelkam.y2y.Counter.Action.CANCEL_GRACE
import com.fadelkam.y2y.Counter.Action.POST
import com.fadelkam.y2y.Counter.Action.START_GRACE
import com.fadelkam.y2y.Counter.Action.STOP
import com.fadelkam.y2y.Counter.Event.GRACE_ELAPSED
import com.fadelkam.y2y.Counter.Event.READING
import com.fadelkam.y2y.Counter.Event.SWIPED
import com.fadelkam.y2y.Counter.Event.WE_DATA_DOWN
import com.fadelkam.y2y.Counter.Event.WE_DATA_UP
import com.fadelkam.y2y.Counter.State.HIDDEN
import com.fadelkam.y2y.Counter.State.HIDING
import com.fadelkam.y2y.Counter.State.SHOWN
import org.junit.Assert.assertEquals
import org.junit.Test

/** The counter's state machine (Counter.kt), including the three bugs seen on the phone on 2026-11-20. */
class CounterTest {
    private fun run(start: Counter.State, vararg events: Counter.Event): Pair<Counter.State, List<Counter.Action>> {
        val c = Counter(start)
        val actions = events.flatMap { c.on(it) }
        return c.state to actions
    }

    @Test fun dataOffHidesAfterTheGracePeriod() =
        assertEquals(HIDDEN to listOf(START_GRACE, STOP), run(SHOWN, WE_DATA_DOWN, GRACE_ELAPSED))

    @Test fun dataBackWithinTheGracePeriodKeepsItShown() =
        assertEquals(SHOWN to listOf(START_GRACE, CANCEL_GRACE, POST), run(SHOWN, WE_DATA_DOWN, WE_DATA_UP))

    /** 13:51: off, swiped, on -> it never came back (the service kept running with no notification). */
    @Test fun swipedWhileDataOffComesBackWhenDataReturns() =
        assertEquals(SHOWN to listOf(START_GRACE, CANCEL_GRACE, POST), run(SHOWN, WE_DATA_DOWN, SWIPED, WE_DATA_UP))

    /** 13:51: a later check restarted the countdown and a hide took 8 s instead of 5. */
    @Test fun anotherDownLookNeverRestartsTheCountdown() =
        assertEquals(HIDING to listOf(START_GRACE), run(SHOWN, WE_DATA_DOWN, WE_DATA_DOWN, WE_DATA_DOWN))

    @Test fun swipedWhileDataOnIsPutBack() = assertEquals(SHOWN to listOf(POST), run(SHOWN, SWIPED))

    @Test fun aReadingUpdatesItWhileShown() = assertEquals(SHOWN to listOf(POST), run(SHOWN, READING))

    @Test fun aReadingWhileDataIsOffDoesNotBringItBack() =
        assertEquals(HIDING to listOf(START_GRACE), run(SHOWN, WE_DATA_DOWN, SWIPED, READING))

    @Test fun aStaleTimerDoesNothingWhileShown() = assertEquals(SHOWN to emptyList<Counter.Action>(), run(SHOWN, GRACE_ELAPSED))

    @Test fun hiddenOnlyWakesForWeData() {
        for (e in listOf(WE_DATA_DOWN, GRACE_ELAPSED, SWIPED, READING)) {
            assertEquals("$e", HIDDEN to emptyList<Counter.Action>(), run(HIDDEN, e))
        }
        assertEquals(SHOWN to listOf(POST), run(HIDDEN, WE_DATA_UP))
    }

    @Test fun aFullDayOfTogglingEndsConsistent() {
        val c = Counter(SHOWN)
        repeat(50) {
            c.on(WE_DATA_DOWN); c.on(SWIPED); c.on(WE_DATA_UP)   // flaky signal
            c.on(WE_DATA_DOWN); c.on(GRACE_ELAPSED)                // data off for real
            assertEquals(HIDDEN, c.state)
            c.on(WE_DATA_UP)                                        // and back on
            assertEquals(SHOWN, c.state)
        }
    }

    /** Every state handles every event (Kotlin's exhaustive `when` makes this a compile error otherwise). */
    @Test fun everyStateHandlesEveryEvent() {
        for (s in Counter.State.entries) for (e in Counter.Event.entries) Counter.next(s, e)
    }
}
