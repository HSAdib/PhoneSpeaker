package com.hasan.phonespeaker

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.util.AttributeSet
import android.util.TypedValue
import android.view.View
import java.util.ArrayDeque

class LatencyGraphView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null
) : View(context, attrs) {

    private val maxPoints = 60
    private val bufferHistory = ArrayDeque<Float>(maxPoints)
    private var currentTargetMs = 30f
    private var currentBufferMs = 0f
    private var estTotalMs = 55f

    private val bgPaint = Paint().apply { color = Color.parseColor("#0A0A0A"); style = Paint.Style.FILL }
    private val headerPaint = Paint().apply { color = Color.parseColor("#141414"); style = Paint.Style.FILL }
    private val borderPaint = Paint().apply { color = Color.parseColor("#444444"); style = Paint.Style.STROKE; strokeWidth = 2f }
    private val gridPaint = Paint().apply { color = Color.parseColor("#222222"); style = Paint.Style.STROKE; strokeWidth = 1f }
    private val targetPaint = Paint().apply {
        color = Color.parseColor("#888888")
        style = Paint.Style.STROKE
        strokeWidth = 2f
        pathEffect = DashPathEffect(floatArrayOf(8f, 8f), 0f)
    }
    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#00FF00")
        style = Paint.Style.STROKE
        strokeWidth = 3f
    }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        typeface = android.graphics.Typeface.MONOSPACE
    }
    private val path = Path()

    init {
        textPaint.textSize = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, 11f, resources.displayMetrics)
        for (i in 0 until maxPoints) bufferHistory.add(0f)
    }

    fun pushSample(bufferMs: Float, targetMs: Float, totalEstimatedMs: Float) {
        if (bufferHistory.size >= maxPoints) bufferHistory.pollFirst()
        bufferHistory.addLast(bufferMs.coerceIn(0f, 200f))
        currentBufferMs = bufferMs
        currentTargetMs = targetMs
        estTotalMs = totalEstimatedMs
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0f || h <= 0f) return

        val headerH = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, 24f, resources.displayMetrics)
        val plotH = (h - headerH).coerceAtLeast(10f)

        canvas.drawRect(0f, 0f, w, h, bgPaint)
        val maxScaleMs = 160f

        for (ms in listOf(40f, 80f, 120f)) {
            val y = h - (ms / maxScaleMs) * plotH
            canvas.drawLine(0f, y, w, y, gridPaint)
        }

        val targetY = (h - (currentTargetMs.coerceIn(0f, maxScaleMs) / maxScaleMs) * plotH).coerceIn(headerH + 2f, h - 2f)
        canvas.drawLine(0f, targetY, w, targetY, targetPaint)

        path.reset()
        val stepX = w / (maxPoints - 1).toFloat()
        var idx = 0
        for (v in bufferHistory) {
            val x = idx * stepX
            val y = (h - (v.coerceIn(0f, maxScaleMs) / maxScaleMs) * plotH).coerceIn(headerH + 2f, h - 2f)
            if (idx == 0) path.moveTo(x, y) else path.lineTo(x, y)
            idx++
        }
        val lineColor = if (currentBufferMs > currentTargetMs + 20f) Color.parseColor("#FF8800") else Color.parseColor("#00FF00")
        linePaint.color = lineColor
        canvas.drawPath(path, linePaint)

        // Draw top header strip to cleanly contain the telemetry text
        canvas.drawRect(0f, 0f, w, headerH, headerPaint)
        canvas.drawLine(0f, headerH, w, headerH, gridPaint)
        canvas.drawRect(0f, 0f, w, h, borderPaint)

        val label = String.format(java.util.Locale.US, "BUF: %.1f ms | EST: ~%.0f ms | TGT: %.0f ms", currentBufferMs, estTotalMs, currentTargetMs)
        val textY = headerH * 0.70f
        canvas.drawText(label, 12f, textY, textPaint)
    }
}
