package com.fadelkam.y2y

import android.app.Application
import com.google.firebase.database.FirebaseDatabase

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        // Disk persistence before any other database call: a write made without a connection
        // (data just switched off, no Wi-Fi) survives the app being killed and is sent later.
        runCatching { FirebaseDatabase.getInstance().setPersistenceEnabled(true) }
        Notifier.createChannels(this)
        // The mobile-data watch is dropped on force-stop; any start of our process re-arms it.
        if (Store.isPaired(this)) Cellular.arm(this)
    }
}
