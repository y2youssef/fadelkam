package com.fadelkam.y2y

import android.app.Application

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        Notifier.createChannels(this)
        // The mobile-data watch is dropped on force-stop; any start of our process re-arms it.
        if (Store.isPaired(this)) Cellular.arm(this)
    }
}
