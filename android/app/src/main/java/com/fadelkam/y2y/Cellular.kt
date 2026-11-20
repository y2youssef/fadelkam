package com.fadelkam.y2y

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log

/**
 * "Mobile data is on" = the phone has a cellular network with internet. The sticky notification is
 * the mobile-data counter, so it shows only then.
 *
 * Nothing of ours runs while mobile data is off: [arm] hands the system a PendingIntent, and the
 * system wakes [CellularReceiver] when a cellular network comes up (this registration outlives our
 * process; it's redone on boot, update and app start). While data is on, QuotaService watches for
 * the network to go away and then hides the notification and stops.
 */
object Cellular {
    val request: NetworkRequest
        get() = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()

    fun isUp(c: Context): Boolean {
        val cm = c.getSystemService(ConnectivityManager::class.java)
        @Suppress("DEPRECATION") // allNetworks: the default network alone hides cellular while on Wi-Fi
        return cm.allNetworks.any { n ->
            cm.getNetworkCapabilities(n)?.let {
                it.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) &&
                    it.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            } == true
        }
    }

    fun arm(c: Context) {
        val pi = PendingIntent.getBroadcast(c, 30, Intent(c, CellularReceiver::class.java),
            // Mutable: the system adds the network to the intent. Explicit component, so it's safe.
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
        runCatching { c.getSystemService(ConnectivityManager::class.java).registerNetworkCallback(request, pi) }
            .onFailure { Log.w("Cellular", "couldn't arm the mobile-data watch", it) }
    }
}

/** Mobile data came up: show the counter. */
class CellularReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        if (Store.isPaired(c) && Cellular.isUp(c) && !QuotaService.running) QuotaService.start(c)
    }
}
