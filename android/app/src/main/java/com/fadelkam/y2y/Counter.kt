package com.fadelkam.y2y

/**
 * When the sticky counter is on screen: one explicit state machine instead of flags spread over
 * the service, the receivers and the push handler (three bugs on 2026-11-20 came from that).
 * Pure Kotlin, no Android calls: QuotaService feeds it events and carries out the actions, and
 * CounterTest checks every transition.
 *
 *              WE data up                   WE data down
 *   HIDDEN ────────────────▶ SHOWN ─────────────────────▶ HIDING ──(grace, still down)──▶ HIDDEN
 *                            ▲  │ up / swiped / reading:      │ up: cancel grace, post
 *                            │  │ post                        │ down / swiped / reading: nothing
 *                            │  └─────────────────────────────┘
 */
class Counter(state: State = State.HIDDEN) {
    enum class State { HIDDEN, SHOWN, HIDING }

    enum class Event {
        WE_DATA_UP,     // a cellular network is up and the data SIM is WE
        WE_DATA_DOWN,   // no cellular network, or data moved to the other SIM
        GRACE_ELAPSED,  // the countdown started by WE_DATA_DOWN ran out
        SWIPED,         // the user swiped the counter away
        READING,        // a new reading or status arrived (push)
    }

    enum class Action { POST, START_GRACE, CANCEL_GRACE, STOP }

    var state: State = state
        private set

    /** Apply an event; returns what the caller must do, in order. */
    fun on(e: Event): List<Action> {
        val (next, actions) = next(state, e)
        state = next
        return actions
    }

    companion object {
        fun next(s: State, e: Event): Pair<State, List<Action>> = when (s) {
            State.HIDDEN -> when (e) {
                Event.WE_DATA_UP -> State.SHOWN to listOf(Action.POST)
                else -> State.HIDDEN to emptyList()
            }
            State.SHOWN -> when (e) {
                // Re-posting is silent and idempotent: it also heals a counter that went missing.
                Event.WE_DATA_UP, Event.SWIPED, Event.READING -> State.SHOWN to listOf(Action.POST)
                Event.WE_DATA_DOWN -> State.HIDING to listOf(Action.START_GRACE)
                Event.GRACE_ELAPSED -> State.SHOWN to emptyList() // a stale timer
            }
            State.HIDING -> when (e) {
                // Swiped while data was off, then data came back: post it again (bug of 13:51).
                Event.WE_DATA_UP -> State.SHOWN to listOf(Action.CANCEL_GRACE, Action.POST)
                Event.GRACE_ELAPSED -> State.HIDDEN to listOf(Action.STOP)
                // Down again: never restart the countdown (a hide took 8 s instead of 5).
                // Swiped or a reading while data is off: don't bring it back.
                Event.WE_DATA_DOWN, Event.SWIPED, Event.READING -> State.HIDING to emptyList()
            }
        }
    }
}
