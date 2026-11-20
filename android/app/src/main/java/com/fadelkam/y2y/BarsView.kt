package com.fadelkam.y2y

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import com.google.android.material.color.MaterialColors
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/** Daily usage as rounded bars: MB above each bar, the weekday below, today in the strong colour. */
class BarsView @JvmOverloads constructor(c: Context, attrs: AttributeSet? = null) : View(c, attrs) {
    data class Bar(val label: String, val mb: Double, val today: Boolean)

    var bars: List<Bar> = emptyList()
        set(value) {
            field = value
            contentDescription = value.joinToString { "${it.label}: ${it.mb.roundToInt()} MB" }
            invalidate()
        }

    private val dp = resources.displayMetrics.density
    private val sp = resources.displayMetrics.scaledDensity
    // Colours that read in both themes: today in primary, other days primary at 55%, and a track
    // that differs from the card behind it (surfaceContainerHighest is the card's own colour).
    private val strong = MaterialColors.getColor(this, androidx.appcompat.R.attr.colorPrimary)
    private val soft = MaterialColors.compositeARGBWithAlpha(strong, 140)
    private val track = MaterialColors.getColor(this, com.google.android.material.R.attr.colorSecondaryContainer)
    private val ink = MaterialColors.getColor(this, com.google.android.material.R.attr.colorOnSurfaceVariant)
    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { textAlign = Paint.Align.CENTER; color = ink }
    private val box = RectF()

    override fun onDraw(canvas: Canvas) {
        if (bars.isEmpty()) return
        val top = 16 * dp       // room for the MB labels
        val bottom = 20 * dp    // room for the weekday labels
        val chart = height - top - bottom
        val slot = width / bars.size.toFloat()
        val barW = min(slot * 0.5f, 22 * dp)
        val peak = max(bars.maxOf { it.mb }, 1.0)
        bars.forEachIndexed { i, b ->
            val cx = slot * (i + 0.5f)
            box.set(cx - barW / 2, top, cx + barW / 2, top + chart)
            barPaint.color = track
            canvas.drawRoundRect(box, barW / 2, barW / 2, barPaint)
            var barTop = top + chart
            if (b.mb > 0) {
                val h = max((b.mb / peak * chart).toFloat(), 4 * dp)  // a used day always shows
                barTop = top + chart - h
                box.set(cx - barW / 2, barTop, cx + barW / 2, top + chart)
                barPaint.color = if (b.today) strong else soft
                val r = min(barW / 2, h / 2)
                canvas.drawRoundRect(box, r, r, barPaint)
            }
            textPaint.textSize = 10 * sp
            textPaint.isFakeBoldText = b.today
            if (b.mb >= 1) canvas.drawText("${b.mb.roundToInt()}", cx, barTop - 4 * dp, textPaint)
            textPaint.textSize = 11 * sp
            canvas.drawText(b.label, cx, height - 4 * dp, textPaint)
        }
    }
}
