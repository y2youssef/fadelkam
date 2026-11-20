package com.fadelkam.y2y

import android.content.Context
import androidx.core.content.edit
import java.io.File

/** Small local state: the last reading, whether we're paired, the captcha on screen. */
object Store {
    private fun prefs(c: Context) = c.getSharedPreferences("we_quota", Context.MODE_PRIVATE)

    private val lock = Any()

    fun load(c: Context): QuotaState = QuotaState.fromJson(prefs(c).getString("state", null))

    /**
     * The one way new data enters, from a push, the service's first read or the app screen:
     * merged by the never-backwards rule (QuotaState.newest) in one locked step (pushes arrive on
     * FCM's thread, the rest on the main thread), and if it changed, the counter is told, so the
     * notification follows whatever source saw the news first.
     */
    fun receive(c: Context, incoming: QuotaState): QuotaState {
        val (next, changed) = synchronized(lock) {
            val current = load(c)
            val next = QuotaState.newest(current, incoming)
            if (next != current) prefs(c).edit { putString("state", next.toJson()) }
            next to (next != current)
        }
        if (changed) QuotaService.send(c, Counter.Event.READING)
        return next
    }

    fun isPaired(c: Context): Boolean = prefs(c).getBoolean("paired", false)
    fun setPaired(c: Context, v: Boolean) = prefs(c).edit { putBoolean("paired", v) }

    /** The captcha id we last notified about, so the same captcha doesn't ring twice. */
    fun shownChallenge(c: Context): String? = prefs(c).getString("challenge", null)
    fun setShownChallenge(c: Context, id: String?) = prefs(c).edit { putString("challenge", id) }

    /** Start (ms) of the WE mobile data session in progress, if any: survives service restarts. */
    fun openSession(c: Context): Long? = prefs(c).getLong("session", 0L).takeIf { it > 0 }
    fun setOpenSession(c: Context, startMs: Long?) = prefs(c).edit { putLong("session", startMs ?: 0L) }

    /** The captcha image, kept for a retry notification and for the app screen. */
    fun captchaFile(c: Context) = File(c.cacheDir, "captcha.png")
}
