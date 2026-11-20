package com.fadelkam.y2y

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.app.RemoteInput
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/** Buttons on the notifications: Refresh, the captcha reply, and putting a swiped-away sticky back. */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        if (i.action == RESHOW) { // swiped away: the counter's state machine decides
            QuotaService.send(c, Counter.Event.SWIPED)
            return
        }
        val pending = goAsync()
        CoroutineScope(SupervisorJob() + Dispatchers.Main).launch {
            try {
                when (i.action) {
                    REFRESH -> Cloud.inbox("refresh", timeoutMs = WRITE_TIMEOUT_MS)
                    ANSWER -> answer(c, i)
                }
            } catch (e: Exception) {
                if (i.action == ANSWER) Notifier.showCaptcha(c, i.getStringExtra(EXTRA_ID) ?: "",
                    i.getIntExtra(EXTRA_TIMEOUT, 600), note = "Not sent (${e.javaClass.simpleName}): try again")
                else Notifier.alert(c, "Refresh not sent", "Couldn't reach Firebase: ${e.javaClass.simpleName}")
            } finally {
                pending.finish()
            }
        }
    }

    private suspend fun answer(c: Context, i: Intent) {
        val id = i.getStringExtra(EXTRA_ID).orEmpty()
        val timeout = i.getIntExtra(EXTRA_TIMEOUT, 600)
        val code = RemoteInput.getResultsFromIntent(i)?.getCharSequence(KEY_CODE)?.toString()?.trim().orEmpty()
        if (!CODE_RE.matches(code) || id.isEmpty()) {
            Notifier.showCaptcha(c, id, timeout, note = "Use 3 to 12 letters or digits")
            return
        }
        Cloud.inbox("answer", mapOf("id" to id, "code" to code), timeoutMs = WRITE_TIMEOUT_MS)
        Notifier.captchaInfo(c, "Code sent", "The laptop is typing it in. A new captcha means it was wrong.")
    }

    companion object {
        const val REFRESH = "com.fadelkam.y2y.REFRESH"
        const val ANSWER = "com.fadelkam.y2y.ANSWER"
        const val RESHOW = "com.fadelkam.y2y.RESHOW"
        const val KEY_CODE = "code"
        const val EXTRA_ID = "id"
        const val EXTRA_TIMEOUT = "timeout_s"
        private const val WRITE_TIMEOUT_MS = 8_000L // a receiver must finish quickly
        val CODE_RE = Regex("^[A-Za-z0-9]{3,12}$")  // same as the server and the database rules
    }
}
