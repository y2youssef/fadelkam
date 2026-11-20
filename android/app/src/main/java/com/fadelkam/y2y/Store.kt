package com.fadelkam.y2y

import android.content.Context
import androidx.core.content.edit
import java.io.File

/** Small local state: the last reading, whether we're paired, the captcha on screen. */
object Store {
    private fun prefs(c: Context) = c.getSharedPreferences("we_quota", Context.MODE_PRIVATE)

    private val lock = Any()

    fun load(c: Context): QuotaState = QuotaState.fromJson(prefs(c).getString("state", null))

    fun save(c: Context, s: QuotaState) = synchronized(lock) { prefs(c).edit { putString("state", s.toJson()) } }

    /** Read-modify-write as one step: pushes arrive on FCM's background thread while the service
     *  and the app screen save on the main thread, and a plain load-then-save could drop one. */
    fun update(c: Context, change: (QuotaState) -> QuotaState): QuotaState = synchronized(lock) {
        change(load(c)).also { prefs(c).edit { putString("state", it.toJson()) } }
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
