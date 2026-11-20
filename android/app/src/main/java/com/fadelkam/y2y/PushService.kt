package com.fadelkam.y2y

import android.util.Log
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.runBlocking

/**
 * FCM from the laptop (server/app/cloud.py, CloudPublisher). Data-only messages:
 *   quota     remaining_mb, total_mb, fetched_at, state   normal priority, at most one per 2 min
 *   status    state, error                                 high only when a human is needed
 *   challenge id, kind                                     high: a captcha is waiting
 *   test      at
 * Normal-priority pushes wait for Doze maintenance windows, which is fine for a counter nobody is
 * looking at; high priority is used only when the result is a notification the user must see.
 * Runs on a background thread with about 20 s to finish.
 */
class PushService : FirebaseMessagingService() {
    /** FCM registered this installation, or re-registered it after the FID changed. */
    override fun onRegistered(fid: String) {
        if (!Store.isPaired(this)) return // registered right after pairing instead
        runBlocking { runCatching { Cloud.registerDevice(fid) }.onFailure { Log.w(TAG, "push id upload failed", it) } }
    }

    override fun onMessageReceived(m: RemoteMessage) {
        val d = m.data
        Log.i(TAG, "push ${d["type"]}, sent ${(System.currentTimeMillis() - m.sentTime) / 1000} s ago")
        when (d["type"]) {
            "quota", "status" -> {
                val s = Store.receive(this, Store.load(this).withPush(d, m.sentTime))
                when (s.state) {
                    "needs_human" -> Notifier.alert(this, "WE login blocked", s.error ?: "Check the laptop")
                    "login_failed" -> Notifier.alert(this, "WE login failed", s.error ?: "Check the laptop")
                    "ok" -> { Notifier.cancelAlert(this); Notifier.cancelCaptcha(this) }
                }
            }
            "challenge" -> fetchChallenge(d["id"])
            "test" -> Notifier.alert(this, "Test from the laptop", "Push messages reach this phone.")
            else -> Log.i(TAG, "unknown push type ${d["type"]}")
        }
    }

    private fun fetchChallenge(pushedId: String?) = runBlocking {
        runCatching {
            val snap = Cloud.read("challenge", timeoutMs = 15_000)
            val id = snap.child("id").value as? String ?: return@runCatching // solved or expired already
            Notifier.showCaptchaOnce(this@PushService, id, snap.child("image_png_b64").value as? String,
                (snap.child("timeout_s").value as? Number)?.toInt() ?: 600)
        }.onFailure {
            Log.w(TAG, "couldn't fetch the captcha", it)
            if (pushedId != null) { // still tell the human; the app shows the image when opened
                Store.captchaFile(this@PushService).delete()
                Notifier.showCaptcha(this@PushService, pushedId, 600, note = "WE wants a captcha")
            }
        }
    }

    companion object {
        private const val TAG = "PushService"
    }
}
