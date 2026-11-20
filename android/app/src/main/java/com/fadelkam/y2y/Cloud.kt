package com.fadelkam.y2y

import android.os.Build
import android.os.Handler
import android.os.Looper
import com.google.firebase.auth.FirebaseAuth
import com.google.firebase.database.DataSnapshot
import com.google.firebase.database.DatabaseError
import com.google.firebase.database.ValueEventListener
import com.google.firebase.database.FirebaseDatabase
import com.google.firebase.database.ServerValue
import com.google.firebase.installations.FirebaseInstallations
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Everything the phone does with Firebase. The paths and fields must match the security rules in
 * firebase/database.rules.json: every `at` is the server's own timestamp, and a phone writes only
 * its own /pairing, /devices and /inbox entries.
 *
 * Battery: the database socket is open only while someone needs it (a one-off read or write, or
 * the app screen being visible) and closed right after. Updates reach the phone as FCM pushes,
 * which share Google Play services' single connection instead of keeping our own.
 */
object Cloud {
    const val TIMEOUT_MS = 20_000L

    val db: FirebaseDatabase get() = FirebaseDatabase.getInstance()
    val uid: String? get() = FirebaseAuth.getInstance().currentUser?.uid
    private val model: String get() = "${Build.MANUFACTURER} ${Build.MODEL}".take(64)

    private val lock = Any()
    private var users = 0

    /** Keep the database connection open (balanced by [release]). */
    fun acquire() = synchronized(lock) { if (users++ == 0) db.goOnline() }

    fun release() = synchronized(lock) { if (users > 0 && --users == 0) db.goOffline() }

    private suspend fun <T> online(timeoutMs: Long, block: suspend () -> T): T {
        acquire()
        try {
            return withTimeout(timeoutMs) { block() }
        } finally {
            release()
        }
    }

    /** Anonymous account; it survives app restarts (a reinstall gets a new one: pair again). */
    suspend fun signIn(): String = uid ?: withTimeout(TIMEOUT_MS) {
        FirebaseAuth.getInstance().signInAnonymously().await().user!!.uid
    }

    suspend fun isAllowed(uid: String): Boolean = read("allowed/$uid").exists()

    /** A fresh read. With disk persistence on, get() answers from the cache if the connection
     *  isn't up yet (seen 2026-11-20: an old /state came back), so wait for the connection first;
     *  if it doesn't come, the cached copy is still better than nothing (Store.receive keeps the newer). */
    suspend fun read(path: String, timeoutMs: Long = TIMEOUT_MS): DataSnapshot =
        online(timeoutMs) {
            withTimeoutOrNull(timeoutMs / 2) { awaitConnected() }
            db.getReference(path).get().await()
        }

    private suspend fun awaitConnected() = suspendCancellableCoroutine { cont ->
        val ref = db.getReference(".info/connected")
        val l = object : ValueEventListener {
            override fun onDataChange(s: DataSnapshot) {
                if (s.getValue(Boolean::class.java) == true) {
                    ref.removeEventListener(this)
                    if (cont.isActive) cont.resume(Unit)
                }
            }

            override fun onCancelled(e: DatabaseError) {
                ref.removeEventListener(this)
                if (cont.isActive) cont.resume(Unit)
            }
        }
        ref.addValueEventListener(l)
        cont.invokeOnCancellation { ref.removeEventListener(l) }
    }

    suspend fun requestPairing(uid: String, code: String) =
        write("pairing/$uid", mapOf("code" to code, "model" to model, "at" to ServerValue.TIMESTAMP))

    /**
     * Registers this phone with FCM by its Firebase installation ID (FID) and stores the FID at
     * /devices/<uid>; the laptop sends pushes to it (HTTP v1 `"fid"` target). Needs the pairing
     * approved. Tokens are deprecated since firebase-messaging 25.1.0; the FID path was checked
     * against the live API on 2026-11-20 (an unknown FID is answered 404 NotRegistered).
     */
    suspend fun registerDevice(fid: String? = null) {
        val u = uid ?: return
        val id = fid ?: withTimeout(TIMEOUT_MS) {
            FirebaseMessaging.getInstance().register().await() // also fires PushService.onRegistered
            FirebaseInstallations.getInstance().id.await()
        }
        write("devices/$u", mapOf("fid" to id, "model" to model, "at" to ServerValue.TIMESTAMP))
    }

    /** Phone -> laptop: "answer" (captcha code), "refresh" (poll now) or "hello". */
    suspend fun inbox(kind: String, extra: Map<String, Any> = emptyMap(), timeoutMs: Long = TIMEOUT_MS) {
        val u = uid ?: error("not signed in")
        write("inbox/$u/$kind", extra + ("at" to ServerValue.TIMESTAMP), timeoutMs)
    }

    /**
     * WE mobile data on/off, for the laptop's usage history (/inbox/<uid>/session/<id>). Fire and
     * forget: the write is queued (on disk) at once, the connection is held until it's sent, at
     * most SESSION_HOLD_MS, and without a network it goes out with the next connection.
     */
    fun sessionEvent(on: Boolean, tMs: Long) {
        val u = uid ?: return
        acquire()
        val released = AtomicBoolean(false)
        val release = { if (released.compareAndSet(false, true)) release() }
        db.getReference("inbox/$u/session").push()
            .setValue(mapOf("on" to on, "t" to tMs, "at" to ServerValue.TIMESTAMP))
            .addOnCompleteListener { release() }
        Handler(Looper.getMainLooper()).postDelayed({ release() }, SESSION_HOLD_MS)
    }

    private const val SESSION_HOLD_MS = 60_000L

    private suspend fun write(path: String, value: Map<String, Any>, timeoutMs: Long = TIMEOUT_MS) {
        online(timeoutMs) { db.getReference(path).setValue(value).await() }
    }
}
