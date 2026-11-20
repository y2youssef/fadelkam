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
    private val cellular = mutableSetOf<Network>()
    private val cm by lazy { getSystemService(ConnectivityManager::class.java) }

    /** Mobile data has been gone for HIDE_GRACE_MS (not just a handover blip): hide and stop. */
    private val hide = Runnable {
        if (!Cellular.isUp(this)) {
            Log.i(TAG, "mobile data off: hiding")
            stopSelf()
        }
    }

    private val net = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(n: Network) {
            main.post { cellular += n; main.removeCallbacks(hide) }
        }

        override fun onLost(n: Network) {
            main.post {
                cellular -= n
                if (cellular.isEmpty()) main.postDelayed(hide, HIDE_GRACE_MS)
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // A started foreground service must call startForeground promptly, even if it stops at once.
        ServiceCompat.startForeground(this, Notifier.ID_QUOTA, Notifier.quota(this, Store.load(this)),
            if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0)
        running = true
        if (!Store.isPaired(this) || !Cellular.isUp(this)) { // e.g. restarted by the system with data off
            stopSelf()
            return
        }
        cm.registerNetworkCallback(Cellular.request, net)
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

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (Cellular.isUp(this)) Notifier.showQuota(this, Store.load(this))
        return START_STICKY // if the system kills us while data is on, it restarts us
    }

    override fun onDestroy() {
        running = false
        main.removeCallbacksAndMessages(null)
        runCatching { cm.unregisterNetworkCallback(net) }
        scope.cancel()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    companion object {
        private const val TAG = "QuotaService"
        private const val HIDE_GRACE_MS = 30_000L

        @Volatile
        var running = false
            private set

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
