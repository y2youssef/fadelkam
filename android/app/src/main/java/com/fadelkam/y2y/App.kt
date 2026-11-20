package com.fadelkam.y2y

import android.app.Application
import androidx.core.content.ContextCompat
import com.google.android.material.color.DynamicColors
import com.google.android.material.color.DynamicColorsOptions
import com.google.firebase.database.FirebaseDatabase

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        // Disk persistence before any other database call: a write made without a connection
        // (data just switched off, no Wi-Fi) survives the app being killed and is sent later.
        runCatching { FirebaseDatabase.getInstance().setPersistenceEnabled(true) }
        Notifier.createChannels(this)
        // A full light/dark Material 3 colour scheme generated from WE purple (Android 12+).
        DynamicColors.applyToActivitiesIfAvailable(this,
            DynamicColorsOptions.Builder().setContentBasedSource(ContextCompat.getColor(this, R.color.brand)).build())
        // The mobile-data watch is dropped on force-stop; any start of our process re-arms it.
        if (Store.isPaired(this)) Cellular.arm(this)
    }
}
