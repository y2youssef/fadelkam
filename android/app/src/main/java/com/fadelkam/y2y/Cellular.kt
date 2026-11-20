package com.fadelkam.y2y

import android.app.PendingIntent
import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.telephony.SubscriptionManager
import android.telephony.TelephonyManager
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

    /** MCC 602 (Egypt) + MNC 04: WE (Telecom Egypt). The phone is dual-SIM (Etisalat + WE). */
    private const val WE_SIM = "60204"

    /** The counter is for WE data: mobile data is on and the data SIM is the WE one. */
    fun isUp(c: Context): Boolean = weIsDataSim(c) && cellularUp(c)

    /** Switching the data SIM to Etisalat (say, when WE runs out) hides the WE counter.
     *  Neither call needs a permission. */
    fun weIsDataSim(c: Context): Boolean {
        val sub = SubscriptionManager.getDefaultDataSubscriptionId()
        if (sub == SubscriptionManager.INVALID_SUBSCRIPTION_ID) return false
        return c.getSystemService(TelephonyManager::class.java).createForSubscriptionId(sub).simOperator == WE_SIM
    }

    fun cellularUp(c: Context): Boolean {
        val cm = c.getSystemService(ConnectivityManager::class.java)
        @Suppress("DEPRECATION") // allNetworks: the default network alone hides cellular while on Wi-Fi
        return cm.allNetworks.any { n ->
            cm.getNetworkCapabilities(n)?.let {
                it.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) &&
                    it.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            } == true
        }
    }

    /**
     * Two independent "wake me when mobile data comes up" triggers, both outliving our process:
     *  1. a PendingIntent network callback: fast (fires as the network comes up);
     *  2. a JobScheduler job with a cellular network constraint: Android's standard battery-friendly
     *     way to wait for a network, a backup in case (1) is dropped. Seen 2026-11-20 13:47:44: the
     *     system released (1) while toggling data, and the counter stayed hidden after data came back.
     * Re-armed on every process start, every hide and every app open, so a lost trigger heals itself.
     */
    fun arm(c: Context) {
        val pi = PendingIntent.getBroadcast(c, 30, Intent(c, CellularReceiver::class.java),
            // Mutable: the system adds the network to the intent. Explicit component, so it's safe.
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
        runCatching { c.getSystemService(ConnectivityManager::class.java).registerNetworkCallback(request, pi) }
            .onFailure { Log.w(TAG, "couldn't arm the network callback", it) }
        // The job only matters while the counter is hidden; with WE data on it would just run once
        // for nothing (the next hide schedules it again).
        if (Build.VERSION.SDK_INT >= 28 && !isUp(c)) {
            val job = JobInfo.Builder(JOB_ID, ComponentName(c, DataUpJob::class.java))
                .setRequiredNetwork(request)
                .setPersisted(true) // survives reboots
                .build()
            runCatching { c.getSystemService(JobScheduler::class.java).schedule(job) }
                .onFailure { Log.w(TAG, "couldn't schedule the backup job", it) }
        }
        Log.i(TAG, "armed: network callback" + if (isUp(c)) "" else " + backup job")
    }

    /** Called by either trigger, and safe to call any time. */
    fun showIfWeData(c: Context, by: String) {
        val up = isUp(c)
        Log.i(TAG, "woken by $by: weData=$up running=${QuotaService.running}")
        if (Store.isPaired(c) && up) QuotaService.send(c, Counter.Event.WE_DATA_UP)
    }

    private const val TAG = "Cellular"
    private const val JOB_ID = 1
}

/** Trigger 1: the network callback's PendingIntent. */
class CellularReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) = Cellular.showIfWeData(c, "network callback")
}

/** Trigger 2: the backup job, run by JobScheduler once a cellular network is available. It isn't
 *  rescheduled from here (that would loop while data is on); Cellular.arm schedules it again on
 *  the next hide or process start. */
class DataUpJob : JobService() {
    override fun onStartJob(params: JobParameters): Boolean {
        Cellular.showIfWeData(this, "backup job")
        return false // done
    }

    override fun onStopJob(params: JobParameters): Boolean = false
}
