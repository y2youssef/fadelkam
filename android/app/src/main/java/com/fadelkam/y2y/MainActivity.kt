package com.fadelkam.y2y

import android.Manifest
import android.app.NotificationManager
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.util.Base64
import android.view.Gravity
import android.view.View
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import androidx.core.widget.TextViewCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.google.android.material.chip.Chip
import com.google.android.material.color.MaterialColors
import com.google.android.material.progressindicator.CircularProgressIndicator
import com.google.android.material.textfield.TextInputEditText
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
 * Pairing, the current reading, usage, a captcha at full size, and the switches the sticky
 * notification depends on (notification permission, battery exemption, Live Updates). While this
 * screen is visible it listens to /state, /challenge and /usage live; it stops as soon as it's hidden.
 */
class MainActivity : ComponentActivity() {
    private val zone: ZoneId = ZoneId.of("Africa/Cairo")

    private lateinit var statusChip: Chip
    private lateinit var readingCard: MaterialCardView
    private lateinit var ring: CircularProgressIndicator
    private lateinit var leftMb: TextView
    private lateinit var ringPct: TextView
    private lateinit var ofTotal: TextView
    private lateinit var times: TextView
    private lateinit var problem: TextView
    private lateinit var refreshBtn: MaterialButton
    private lateinit var captchaCard: MaterialCardView
    private lateinit var captchaImg: ImageView
    private lateinit var captchaInput: TextInputEditText
    private lateinit var usageCard: MaterialCardView
    private lateinit var todayMb: TextView
    private lateinit var bars: BarsView
    private lateinit var sessionsList: LinearLayout
    private lateinit var pairCard: MaterialCardView
    private lateinit var pairHint: TextView
    private lateinit var codeView: TextView
    private lateinit var pairBtn: MaterialButton
    private lateinit var setupCard: MaterialCardView
    private lateinit var notifBtn: MaterialButton
    private lateinit var batteryBtn: MaterialButton
    private lateinit var liveBtn: MaterialButton
    private lateinit var footer: TextView

    private var challengeId: String? = null
    private var listening = false
    private var allowedRef: DatabaseReference? = null
    private var allowedListener: ValueEventListener? = null
    private var message: String? = null // connection / pairing message for the footer

    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { render() }

    private val stateListener = object : ValueEventListener {
        override fun onDataChange(s: DataSnapshot) {
            // Through the same path as pushes: the notification updates too (it didn't, 2026-11-20).
            if (s.exists()) Store.receive(this@MainActivity, QuotaState.fromSnapshot(s))
            render()
        }

        override fun onCancelled(e: DatabaseError) {
            say("Can't read the laptop's data: ${e.message}")
            if (e.code == DatabaseError.PERMISSION_DENIED) { // unpaired on the laptop (cloud revoke)
                unlisten()
                Store.setPaired(this@MainActivity, false)
                lifecycleScope.launch { connect() }
            }
        }
    }

    private val challengeListener = object : ValueEventListener {
        override fun onDataChange(s: DataSnapshot) {
            challengeId = s.child("id").value as? String
            val img = (s.child("image_png_b64").value as? String)?.let {
                runCatching { Base64.decode(it, Base64.DEFAULT) }.getOrNull()
            }
            captchaCard.isVisible = challengeId != null
            captchaImg.setImageBitmap(img?.let { BitmapFactory.decodeByteArray(it, 0, it.size) })
        }

        override fun onCancelled(e: DatabaseError) = Unit
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

    // ------------------------------------------------------------------ lifecycle
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContentView(R.layout.activity_main)
        bindViews()
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

    private fun bindViews() {
        statusChip = findViewById(R.id.statusChip)
        readingCard = findViewById(R.id.readingCard)
        ring = findViewById(R.id.ring)
        leftMb = findViewById(R.id.leftMb)
        ringPct = findViewById(R.id.ringPct)
        ofTotal = findViewById(R.id.ofTotal)
        times = findViewById(R.id.times)
        problem = findViewById(R.id.problem)
        refreshBtn = findViewById(R.id.refreshBtn)
        captchaCard = findViewById(R.id.captchaCard)
        captchaImg = findViewById(R.id.captchaImg)
        captchaInput = findViewById(R.id.captchaInput)
        usageCard = findViewById(R.id.usageCard)
        todayMb = findViewById(R.id.todayMb)
        bars = findViewById(R.id.bars)
        sessionsList = findViewById(R.id.sessionsList)
        pairCard = findViewById(R.id.pairCard)
        pairHint = findViewById(R.id.pairHint)
        codeView = findViewById(R.id.codeView)
        pairBtn = findViewById(R.id.pairBtn)
        setupCard = findViewById(R.id.setupCard)
        notifBtn = findViewById(R.id.notifBtn)
        batteryBtn = findViewById(R.id.batteryBtn)
        liveBtn = findViewById(R.id.liveBtn)
        footer = findViewById(R.id.footer)

        refreshBtn.setOnClickListener { refresh() }
        pairBtn.setOnClickListener { pair() }
        findViewById<View>(R.id.captchaSend).setOnClickListener { sendCaptcha() }
        notifBtn.setOnClickListener {
            if (Build.VERSION.SDK_INT >= 33) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        batteryBtn.setOnClickListener {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }
        liveBtn.setOnClickListener {
            if (Build.VERSION.SDK_INT >= 36) {
                startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_PROMOTION_SETTINGS)
                    .putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
            }
        }
        val root = findViewById<View>(R.id.root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime()
                or WindowInsetsCompat.Type.displayCutout())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
    }

    // ------------------------------------------------------------------ pairing
    private suspend fun connect() {
        say("Connecting…")
        val uid = try {
            Cloud.signIn()
        } catch (e: Exception) {
            say("Sign-in failed (${e.javaClass.simpleName}). Check the connection and reopen the app.")
            return
        }
        val allowed = try {
            Cloud.isAllowed(uid)
        } catch (e: Exception) {
            say("Firebase unreachable (${e.javaClass.simpleName}). Reopen the app to retry.")
            return
        }
        if (allowed) {
            onPaired()
        } else {
            Store.setPaired(this, false)
            render()
            if (codeView.isVisible) waitForApproval(uid) // a code is on screen: keep waiting for it
            else pairHint.text = "Tap below for a 6-digit code, then approve it on the laptop."
            say(null)
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
                say("Couldn't send the pairing request (${e.javaClass.simpleName}).")
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
        pairHint.text = "On the laptop run  python -m app.cloud pair  and approve it if it shows this code."
        if (allowedRef != null) return
        val ref = Cloud.db.getReference("allowed").child(uid)
        val l = object : ValueEventListener {
            override fun onDataChange(s: DataSnapshot) {
                if (!s.exists()) return
                stopWaitingForApproval()
                lifecycleScope.launch { onPaired() }
            }

            override fun onCancelled(e: DatabaseError) = say("Waiting failed: ${e.message}")
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
        codeView.isVisible = false
        say(null)
        registerForPush()
        Cellular.arm(this)
        Cellular.showIfWeData(this, "paired")
        listen()
        render()
    }

    /** Push id up to date (it can change), and a hello so the laptop's log shows the phone. */
    private suspend fun registerForPush() {
        try {
            Cloud.registerDevice()
            Cloud.inbox("hello")
        } catch (e: Exception) {
            say("Push registration failed (${e.javaClass.simpleName}). Reopen the app to retry.")
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
        refreshBtn.isEnabled = false
        lifecycleScope.launch {
            val ok = runCatching { Cloud.inbox("refresh") }.isSuccess
            Toast.makeText(this@MainActivity,
                if (ok) "Asked the laptop to check now" else "Couldn't reach Firebase", Toast.LENGTH_SHORT).show()
            refreshBtn.isEnabled = true
        }
    }

    private fun sendCaptcha() {
        val id = challengeId ?: return
        val code = captchaInput.text?.toString()?.trim().orEmpty()
        if (!ActionReceiver.CODE_RE.matches(code)) {
            captchaInput.error = "3 to 12 letters or digits"
            return
        }
        lifecycleScope.launch {
            val ok = runCatching { Cloud.inbox("answer", mapOf("id" to id, "code" to code)) }.isSuccess
            if (ok) {
                captchaInput.text?.clear()
                Notifier.captchaInfo(this@MainActivity, "Code sent", "The laptop is typing it in.")
            }
            Toast.makeText(this@MainActivity, if (ok) "Code sent" else "Not sent, try again", Toast.LENGTH_SHORT).show()
        }
    }

    // ------------------------------------------------------------------ screen
    private fun say(text: String?) {
        message = text
        if (::footer.isInitialized) render()
    }

    private fun render() {
        val s = Store.load(this)
        val paired = Store.isPaired(this)
        readingCard.isVisible = paired
        usageCard.isVisible = paired
        pairCard.isVisible = !paired

        val total = s.totalMb
        val left = s.remainingMb
        val share = if (total != null && left != null && total > 0) (left / total).coerceIn(0.0, 1.0) else null
        leftMb.text = left?.let { "${Notifier.mb(it)} left" } ?: "No reading yet"
        ringPct.text = share?.let { "${(it * 100).roundToInt()}%" } ?: "—"
        ring.setProgressCompat(((share ?: 0.0) * 1000).toInt(), true)
        ofTotal.text = total?.let { "of ${Notifier.mb(it)}" } ?: ""
        times.text = listOfNotNull(
            s.renewsInDays?.let { "Renews in $it days" },
            listOfNotNull(
                s.fetchedAtMs?.let { "Checked ${Notifier.hhmm(it)}" },
                s.changedAtMs?.let { "changed ${Notifier.hhmm(it)}" },
            ).joinToString(" · ").ifEmpty { null },
        ).joinToString("\n")
        val trouble = Notifier.problem(s)
        problem.text = trouble
        problem.isVisible = trouble != null

        val weData = Cellular.isUp(this)
        statusChip.text = when {
            !paired -> "Not paired"
            trouble != null && s.isStale() -> "Laptop offline"
            trouble != null -> "Needs attention"
            weData -> "WE data on"
            else -> "WE data off"
        }

        val needNotif = !Notifier.canNotify(this)
        val needBattery = !getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)
        val needLive = Build.VERSION.SDK_INT >= 36 &&
            !getSystemService(NotificationManager::class.java).canPostPromotedNotifications()
        notifBtn.isVisible = needNotif
        batteryBtn.isVisible = needBattery
        liveBtn.isVisible = needLive
        setupCard.isVisible = paired && (needNotif || needBattery || needLive)

        footer.text = message ?: when {
            !paired -> ""
            weData -> "The counter is in your notifications while WE mobile data is on."
            Cellular.cellularUp(this) -> "Mobile data is on the other SIM: the WE counter is hidden."
            else -> "Mobile data is off: the counter is hidden."
        }
        renderUsage()
    }

    /** Today, a bar per day for the last 7 days, and the last sessions. Days are Cairo days. */
    private fun renderUsage() {
        val today = LocalDate.now(zone).toString()
        todayMb.text = "Today ${Notifier.mb(daily.firstOrNull { it.first == today }?.second ?: 0.0)}"
        // Always the last 7 days, so the chart keeps its shape (a day with no reading is 0).
        val weekday = DateTimeFormatter.ofPattern("EEE", Locale.US)
        val byDay = daily.toMap()
        val todayDate = LocalDate.now(zone)
        bars.bars = (6 downTo 0).map { back ->
            val d = todayDate.minusDays(back.toLong())
            BarsView.Bar(d.format(weekday), byDay[d.toString()] ?: 0.0, back == 0)
        }

        sessionsList.removeAllViews()
        if (sessions.isEmpty()) {
            sessionsList.addView(row("None yet: one starts when WE mobile data turns on", null))
            return
        }
        val start = DateTimeFormatter.ofPattern("EEE HH:mm", Locale.US).withZone(zone)
        val time = DateTimeFormatter.ofPattern("HH:mm", Locale.US).withZone(zone)
        for ((from, to, mb) in sessions.take(5)) {
            val end = to?.let { time.format(Instant.ofEpochMilli(it)) } ?: "now"
            sessionsList.addView(row("${start.format(Instant.ofEpochMilli(from))} → $end",
                String.format(Locale.US, "%.1f MB", mb)))
        }
    }

    private fun row(left: String, right: String?): View {
        val pad = (6 * resources.displayMetrics.density).toInt()
        return LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, pad, 0, pad)
            addView(TextView(context).apply {
                text = left
                TextViewCompat.setTextAppearance(this, com.google.android.material.R.style.TextAppearance_Material3_BodyMedium)
                setTextColor(MaterialColors.getColor(this, com.google.android.material.R.attr.colorOnSurface))
            }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            if (right != null) addView(TextView(context).apply {
                text = right
                gravity = Gravity.END
                fontFeatureSettings = "tnum"
                TextViewCompat.setTextAppearance(this, com.google.android.material.R.style.TextAppearance_Material3_BodyMedium)
                setTextColor(MaterialColors.getColor(this, com.google.android.material.R.attr.colorOnSurfaceVariant))
            })
        }
    }
}
