package com.fadelkam.y2y

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.os.Build
import android.util.Base64
import androidx.core.app.NotificationCompat
import androidx.core.app.RemoteInput
import androidx.core.content.ContextCompat
import androidx.core.graphics.drawable.IconCompat
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.roundToInt

object Notifier {
    const val CH_QUOTA = "quota"
    const val CH_ALERTS = "alerts"
    const val ID_QUOTA = 1
    const val ID_CAPTCHA = 2
    const val ID_ALERT = 3

    fun createChannels(c: Context) {
        val nm = c.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CH_QUOTA, "Remaining data", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "The sticky notification with the mobile data left"
                setSound(null, null)
                enableVibration(false)
                setShowBadge(false)
            })
        nm.createNotificationChannel(
            NotificationChannel(CH_ALERTS, "Needs you", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "A captcha to type, or the laptop needs attention"
            })
    }

    // ------------------------------------------------------------------ the sticky one
    /**
     * Ongoing, updated in place. Asks to be a Live Update (Android 16+): One UI shows it as a status
     * bar chip with [shortText] and in the Now Bar. If the system declines, it is a normal ongoing
     * notification. Promotion needs: ongoing, a title, not colorized, a standard or BigText style.
     */
    fun quota(c: Context, s: QuotaState): Notification {
        val stale = s.isStale()
        val problem = problem(s, stale)
        val fraction = fraction(s)
        // Compact on purpose (owner, 2026-11-20: "it is huge"): a title and one line; the picture on
        // the right carries the share left. Renewal, offer and history are in the app.
        val checked = s.fetchedAtMs?.let { "checked ${hhmm(it)}" }
        val b = NotificationCompat.Builder(c, CH_QUOTA)
            .setSmallIcon(ringIcon(c, fraction))
            .setContentTitle(s.remainingMb?.let { "${mb(it)} left" } ?: "No reading yet")
            .setContentText(listOfNotNull(problem ?: s.totalMb?.let { "of ${mb(it)}" }, checked).joinToString(" · "))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_STATUS)
            .setContentIntent(openApp(c))
            // Android 14+ lets a swipe dismiss even this; put it straight back.
            .setDeleteIntent(broadcast(c, ActionReceiver.RESHOW, 10))
            .addAction(0, "Refresh", broadcast(c, ActionReceiver.REFRESH, 11))
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setRequestPromotedOngoing(true)
            .setShortCriticalText(shortText(s, stale))
        val fetched = s.fetchedAtMs
        if (fetched != null) b.setWhen(fetched).setShowWhen(true) else b.setShowWhen(false)
        if (fraction != null) b.setLargeIcon(ringLarge(c, fraction))
        return b.build()
    }

    private fun fraction(s: QuotaState): Float? {
        val total = s.totalMb ?: return null
        val left = s.remainingMb ?: return null
        return if (total > 0) (left / total).toFloat().coerceIn(0f, 1f) else null
    }

    /**
     * Status bar icon (also in the Live Update chip): the data arrows inside a ring that fills with
     * the share of data left. Android paints small icons in one colour from their alpha, so the
     * empty part of the ring is faint and the filled part solid.
     */
    private fun ringIcon(c: Context, fraction: Float?): IconCompat {
        val size = 96
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bmp)
        val stroke = size * 0.12f
        val box = RectF(stroke / 2, stroke / 2, size - stroke / 2, size - stroke / 2)
        val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE; strokeWidth = stroke; color = Color.WHITE; strokeCap = Paint.Cap.ROUND
        }
        paint.alpha = 85
        canvas.drawOval(box, paint)
        if (fraction != null && fraction > 0f) {
            paint.alpha = 255
            canvas.drawArc(box, -90f, 360f * fraction, false, paint)
        }
        ContextCompat.getDrawable(c, R.drawable.ic_stat_data)?.mutate()?.let { arrows ->
            val inset = (size * 0.27f).toInt()
            arrows.setBounds(inset, inset, size - inset, size - inset)
            arrows.setTint(Color.WHITE)
            arrows.draw(canvas)
        }
        return IconCompat.createWithBitmap(bmp)
    }

    /**
     * The picture on the right of the notification: a solid purple disc with a white ring (share of
     * data left) and the white percentage. Readable on light and dark notification backgrounds
     * alike; the first version, a purple ring on a transparent background, vanished in dark mode
     * (owner, 2026-11-20).
     */
    private fun ringLarge(c: Context, fraction: Float): Bitmap {
        val size = 192
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bmp)
        val disc = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = ContextCompat.getColor(c, R.color.notif_disc) }
        canvas.drawCircle(size / 2f, size / 2f, size / 2f, disc)
        val stroke = size * 0.085f
        val inset = stroke / 2 + size * 0.08f
        val box = RectF(inset, inset, size - inset, size - inset)
        val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE; strokeWidth = stroke; strokeCap = Paint.Cap.ROUND; color = Color.WHITE
        }
        ring.alpha = 70
        canvas.drawOval(box, ring)
        ring.alpha = 255
        if (fraction > 0f) canvas.drawArc(box, -90f, 360f * fraction, false, ring)
        val text = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.WHITE; textAlign = Paint.Align.CENTER; textSize = size * 0.24f; isFakeBoldText = true
        }
        val y = size / 2f - (text.descent() + text.ascent()) / 2
        canvas.drawText("${(fraction * 100).roundToInt()}%", size / 2f, y, text)
        return bmp
    }

    fun showQuota(c: Context, s: QuotaState) = notify(c, ID_QUOTA, quota(c, s))

    fun cancelQuota(c: Context) = c.getSystemService(NotificationManager::class.java).cancel(ID_QUOTA)

    fun problem(s: QuotaState, stale: Boolean = s.isStale()): String? = when {
        s.state == "unpaired" -> "Not paired: open the app"
        s.state == "stopped" -> "Laptop server stopped"
        stale -> "Laptop offline since ${s.heartbeatAtMs?.let(::hhmm) ?: "?"}"
        s.state == "challenge_pending" -> "Captcha waiting"
        s.state == "needs_human" -> "Login blocked: check the laptop"
        s.state == "login_failed" -> "Login failed: check the laptop"
        s.state in setOf("degraded", "session_expired", "login_blocked") -> "Retrying (${s.state})"
        s.state in setOf("broken", "error") -> "Scraper problem: check the laptop"
        else -> null
    }

    /** Status bar chip: a few characters only. */
    private fun shortText(s: QuotaState, stale: Boolean): String {
        val left = s.remainingMb ?: return "—"
        val v = if (left >= 1024) String.format(Locale.US, "%.2f GB", left / 1024)
                else String.format(Locale.US, "%.0f MB", left)
        return if (stale || s.state == "stopped") "$v?" else v
    }

    // ------------------------------------------------------------------ captcha
    /** Captcha from the laptop: the image, plus an inline reply box. Once per captcha id. */
    fun showCaptchaOnce(c: Context, id: String, imageB64: String?, timeoutS: Int) {
        if (Store.shownChallenge(c) == id) return
        imageB64?.let { b64 -> runCatching { Store.captchaFile(c).writeBytes(Base64.decode(b64, Base64.DEFAULT)) } }
            ?: Store.captchaFile(c).delete()
        Store.setShownChallenge(c, id)
        showCaptcha(c, id, timeoutS, note = null)
    }

    fun showCaptcha(c: Context, id: String, timeoutS: Int, note: String?) {
        val bmp: Bitmap? = Store.captchaFile(c).takeIf { it.exists() }?.let { BitmapFactory.decodeFile(it.path) }
        val input = RemoteInput.Builder(ActionReceiver.KEY_CODE).setLabel("Code from the picture").build()
        val reply = PendingIntent.getBroadcast(
            c, 20,
            Intent(c, ActionReceiver::class.java).setAction(ActionReceiver.ANSWER)
                .putExtra(ActionReceiver.EXTRA_ID, id).putExtra(ActionReceiver.EXTRA_TIMEOUT, timeoutS),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE) // RemoteInput fills it in
        val b = NotificationCompat.Builder(c, CH_ALERTS)
            .setSmallIcon(R.drawable.ic_stat_data)
            .setContentTitle(note ?: "WE wants a captcha")
            .setContentText(if (bmp == null) "Open the app to see it" else "Type the code you see (${timeoutS / 60} min)")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setOnlyAlertOnce(note != null)
            .setTimeoutAfter(timeoutS * 1000L)
            .setContentIntent(openApp(c))
            .addAction(NotificationCompat.Action.Builder(0, "Type code", reply).addRemoteInput(input)
                .setAllowGeneratedReplies(false).build())
        if (bmp != null) {
            b.setLargeIcon(bmp).setStyle(NotificationCompat.BigPictureStyle().bigPicture(bmp).bigLargeIcon(null as Bitmap?))
        }
        notify(c, ID_CAPTCHA, b.build())
    }

    /** Replaces the captcha notification after a reply (otherwise its spinner never stops). */
    fun captchaInfo(c: Context, title: String, text: String) {
        notify(c, ID_CAPTCHA, NotificationCompat.Builder(c, CH_ALERTS)
            .setSmallIcon(R.drawable.ic_stat_data)
            .setContentTitle(title).setContentText(text)
            .setSilent(true).setTimeoutAfter(5 * 60_000L).setAutoCancel(true)
            .setContentIntent(openApp(c))
            .build())
    }

    fun cancelCaptcha(c: Context) {
        Store.setShownChallenge(c, null)
        c.getSystemService(NotificationManager::class.java).cancel(ID_CAPTCHA)
    }

    // ------------------------------------------------------------------ alerts
    fun alert(c: Context, title: String, text: String) {
        notify(c, ID_ALERT, NotificationCompat.Builder(c, CH_ALERTS)
            .setSmallIcon(R.drawable.ic_stat_data)
            .setContentTitle(title).setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(openApp(c))
            .build())
    }

    fun cancelAlert(c: Context) = c.getSystemService(NotificationManager::class.java).cancel(ID_ALERT)

    // ------------------------------------------------------------------ helpers
    /** Exact MB ("3,109 MB"): WE's number moves in steps of a few MB, which GB would hide. */
    fun mb(v: Double): String = String.format(Locale.US, "%,.0f MB", v)

    fun size(mb: Double): String =
        if (mb >= 1024) String.format(Locale.US, "%.2f GB", mb / 1024) else String.format(Locale.US, "%.0f MB", mb)

    fun hhmm(ms: Long): String =
        Instant.ofEpochMilli(ms).atZone(ZoneId.systemDefault()).format(DateTimeFormatter.ofPattern("HH:mm"))

    fun canNotify(c: Context): Boolean = Build.VERSION.SDK_INT < 33 ||
        c.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    private fun notify(c: Context, id: Int, n: Notification) {
        if (canNotify(c)) c.getSystemService(NotificationManager::class.java).notify(id, n)
    }

    private fun openApp(c: Context): PendingIntent = PendingIntent.getActivity(
        c, 0, Intent(c, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

    private fun broadcast(c: Context, action: String, request: Int): PendingIntent = PendingIntent.getBroadcast(
        c, request, Intent(c, ActionReceiver::class.java).setAction(action),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
}
