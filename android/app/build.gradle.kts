plugins {
    id("com.android.application")
    // Reads app/google-services.json (git-ignored; download it from the Firebase console).
    id("com.google.gms.google-services")
}

android {
    namespace = "com.fadelkam.y2y"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.fadelkam.y2y" // must match the app registered in Firebase
        minSdk = 26
        targetSdk = 37 // the phone runs Android 17 (One UI 9)
        versionCode = 1
        versionName = "0.1"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation(platform("com.google.firebase:firebase-bom:35.2.0"))
    implementation("com.google.firebase:firebase-auth")
    implementation("com.google.firebase:firebase-database")
    implementation("com.google.firebase:firebase-messaging")
    implementation("com.google.firebase:firebase-installations") // the FID we register for pushes
    implementation("androidx.core:core-ktx:1.19.1")
    implementation("com.google.android.material:material:1.14.0") // Material 3 look, light and dark
    implementation("androidx.activity:activity-ktx:1.13.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.11.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-play-services:1.11.0")
    testImplementation("junit:junit:4.13.2") // CounterTest: the counter state machine, on the laptop
}
