package com.fadelkam.y2y

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** After a reboot or an app update: re-arm the mobile-data watch, and show the counter if data is on. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        if (i.action !in setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)) return
        if (!Store.isPaired(c)) return
        android.util.Log.i("QuotaBoot", "${i.action}: arming, weData=${Cellular.isUp(c)}")
        Cellular.arm(c)
        if (Cellular.isUp(c)) QuotaService.start(c)
    }
}
