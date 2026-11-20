package com.fadelkam.y2y

import android.Manifest
import android.app.NotificationManager
import android.content.Intent
import android.graphics.BitmapFactory
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.util.Base64
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import androidx.lifecycle.lifecycleScope
import com.google.firebase.database.DataSnapshot
import com.google.firebase.database.DatabaseError
import com.google.firebase.database.DatabaseReference
import com.google.firebase.database.ValueEventListener
import kotlinx.coroutines.launch
import java.security.SecureRandom
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.roundToInt

/**
 * Pairing, the current reading, a captcha at full size, and the switches the sticky notification
 * depends on (notification permission, battery exemption, Live Updates). While this screen is
 * visible it listens to /state and /challenge live; it stops listening as soon as it's hidden.
 */
class MainActivity : ComponentActivity() {
    private lateinit var reading: TextView
    private lateinit var details: TextView
    private lateinit var usage: TextView
    private lateinit var status: TextView
    private lateinit var codeView: TextView
    private lateinit var pairBtn: Button
    private lateinit var refreshBtn: Button
    private lateinit var notifBtn: Button
    private lateinit var batteryBtn: Button
    private lateinit var liveBtn: Button
    private lateinit var captchaBox: LinearLayout
    private lateinit var captchaImg: ImageView
    private lateinit var captchaInput: EditText

    private var challengeId: String? = null
    private var listening = false
    private var allowedRef: DatabaseReference? = null
    private var allowedListener: ValueEventListener? = null

    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { render() }

    private val stateListener = object : ValueEventListener {
        override fun onDataChange(s: DataSnapshot) {
            // Through the same path as pushes: the notification updates too (it didn't, 2026-11-20).
            if (s.exists()) Store.receive(this@MainActivity, QuotaState.fromSnapshot(s))
            render()
        }

        override fun onCancelled(e: DatabaseError) {
            status.text = "Can't read the laptop's data: ${e.message}"
            if (e.code == DatabaseError.PERMISSION_DENIED) { // unpaired on the laptop (cloud revoke)
                unlisten()
                Store.setPaired(this@MainActivity, false)
                lifecycleScope.launch { connect() }
            }
        }
    }

    // ------------------------------------------------------------------ usage (laptop's /usage)
    private val dailyQuery by lazy { Cloud.db.getReference("usage/daily").orderByKey().limitToLast(7) }
    private val sessionsQuery by lazy { Cloud.db.getReference("usage/sessions").orderByKey().limitToLast(10) }
    private var daily: List<Pair<String, Double>> = emptyList()
    private var sessions: List<Triple<Long, Long?, Double>> = emptyList() // start, end (null: on), MB

    private val dailyListener = object : ValueEventListener {
        override fun onDataChange(s: DataSnapshot) {
            daily = s.children.mapNotNull { d ->
                val key = d.key ?: return@mapNotNull null
                (d.child("used_mb").value as? Number)?.toDouble()?.let { key to it }
            }
            renderUsage()
        }

        override fun onCancelled(e: DatabaseError) = Unit
    }

    private val sessionsListener = object : ValueEventListener {
        override fun onDataChange(s: DataSnapshot) {
            sessions = s.children.mapNotNull { x ->
                val start = x.key?.toLongOrNull() ?: return@mapNotNull null
                val end = (x.child("end").value as? String)?.let {
                    runCatching { OffsetDateTime.parse(it).toInstant().toEpochMilli() }.getOrNull()
                }
                Triple(start, end, (x.child("used_mb").value as? Number)?.toDouble() ?: 0.0)
            }.reversed()
            renderUsage()
        }

        override fun onCancelled(e: DatabaseError) = Unit
    }

    /** Today, a bar per day for the last 7 days, and the last sessions. Days are Cairo days. */
    private fun renderUsage() {
        val zone = ZoneId.of("Africa/Cairo")
        val today = LocalDate.now(zone).toString()
        val sb = StringBuilder("Today: ${Notifier.mb(daily.firstOrNull { it.first == today }?.second ?: 0.0)}\n\n")
        val max = (daily.maxOfOrNull { it.second } ?: 0.0).coerceAtLeast(1.0)
        for ((day, mb) in daily) {
            val label = LocalDate.parse(day).format(DateTimeFormatter.ofPattern("EEE d", Locale.US))
            val bar = "█".repeat((mb / max * 14).roundToInt()).ifEmpty { if (mb > 0) "▏" else "" }
            sb.append(String.format(Locale.US, "%-7s %-14s %6.0f MB\n", label, bar, mb))
        }
        if (sessions.isNotEmpty()) {
            sb.append("\nSessions (WE data on → off)\n")
            val day = DateTimeFormatter.ofPattern("EEE HH:mm", Locale.US).withZone(zone)
            val time = DateTimeFormatter.ofPattern("HH:mm", Locale.US).withZone(zone)
            for ((start, end, mb) in sessions) {
                val e = end?.let { time.format(Instant.ofEpochMilli(it)) } ?: "now"
                sb.append(String.format(Locale.US, "%s → %-5s %7.1f MB\n", day.format(Instant.ofEpochMilli(start)), e, mb))
            }
        }
        usage.text = sb.toString()
    }

    private val challengeListener = object : ValueEventListener {
        override fun onDataChange(s: DataSnapshot) {
            challengeId = s.child("id").value as? String
            val img = (s.child("image_png_b64").value as? String)?.let {
                runCatching { Base64.decode(it, Base64.DEFAULT) }.getOrNull()
            }
            captchaBox.isVisible = challengeId != null
            captchaImg.setImageBitmap(img?.let { BitmapFactory.decodeByteArray(it, 0, it.size) })
        }

        override fun onCancelled(e: DatabaseError) = Unit
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContentView(buildUi())
        if (Store.isPaired(this)) lifecycleScope.launch { registerForPush() }
    }

    override fun onStart() {
        super.onStart()
        if (Store.isPaired(this)) {
            listen()
            Cellular.arm(this)                               // heal a lost trigger
            Cellular.showIfWeData(this, "app opened")        // and show the counter if it's missing
        } else {
            lifecycleScope.launch { connect() }
        }
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    override fun onStop() {
        unlisten()
        super.onStop()
    }

    // ------------------------------------------------------------------ pairing
    private suspend fun connect() {
        status.text = "Connecting…"
        val uid = try {
            Cloud.signIn()
        } catch (e: Exception) {
            status.text = "Sign-in failed (${e.javaClass.simpleName}). Check the connection and reopen the app."
            return
        }
        val allowed = try {
            Cloud.isAllowed(uid)
        } catch (e: Exception) {
            status.text = "Firebase unreachable (${e.javaClass.simpleName}). Reopen the app to retry."
            return
        }
        if (allowed) {
            onPaired()
        } else {
            Store.setPaired(this, false)
            pairBtn.isVisible = true
            if (codeView.isVisible) waitForApproval(uid) // a code is on screen: keep waiting for it
            else status.text = "This phone isn't paired with the laptop yet."
        }
    }

    private fun pair() {
        val uid = Cloud.uid ?: return
        val code = String.format(Locale.US, "%06d", SecureRandom().nextInt(1_000_000))
        pairBtn.isEnabled = false
        lifecycleScope.launch {
            try {
                Cloud.requestPairing(uid, code)
            } catch (e: Exception) {
                status.text = "Couldn't send the pairing request (${e.javaClass.simpleName})."
                pairBtn.isEnabled = true
                return@launch
            }
            codeView.text = code
            codeView.isVisible = true
            pairBtn.text = "New code"
            pairBtn.isEnabled = true
            waitForApproval(uid)
        }
    }

    /** Live while this screen is visible; [connect] re-checks when it comes back. */
    private fun waitForApproval(uid: String) {
        status.text = "On the laptop run\n    python -m app.cloud pair\nand approve it if it shows this code. Waiting…"
        if (allowedRef != null) return
        val ref = Cloud.db.getReference("allowed").child(uid)
        val l = object : ValueEventListener {
            override fun onDataChange(s: DataSnapshot) {
                if (!s.exists()) return
                stopWaitingForApproval()
                lifecycleScope.launch { onPaired() }
            }

            override fun onCancelled(e: DatabaseError) {
                status.text = "Waiting failed: ${e.message}"
            }
        }
        Cloud.acquire()
        ref.addValueEventListener(l)
        allowedRef = ref
        allowedListener = l
    }

    private fun stopWaitingForApproval() {
        val ref = allowedRef ?: return
        allowedListener?.let { ref.removeEventListener(it) }
        allowedRef = null
        allowedListener = null
        Cloud.release()
    }

    private suspend fun onPaired() {
        Store.setPaired(this, true)
        pairBtn.isVisible = false
        codeView.isVisible = false
        status.text = "Paired."
        registerForPush()
        Cellular.arm(this)
        Cellular.showIfWeData(this, "paired")
        refreshBtn.isVisible = true
        listen()
        render()
    }

    /** Push token up to date (it can change), and a hello so the laptop's log shows the phone. */
    private suspend fun registerForPush() {
        try {
            Cloud.registerDevice()
            Cloud.inbox("hello")
        } catch (e: Exception) {
            status.text = "Push registration failed (${e.javaClass.simpleName}). Reopen the app to retry."
        }
    }

    // ------------------------------------------------------------------ live while visible
    private fun listen() {
        if (listening || !Store.isPaired(this)) return
        listening = true
        Cloud.acquire()
        Cloud.db.getReference("state").addValueEventListener(stateListener)
        Cloud.db.getReference("challenge").addValueEventListener(challengeListener)
        dailyQuery.addValueEventListener(dailyListener)
        sessionsQuery.addValueEventListener(sessionsListener)
    }

    private fun unlisten() {
        stopWaitingForApproval()
        if (!listening) return
        listening = false
        Cloud.db.getReference("state").removeEventListener(stateListener)
        Cloud.db.getReference("challenge").removeEventListener(challengeListener)
        dailyQuery.removeEventListener(dailyListener)
        sessionsQuery.removeEventListener(sessionsListener)
        Cloud.release()
    }

    private fun refresh() {
        lifecycleScope.launch {
            val ok = runCatching { Cloud.inbox("refresh") }.isSuccess
            Toast.makeText(this@MainActivity,
                if (ok) "Asked the laptop to check now" else "Couldn't reach Firebase", Toast.LENGTH_SHORT).show()
        }
    }

    private fun sendCaptcha() {
        val id = challengeId ?: return
        val code = captchaInput.text.toString().trim()
        if (!ActionReceiver.CODE_RE.matches(code)) {
            captchaInput.error = "3 to 12 letters or digits"
            return
        }
        lifecycleScope.launch {
            val ok = runCatching { Cloud.inbox("answer", mapOf("id" to id, "code" to code)) }.isSuccess
            if (ok) {
                captchaInput.text.clear()
                Notifier.captchaInfo(this@MainActivity, "Code sent", "The laptop is typing it in.")
            }
            Toast.makeText(this@MainActivity, if (ok) "Code sent" else "Not sent, try again", Toast.LENGTH_SHORT).show()
        }
    }

    // ------------------------------------------------------------------ screen
    private fun render() {
        val s = Store.load(this)
        reading.text = s.remainingMb?.let { "${Notifier.mb(it)} left" } ?: "No reading yet"
        val lines = mutableListOf<String>()
        s.totalMb?.let { lines += "of ${Notifier.mb(it)} (${Notifier.size(it)})" + (s.renewsInDays?.let { d -> ", renews in $d days" } ?: "") }
        s.offer?.let { lines += it }
        s.fetchedAtMs?.let { lines += "Reading from ${Notifier.hhmm(it)}" }
        s.state?.let { lines += "Laptop: $it" + (s.error?.let { e -> " ($e)" } ?: "") }
        if (s.isStale()) lines += "No word from the laptop since ${s.heartbeatAtMs?.let(Notifier::hhmm)}"
        lines += when {
            Cellular.isUp(this) -> "Using WE mobile data: the counter is in your notifications"
            Cellular.cellularUp(this) -> "Mobile data is on the other SIM: the WE counter is hidden"
            else -> "Mobile data off: the counter is hidden"
        }
        details.text = lines.joinToString("\n")

        notifBtn.isVisible = !Notifier.canNotify(this)
        batteryBtn.isVisible = !getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)
        liveBtn.isVisible = Build.VERSION.SDK_INT >= 36 &&
            !getSystemService(NotificationManager::class.java).canPostPromotedNotifications()
        refreshBtn.isVisible = Store.isPaired(this)
    }

    private fun buildUi(): View {
        val pad = (16 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        fun text(sizeSp: Float, bold: Boolean = false) = TextView(this).apply {
            textSize = sizeSp
            if (bold) setTypeface(typeface, Typeface.BOLD)
            setPadding(0, pad / 2, 0, pad / 2)
        }.also { col.addView(it) }
        fun button(label: String, onClick: () -> Unit) = Button(this).apply {
            text = label
            isAllCaps = false
            setOnClickListener { onClick() }
            isVisible = false
        }.also { col.addView(it) }

        text(22f, bold = true).text = "WE Quota"
        reading = text(34f, bold = true)
        details = text(16f)
        usage = text(14f).apply { typeface = Typeface.MONOSPACE }
        status = text(15f)
        codeView = text(44f, bold = true).apply { letterSpacing = 0.2f; isVisible = false }
        pairBtn = button("Pair this phone") { pair() }
        refreshBtn = button("Check now") { refresh() }

        captchaBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; isVisible = false }
        captchaBox.addView(TextView(this).apply { text = "WE wants a captcha:"; textSize = 18f })
        captchaImg = ImageView(this).apply { adjustViewBounds = true; setPadding(0, pad / 2, 0, pad / 2) }
        captchaBox.addView(captchaImg)
        captchaInput = EditText(this).apply {
            hint = "Code from the picture"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        }
        captchaBox.addView(captchaInput)
        captchaBox.addView(Button(this).apply { text = "Send code"; isAllCaps = false; setOnClickListener { sendCaptcha() } })
        col.addView(captchaBox)

        notifBtn = button("Allow notifications") {
            if (Build.VERSION.SDK_INT >= 33) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        batteryBtn = button("Let it run in the background (battery: unrestricted)") {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }
        liveBtn = button("Turn on Live Updates (status bar chip)") {
            if (Build.VERSION.SDK_INT >= 36) {
                startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_PROMOTION_SETTINGS)
                    .putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
            }
        }

        val root = ScrollView(this).apply { addView(col) }
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime()
                or WindowInsetsCompat.Type.displayCutout())
            v.setPadding(bars.left + pad, bars.top + pad, bars.right + pad, bars.bottom + pad)
            insets
        }
        return root
    }
}
