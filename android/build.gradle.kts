plugins {
    id("com.android.application") version "9.5.1" apply false
    // AGP 9 compiles Kotlin itself, with the Kotlin Gradle plugin found on the classpath (2.2 by
    // default). Firebase 35 ships Kotlin 2.4 metadata, so pin a newer one here.
    id("org.jetbrains.kotlin.android") version "2.4.21" apply false
    id("com.google.gms.google-services") version "4.6.0" apply false
}
