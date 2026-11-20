# WE Quota: phone app

Shows the mobile data left as a sticky notification while mobile data is on. On Android 16+ it
is a Live Update: a chip in the status bar (for example `3.5 GB`) and an entry in One UI's Now
Bar. It hides itself about 30 s after mobile data goes off. The laptop (`../server`) does the
scraping and sends everything through Firebase; the phone never talks to WE.

Package `com.fadelkam.y2y` (as registered in the Firebase project `fadelkam-y2y`); compile and
target SDK 37 (the phone runs Android 17 / One UI 9); min SDK 26.

## How it saves battery

- **Pushes, not polling.** The laptop sends FCM data messages: a reading at most every 2 min
  and only when it changed (normal priority, so Doze batches them), and high priority only for
  a captcha or a login that needs a human. FCM shares Google Play services' one connection.
- **No connection of our own.** The database socket is opened for one read or write and closed
  again, or kept open only while the app screen is visible.
- **Nothing runs while mobile data is off.** The system wakes `CellularReceiver` when a cellular
  network comes up, through a `PendingIntent` network callback that outlives our process. Only
  then does `QuotaService` (a `specialUse` foreground service) start. It has no timers,
  wakelocks or polling, only a network callback, and it stops when mobile data goes away.

## Build (on the laptop)

```bash
cd android
export JAVA_HOME=~/Android/jdk-21 ANDROID_HOME=~/Android/Sdk
./gradlew assembleDebug            # -> app/build/outputs/apk/debug/app-debug.apk
```

Toolchain (all under `~/Android`, no root): Temurin JDK 21, SDK command-line tools,
platform-tools (adb), `platforms;android-37.0`, build-tools 36.1.0. Gradle 9.9.1 comes from the
wrapper (checksum-pinned), with AGP 9.5.1 and Kotlin 2.4.21. `app/google-services.json` and
`local.properties` are git-ignored.

## Install and pair

1. Install: `~/Android/Sdk/platform-tools/adb install -r app/build/outputs/apk/debug/app-debug.apk`
   (USB debugging, or Wireless debugging: `adb pair <ip:port> <code>`, then `adb connect <ip:port>`).
2. Open **WE Quota**, tap **Pair this phone**. It shows a 6-digit code.
3. On the laptop: `cd ~/fadelkam/server && .venv/bin/python -m app.cloud pair`, check the code
   matches, approve.
4. In the app, tap the buttons that appear: **Allow notifications**, **battery: unrestricted**
   (lets the counter appear when mobile data turns on, and keeps One UI from deep-sleeping the
   app), and **Live Updates** if they're off.
5. Test: `python -m app.cloud test-push` on the laptop.

Debug: `adb logcat -s QuotaService PushService Cellular`.
