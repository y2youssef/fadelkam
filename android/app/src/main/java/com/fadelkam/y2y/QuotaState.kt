package com.fadelkam.y2y

import com.google.firebase.database.DataSnapshot
import org.json.JSONObject
import java.time.OffsetDateTime

/**
 * What the laptop publishes at /state (server/app/cloud.py, `cloud_state`), kept on the phone so a
 * push carrying only a few fields can still redraw the whole notification.
 */
data class QuotaState(
    val remainingMb: Double? = null,
    val totalMb: Double? = null,
    val offer: String? = null,
    val renewsInDays: Int? = null,
    val fetchedAt: String? = null,   // ISO time of the reading on the laptop
    val state: String? = null,       // poller state: ok, degraded, needs_human, stopped, ... ("unpaired" is ours)
    val error: String? = null,
    val heartbeatAtMs: Long? = null, // server time of the laptop's last write (or of the last push)
    val heartbeatS: Int = 300,
) {
    val fetchedAtMs: Long?
        get() = fetchedAt?.let { runCatching { OffsetDateTime.parse(it).toInstant().toEpochMilli() }.getOrNull() }

    /** No sign of the laptop for 2.5 heartbeats: asleep, offline or crashed (FAILURE_MODES #44). */
    fun isStale(nowMs: Long = System.currentTimeMillis()): Boolean =
        heartbeatAtMs != null && nowMs - heartbeatAtMs > heartbeatS * 2500L

    /** A push from the laptop: type "quota" or "status" (cloud.py, CloudPublisher._pushes). */
    fun withPush(d: Map<String, String>, sentAtMs: Long): QuotaState {
        val alive = maxOf(heartbeatAtMs ?: 0L, sentAtMs)
        return when (d["type"]) {
            "quota" -> copy(
                remainingMb = d["remaining_mb"]?.toDoubleOrNull() ?: remainingMb,
                totalMb = d["total_mb"]?.toDoubleOrNull() ?: totalMb,
                fetchedAt = d["fetched_at"] ?: fetchedAt,
                state = d["state"] ?: state,
                heartbeatAtMs = alive,
            )
            "status" -> copy(state = d["state"] ?: state, error = d["error"], heartbeatAtMs = alive)
            else -> this
        }
    }

    fun toJson(): String = JSONObject().apply {
        put("remainingMb", remainingMb); put("totalMb", totalMb); put("offer", offer)
        put("renewsInDays", renewsInDays); put("fetchedAt", fetchedAt); put("state", state)
        put("error", error); put("heartbeatAtMs", heartbeatAtMs); put("heartbeatS", heartbeatS)
    }.toString()

    companion object {
        /**
         * Never backwards: an incoming state replaces the current one only if its reading isn't
         * older (same reading: only if its sign of life isn't older). Seen 2026-11-20 14:26: after a
         * restart Firebase's disk cache answered with an old /state (3,065 MB) and it overwrote the
         * newer 3,048 MB a push had just delivered. The latest sign of life is kept either way.
         */
        fun newest(current: QuotaState, incoming: QuotaState): QuotaState {
            val cur = current.fetchedAtMs
            val inc = incoming.fetchedAtMs
            val alive = maxOf(current.heartbeatAtMs ?: 0L, incoming.heartbeatAtMs ?: 0L).takeIf { it > 0 }
            val older = (cur != null && inc != null && inc < cur) ||
                (cur != null && inc == cur && (incoming.heartbeatAtMs ?: 0L) < (current.heartbeatAtMs ?: 0L))
            return (if (older) current else incoming).copy(heartbeatAtMs = alive)
        }

        fun fromJson(s: String?): QuotaState {
            val o = s?.let { runCatching { JSONObject(it) }.getOrNull() } ?: return QuotaState()
            fun dbl(k: String): Double? = if (o.has(k)) o.optDouble(k).takeUnless { it.isNaN() } else null
            fun str(k: String): String? = if (o.has(k)) o.optString(k) else null
            return QuotaState(
                remainingMb = dbl("remainingMb"), totalMb = dbl("totalMb"), offer = str("offer"),
                renewsInDays = if (o.has("renewsInDays")) o.optInt("renewsInDays") else null,
                fetchedAt = str("fetchedAt"), state = str("state"), error = str("error"),
                heartbeatAtMs = if (o.has("heartbeatAtMs")) o.optLong("heartbeatAtMs") else null,
                heartbeatS = o.optInt("heartbeatS", 300),
            )
        }

        fun fromSnapshot(s: DataSnapshot): QuotaState {
            val q = s.child("quota")
            val st = s.child("status")
            return QuotaState(
                remainingMb = (q.child("remaining_mb").value as? Number)?.toDouble(),
                totalMb = (q.child("total_mb").value as? Number)?.toDouble(),
                offer = q.child("offer").value as? String,
                renewsInDays = (q.child("renews_in_days").value as? Number)?.toInt(),
                fetchedAt = q.child("fetched_at").value as? String,
                state = st.child("state").value as? String,
                error = st.child("error").value as? String,
                heartbeatAtMs = (s.child("heartbeat_at").value as? Number)?.toLong(),
                heartbeatS = (s.child("heartbeat_s").value as? Number)?.toInt() ?: 300,
            )
        }
    }
}
