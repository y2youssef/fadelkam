package com.fadelkam.y2y

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * Runs only while mobile data is on, and owns the sticky notification (Live Update) for that time.
 *
 * Battery: no timers, no polling, no wakelocks, no open database connection. It reads /state once
 * when it starts (fresh numbers and heartbeat); after that the laptop's FCM pushes update the
 * notification. Its only job while idle is a network callback, which costs nothing until mobile
 * data goes away. Then it removes the notification and stops, and the process can be reclaimed.
 */
class QuotaService : Service() {
    private val main = Handler(Looper.getMainLooper())
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val cm by lazy { getSystemService(ConnectivityManager::class.java) }

    /** Cellular networks with internet, as the callbacks report them (main thread only). Asking
     *  ConnectivityManager from inside onLost isn't reliable: the dying network can still be
     *  listed there, which left the counter up after data was switched off (seen 2026-11-20 13:44). */
    private val cellular = mutableSetOf<Network>()

    /** Starts SHOWN: startForeground() in onCreate has just posted the counter. */
    private val counter = Counter(Counter.State.SHOWN)
    private var destroyed = false

    private val graceOver = Runnable { handle(Counter.Event.GRACE_ELAPSED) }

    /** When WE data went down (start of the grace countdown): the session's end if it stays down. */
    private var downAtMs: Long? = null

    /** One more look a little later: right after a data-SIM switch the new network can come up
     *  before the data-SIM setting changes, and a change can land between our looks. */
    private val recheck = Runnable { look(again = false) }

    private val net = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(n: Network) {
            main.post { cellular += n; Log.i(TAG, "cellular up: $n"); look() }
        }

        override fun onLost(n: Network) {
            main.post { cellular -= n; Log.i(TAG, "cellular lost: $n"); look() }
        }
    }

    /** Turn what the network callbacks reported into a WE_DATA_UP/DOWN event (main thread). */
    private fun look(again: Boolean = true) {
        val up = cellular.isNotEmpty() && Cellular.weIsDataSim(this)
        handle(if (up) Counter.Event.WE_DATA_UP else Counter.Event.WE_DATA_DOWN)
        if (again) {
            main.removeCallbacks(recheck)
            main.postDelayed(recheck, RECHECK_MS)
        }
    }

    /** The only place the counter changes (main thread). */
    private fun handle(e: Counter.Event) {
        if (destroyed) return
        val from = counter.state
        val actions = counter.on(e)
        if (from != counter.state || Counter.Action.POST !in actions) Log.i(TAG, "$from --$e--> ${counter.state} $actions")
        for (a in actions) when (a) {
            Counter.Action.POST -> Notifier.showQuota(this, Store.load(this))
            Counter.Action.START_GRACE -> { downAtMs = System.currentTimeMillis(); main.postDelayed(graceOver, HIDE_GRACE_MS) }
            Counter.Action.CANCEL_GRACE -> { downAtMs = null; main.removeCallbacks(graceOver) }
            Counter.Action.STOP -> { endSession(downAtMs ?: System.currentTimeMillis()); stopSelf() }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // A started foreground service must call startForeground promptly, even if it stops at once.
        ServiceCompat.startForeground(this, Notifier.ID_QUOTA, Notifier.quota(this, Store.load(this)),
            if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0)
        instance = this
        if (!Store.isPaired(this) || !Cellular.isUp(this)) { // e.g. restarted by the system with data off
            stopSelf()
            return
        }
        beginSession()
        cm.registerNetworkCallback(Cellular.request, net)
        // The callback reports the networks already up within moments. If data went off between
        // the check above and the registration, none come: this look catches that case.
        main.postDelayed({ look() }, FIRST_LOOK_MS)
        scope.launch {
            runCatching { Cloud.read("state") }
                .onSuccess { snap ->
                    if (snap.exists()) {
                        val q = QuotaState.fromSnapshot(snap)
                        Store.save(this@QuotaService, q)
                        Notifier.showQuota(this@QuotaService, q)
                    }
                }
                .onFailure { Log.w(TAG, "first read failed; pushes will fill in", it) }
        }
    }

    /** A WE mobile data session starts with the service, unless one is already open (the system
     *  restarted us, or the phone rebooted with data on): that one simply continues. */
    private fun beginSession() {
        if (Store.openSession(this) != null) return
        val now = System.currentTimeMillis()
        Store.setOpenSession(this, now)
        Cloud.sessionEvent(on = true, tMs = now)
        Log.i(TAG, "session started")
    }

    private fun endSession(atMs: Long) {
        if (Store.openSession(this) == null) return
        Store.setOpenSession(this, null)
        Cloud.sessionEvent(on = false, tMs = atMs)
        Log.i(TAG, "session ended")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (Cellular.isUp(this)) handle(Counter.Event.WE_DATA_UP)
        return START_STICKY // if the system kills us while data is on, it restarts us
    }

    override fun onDestroy() {
        destroyed = true
        instance = null
        Cellular.arm(this) // hidden now: make sure something wakes us when WE data comes back
        main.removeCallbacksAndMessages(null)
        runCatching { cm.unregisterNetworkCallback(net) }
        scope.cancel()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    companion object {
        private const val TAG = "QuotaService"
        private const val HIDE_GRACE_MS = 5_000L  // rides out a brief signal drop; data off hides in ~5 s
        private const val RECHECK_MS = 5_000L
        private const val FIRST_LOOK_MS = 10_000L

        @Volatile
        private var instance: QuotaService? = null

        val running: Boolean get() = instance != null

        /**
         * An event from elsewhere in the app (a push, a swipe, a wake-up trigger, the app screen).
         * Running: handed to the service's state machine. Not running: start the service if WE
         * data is on (it shows the counter), otherwise drop a leftover plain notification.
         */
        fun send(c: Context, e: Counter.Event) {
            val s = instance
            when {
                s != null -> s.main.post { s.handle(e) }
                Cellular.isUp(c) -> start(c)
                else -> Notifier.cancelQuota(c)
            }
        }

        /**
         * Show the counter. From the background Android allows this only in some cases: right
         * after boot or an update, a high-priority push, a tap on our notification, or when the
         * app is exempt from battery optimization (the app asks for that). Otherwise the
         * notification is posted on its own, without the service.
         */
        fun start(c: Context) {
            runCatching { ContextCompat.startForegroundService(c, Intent(c, QuotaService::class.java)) }
                .onFailure {
                    Log.i(TAG, "service not allowed now (${it.javaClass.simpleName}); plain notification")
                    Notifier.showQuota(c, Store.load(c))
                }
        }
    }
}
