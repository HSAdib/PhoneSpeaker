package com.hasan.phonespeaker

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.KeyEvent
import android.view.View
import android.widget.ArrayAdapter
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.hasan.phonespeaker.databinding.ActivityMainBinding
import org.json.JSONObject
import java.net.Inet4Address
import java.net.NetworkInterface

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var preStreamDiscoveryChannel: ControlChannel? = null
    private val handler = Handler(Looper.getMainLooper())

    private var localRev = 1
    private var lastSyncId = (System.currentTimeMillis() and 0x7FFFFFFF).toInt()
    private var suppressSync = false

    private val bitrateValues = listOf("10", "24", "32", "64", "96", "128", "160", "192", "256", "320")

    private fun computeExpectedDelayMs(): Pair<Int, String> {
        val isOpus = binding.rbCodecOpus.isChecked || binding.rbPlayerCodecOpus.isChecked
        val bitrate = bitrateValues.getOrElse(binding.spinnerBitrate.selectedItemPosition) { "128" }.toIntOrNull() ?: 128
        val frameMs = 5
        val bufMs = quantizeTarget(AudioNative.nativeGetTargetBufferMs()).toInt()
        val captureMs = 10
        val hwMs = 10
        val (codecNetMs, qualityNote) = if (!isOpus) {
            Pair(3, "Lossless 1536 kbps")
        } else {
            when {
                bitrate <= 32 -> Pair(9, "Opus ${bitrate}k (Voice/Low BW)")
                bitrate <= 96 -> Pair(6, "Opus ${bitrate}k (Balanced)")
                bitrate <= 192 -> Pair(5, "Opus ${bitrate}k (High Quality)")
                else -> Pair(6, "Opus ${bitrate}k (Studio Max)")
            }
        }
        val totalMs = captureMs + frameMs + codecNetMs + bufMs + hwMs
        return Pair(totalMs, "Expected Delay: ~$totalMs ms ($qualityNote, ${frameMs}ms frame + ${bufMs}ms buf)")
    }

    private fun updateExpectedDelayUI() {
        val (_, text) = computeExpectedDelayMs()
        binding.tvExpectedDelay.text = text
    }

    private fun showHelpDialog() {
        val guideText = """
        ============================================================
        PHONESPEAKER v2.0 — USER GUIDE & HELP
        ============================================================

        ✓ SPEAKER & MIC STREAMING STATUS: VERIFIED & WORKING
        ------------------------------------------------------------
        Both directions have dedicated, independent controls on both
        your Phone and PC (Windows .exe & Linux binary):
        * [ ▶ START SPEAKER PLAYER (PORT 5004) ] — Receives PC audio
        * [ 🎤 START MIC STREAM TO PC (PORT 5006) ] — Sends Phone Mic
        You can run either one solo or both simultaneously (Full-Duplex)!

        1. USING PHONE AS WIRELESS SPEAKER (PC -> PHONE :5004)
        ------------------------------------------------------------
        * Tap [ ▶ START SPEAKER PLAYER (PORT 5004) ] on Phone and
          click [ ▶ START SPEAKER STREAM ] on PC.
        * PC auto-discovers your phone via UDP beacons (Port 5005).
        * Buffer Presets: Low (20ms), Medium (40ms), High (80ms), Auto.

        2. USING PHONE AS WIRELESS MIC (PHONE -> PC :5006)
        ------------------------------------------------------------
        * Tap [ 🎤 START MIC STREAM TO PC (PORT 5006) ] on either the
          PLAYER tab or SERVER tab (or toggle [ 🎤 MIC RECEIVER ] on PC).
        * Verify it is working:
          - Phone shows 'MIC SERVER: STREAMING TO PC' and 'TX PPS: ~200'.
          - PC shows 'MIC RX: ACTIVE' and 'RX PPS: ~200' + Level Meter!
        * Using in Discord / Zoom / OBS:
          - Windows: Set PC Playback Dev to 'CABLE Input' (VB-CABLE),
            then select 'CABLE Output' as your Mic in Discord/Zoom.
          - Linux: Click 'Create Virtual Mic (PipeWire/PulseAudio)' in
            Tab 3 and select 'PhoneSpeaker Wireless Mic' in your app.

        3. SETTINGS TAB (REAL-TIME 2-WAY SYNC)
        ------------------------------------------------------------
        * Any changes on PC or Phone sync immediately over UDP 5005.
        * Bitrates: 10 to 320 kbps Opus or uncompressed Lossless PCM16.
        * Dual-Speaker Sync: Play on PC and Phone simultaneously.
        """.trimIndent()

        val tv = android.widget.TextView(this).apply {
            text = guideText
            setTextColor(0xFFFFFFFF.toInt())
            setBackgroundColor(0xFF0A0A0A.toInt())
            typeface = android.graphics.Typeface.MONOSPACE
            textSize = 12f
            setPadding(32, 24, 32, 24)
        }
        val scroll = android.widget.ScrollView(this).apply {
            addView(tv)
            setBackgroundColor(0xFF0A0A0A.toInt())
        }

        androidx.appcompat.app.AlertDialog.Builder(this)
            .setTitle("PhoneSpeaker v2.0 Guide")
            .setView(scroll)
            .setPositiveButton("Close", null)
            .show()
    }

    private val statsRunnable = object : Runnable {
        override fun run() {
            updateStatsUI()
            handler.postDelayed(this, 200) // 5 Hz
        }
    }

    private val serviceReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                AudioStreamService.ACTION_STATE_CHANGED -> {
                    refreshServiceButtonState()
                }
                AudioStreamService.ACTION_CONFIG_SYNC_RECEIVED -> {
                    val jsonStr = intent.getStringExtra(AudioStreamService.EXTRA_CONFIG_JSON)
                    if (!jsonStr.isNullOrBlank()) {
                        try {
                            applyIncomingConfigSync(JSONObject(jsonStr))
                        } catch (e: Exception) {
                            Log.e("PhoneSpeaker", "Error parsing incoming CONFIG_SYNC", e)
                        }
                    }
                }
            }
        }
    }

    private fun quantizeTarget(value: Float): Float {
        val safe = if (value.isFinite()) value else 30f
        val clamped = safe.coerceIn(10f, 200f)
        return (kotlin.math.round(clamped / 2.5f) * 2.5f).coerceIn(10f, 200f)
    }

    private fun quantizeGain(value: Float): Float {
        val safe = if (value.isFinite()) value else 1.0f
        return safe.coerceIn(0f, 2.0f)
    }

    @Synchronized
    private fun startPreStreamDiscoveryIfNeeded() {
        if (!AudioStreamService.isStreaming && !AudioStreamService.isMicStreaming && preStreamDiscoveryChannel == null) {
            preStreamDiscoveryChannel = ControlChannel(
                audioPort = 5004,
                controlPort = 5005,
                onConfigSyncReceived = { configJson ->
                    runOnUiThread { applyIncomingConfigSync(configJson) }
                },
                onMicControlReceived = { wantActive ->
                    runOnUiThread {
                        if (wantActive != AudioStreamService.isMicStreaming) {
                            toggleMicStreaming()
                        }
                    }
                }
            ).also {
                it.start()
            }
        }
    }

    @Synchronized
    private fun stopPreStreamDiscovery() {
        preStreamDiscoveryChannel?.stop()
        preStreamDiscoveryChannel = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        volumeControlStream = AudioManager.STREAM_MUSIC
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setupTabs()
        setupUI()
        checkPermissions()
        refreshLocalIps()
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN || keyCode == KeyEvent.KEYCODE_VOLUME_UP) {
            val am = getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            val direction = if (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) AudioManager.ADJUST_LOWER else AudioManager.ADJUST_RAISE
            am?.adjustStreamVolume(AudioManager.STREAM_MUSIC, direction, AudioManager.FLAG_SHOW_UI)
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter().apply {
            addAction(AudioStreamService.ACTION_STATE_CHANGED)
            addAction(AudioStreamService.ACTION_CONFIG_SYNC_RECEIVED)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(serviceReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(serviceReceiver, filter)
        }
        refreshServiceButtonState()
    }

    override fun onResume() {
        super.onResume()
        handler.removeCallbacks(statsRunnable)
        handler.post(statsRunnable)
        startPreStreamDiscoveryIfNeeded()
        refreshBatteryOptimizationStatus()
    }

    override fun onPause() {
        super.onPause()
        handler.removeCallbacks(statsRunnable)
    }

    override fun onStop() {
        super.onStop()
        try { unregisterReceiver(serviceReceiver) } catch (_: Exception) {}
    }

    override fun onDestroy() {
        super.onDestroy()
        stopPreStreamDiscovery()
    }

    // ------------------------------------------------------------------
    // Tab Navigation
    // ------------------------------------------------------------------

    private fun setupTabs() {
        binding.tabBtnPlayer.setOnClickListener { selectTab(0) }
        binding.tabBtnServer.setOnClickListener { selectTab(1) }
        binding.tabBtnSettings.setOnClickListener { selectTab(2) }
        selectTab(0)
    }

    private fun selectTab(index: Int) {
        binding.layoutPlayerTab.visibility = if (index == 0) View.VISIBLE else View.GONE
        binding.layoutServerTab.visibility = if (index == 1) View.VISIBLE else View.GONE
        binding.layoutSettingsTab.visibility = if (index == 2) View.VISIBLE else View.GONE

        val tabs = listOf(binding.tabBtnPlayer, binding.tabBtnServer, binding.tabBtnSettings)
        for ((i, btn) in tabs.withIndex()) {
            val active = (i == index)
            btn.setTextColor(if (active) 0xFF00FF00.toInt() else 0xFF888888.toInt())
            btn.strokeColor = android.content.res.ColorStateList.valueOf(
                if (active) 0xFF00FF00.toInt() else 0xFF444444.toInt()
            )
            btn.strokeWidth = if (active) 4 else 2
            btn.backgroundTintList = android.content.res.ColorStateList.valueOf(
                if (active) 0xFF0F220F.toInt() else 0xFF000000.toInt()
            )
        }
    }

    // ------------------------------------------------------------------
    // UI Wiring
    // ------------------------------------------------------------------

    private fun setupUI() {
        // Bitrate spinner with custom dark adapter & selection listener
        val bitrateAdapter = object : ArrayAdapter<String>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            bitrateValues.map { "$it Kbps" }
        ) {
            override fun getView(position: Int, convertView: View?, parent: android.view.ViewGroup): View {
                val v = super.getView(position, convertView, parent)
                if (v is android.widget.TextView) {
                    v.setTextColor(0xFFFFFFFF.toInt())
                    v.setBackgroundColor(0xFF111111.toInt())
                }
                return v
            }

            override fun getDropDownView(position: Int, convertView: View?, parent: android.view.ViewGroup): View {
                val v = super.getDropDownView(position, convertView, parent)
                if (v is android.widget.TextView) {
                    v.setTextColor(0xFFFFFFFF.toInt())
                    v.setBackgroundColor(0xFF111111.toInt())
                }
                return v
            }
        }
        binding.spinnerBitrate.adapter = bitrateAdapter
        binding.spinnerBitrate.setSelection(5) // 128 Kbps default

        val playerBitrateAdapter = object : ArrayAdapter<String>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            bitrateValues.map { "$it Kbps" }
        ) {
            override fun getView(position: Int, convertView: View?, parent: android.view.ViewGroup): View {
                val v = super.getView(position, convertView, parent)
                if (v is android.widget.TextView) {
                    v.setTextColor(0xFFFFFFFF.toInt())
                    v.setBackgroundColor(0xFF111111.toInt())
                }
                return v
            }
            override fun getDropDownView(position: Int, convertView: View?, parent: android.view.ViewGroup): View {
                val v = super.getDropDownView(position, convertView, parent)
                if (v is android.widget.TextView) {
                    v.setTextColor(0xFFFFFFFF.toInt())
                    v.setBackgroundColor(0xFF111111.toInt())
                }
                return v
            }
        }
        binding.spPlayerBitrate.adapter = playerBitrateAdapter
        binding.spPlayerBitrate.setSelection(5)

        binding.spinnerBitrate.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long) {
                if (suppressSync) return
                if (binding.spPlayerBitrate.selectedItemPosition != position) {
                    suppressSync = true
                    binding.spPlayerBitrate.setSelection(position)
                    suppressSync = false
                }
                onLocalSettingChanged()
            }
            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
        }

        binding.spPlayerBitrate.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long) {
                if (suppressSync) return
                if (binding.spinnerBitrate.selectedItemPosition != position) {
                    suppressSync = true
                    binding.spinnerBitrate.setSelection(position)
                    suppressSync = false
                }
                onLocalSettingChanged()
            }
            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
        }

        // Buffer target slider
        val initialTarget = quantizeTarget(AudioNative.nativeGetTargetBufferMs())
        binding.sliderTargetBuffer.value = initialTarget
        binding.tvTargetBufferValue.text = "${initialTarget.toInt()} ms"

        binding.sliderTargetBuffer.addOnChangeListener { _, value, fromUser ->
            if (fromUser) {
                val qVal = quantizeTarget(value)
                AudioNative.nativeSetTargetBufferMs(qVal)
                binding.tvTargetBufferValue.text = "${qVal.toInt()} ms"
                binding.rbBufCustom.isChecked = true
                onLocalSettingChanged()
            }
        }

        // Buffer presets radio group
        binding.rgBufferMode.setOnCheckedChangeListener { _, checkedId ->
            when (checkedId) {
                R.id.rbBufLow -> {
                    AudioNative.nativeSetTargetBufferMs(20f)
                    AudioNative.nativeSetAutoMode(false)
                    binding.sliderTargetBuffer.value = 20f
                    binding.tvTargetBufferValue.text = "20 ms"
                }
                R.id.rbBufMed -> {
                    AudioNative.nativeSetTargetBufferMs(40f)
                    AudioNative.nativeSetAutoMode(false)
                    binding.sliderTargetBuffer.value = 40f
                    binding.tvTargetBufferValue.text = "40 ms"
                }
                R.id.rbBufHigh -> {
                    AudioNative.nativeSetTargetBufferMs(80f)
                    AudioNative.nativeSetAutoMode(false)
                    binding.sliderTargetBuffer.value = 80f
                    binding.tvTargetBufferValue.text = "80 ms"
                }
                R.id.rbBufAuto -> {
                    AudioNative.nativeSetAutoMode(true)
                }
                R.id.rbBufCustom -> {
                    AudioNative.nativeSetAutoMode(false)
                }
            }
            onLocalSettingChanged()
        }

        // Gain slider & Mute
        try {
            binding.sliderGain.value = quantizeGain(AudioNative.nativeGetVolumeGain())
            binding.tvGainValue.text = "${(binding.sliderGain.value * 100).toInt()}%"
        } catch (_: Exception) {}
        binding.sliderGain.addOnChangeListener { _, value, fromUser ->
            if (fromUser) {
                AudioNative.nativeSetVolumeGain(value)
                binding.tvGainValue.text = "${(value * 100).toInt()}%"
                onLocalSettingChanged()
            }
        }

        binding.btnMuteToggle.setOnClickListener {
            val muted = !AudioNative.nativeIsMuted()
            AudioNative.nativeSetMuted(muted)
            binding.btnMuteToggle.text = if (muted) "MUTED" else "MUTE"
            onLocalSettingChanged()
        }

        // Codec selection synced across Player tab and Settings tab
        binding.rgPlayerCodec.setOnCheckedChangeListener { _, checkedId ->
            if (suppressSync) return@setOnCheckedChangeListener
            suppressSync = true
            if (checkedId == R.id.rbPlayerCodecOpus) {
                binding.rbCodecOpus.isChecked = true
            } else {
                binding.rbCodecPcm.isChecked = true
            }
            suppressSync = false
            onLocalSettingChanged()
        }

        binding.rgCodec.setOnCheckedChangeListener { _, checkedId ->
            if (suppressSync) return@setOnCheckedChangeListener
            suppressSync = true
            if (checkedId == R.id.rbCodecOpus) {
                binding.rbPlayerCodecOpus.isChecked = true
            } else {
                binding.rbPlayerCodecPcm.isChecked = true
            }
            suppressSync = false
            onLocalSettingChanged()
        }

        // Speaker routing & Codec selection
        binding.rgOutputTargets.setOnCheckedChangeListener { _, _ -> onLocalSettingChanged() }

        // Role & Advanced settings listeners
        binding.rgServerRole.setOnCheckedChangeListener { _, _ -> onLocalSettingChanged() }
        binding.rgOutputEngine.setOnCheckedChangeListener { _, _ -> onLocalSettingChanged() }
        binding.switchAudioFx.setOnCheckedChangeListener { _, _ -> onLocalSettingChanged() }
        binding.rgExclusiveMode.setOnCheckedChangeListener { _, _ -> onLocalSettingChanged() }
        binding.switchHeadsetDisconnect.setOnCheckedChangeListener { _, _ -> onLocalSettingChanged() }

        // Help button
        binding.btnHelp.setOnClickListener { showHelpDialog() }
        updateExpectedDelayUI()

        // Start / Stop Player Button
        binding.btnToggleStream.setOnClickListener {
            if (AudioStreamService.isStreaming) {
                val intent = Intent(this, AudioStreamService::class.java).apply {
                    action = AudioStreamService.ACTION_STOP
                }
                startService(intent)
            } else {
                stopPreStreamDiscovery()
                val intent = Intent(this, AudioStreamService::class.java).apply {
                    action = AudioStreamService.ACTION_START
                    putExtra(AudioStreamService.EXTRA_AUDIO_ENGINE, if (binding.rbEngineAudioTrack.isChecked) "AUDIOTRACK_STANDARD" else "OBOE_LOW_LATENCY")
                    putExtra(AudioStreamService.EXTRA_AUDIO_FX, binding.switchAudioFx.isChecked)
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    startForegroundService(intent)
                } else {
                    startService(intent)
                }

                if (binding.rbFullDuplex.isChecked && !AudioStreamService.isMicStreaming) {
                    if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                        val targetIp = binding.etPcIp.text.toString().trim()
                        val micIntent = Intent(this, AudioStreamService::class.java).apply {
                            action = AudioStreamService.ACTION_START_MIC
                            if (targetIp.isNotEmpty()) putExtra(AudioStreamService.EXTRA_TARGET_IP, targetIp)
                            putExtra(AudioStreamService.EXTRA_MIC_FX, binding.switchMicFx.isChecked)
                        }
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(micIntent)
                        } else {
                            startService(micIntent)
                        }
                    }
                }
            }
            handler.postDelayed({ refreshServiceButtonState() }, 200L)
        }

        // Mic Server Start / Stop Buttons (Main Player Tab & Server Tab)
        binding.btnToggleMic.setOnClickListener { toggleMicStreaming() }
        binding.btnToggleMicMain.setOnClickListener { toggleMicStreaming() }

        binding.btnBatteryOpt.setOnClickListener { requestBatteryOptimizationExemption() }
        binding.btnRefreshIps.setOnClickListener { refreshLocalIps() }
    }

    // ------------------------------------------------------------------
    // Settings Synchronization (Two-Way)
    // ------------------------------------------------------------------

    private fun onLocalSettingChanged() {
        if (suppressSync) return
        localRev++
        val syncId = (System.currentTimeMillis() and 0x7FFFFFFF).toInt()
        lastSyncId = syncId
        binding.tvSyncRev.text = "REV: $localRev"
        updateExpectedDelayUI()

        val json = JSONObject().apply {
            put("type", "CONFIG_SYNC")
            put("rev", localRev)
            put("sync_id", syncId)
            put("direction", if (binding.rbFullDuplex.isChecked) "FULL_DUPLEX" else "PC_TO_PHONE")
            put("pc_source", "SYSTEM_AUDIO")
            put("phone_source", "MIC")
            put("output_targets", when (binding.rgOutputTargets.checkedRadioButtonId) {
                R.id.rbTargetBoth -> "BOTH_SYNCED"
                R.id.rbTargetPc -> "PC_ONLY"
                else -> "PHONE_ONLY"
            })
            put("codec", if (binding.rbCodecOpus.isChecked || binding.rbPlayerCodecOpus.isChecked) 1 else 0)
            put("opus_bitrate_kbps", bitrateValues.getOrElse(binding.spinnerBitrate.selectedItemPosition) { "128" }.toInt())
            put("buffer_mode", when (binding.rgBufferMode.checkedRadioButtonId) {
                R.id.rbBufLow -> "LOW"
                R.id.rbBufMed -> "MEDIUM"
                R.id.rbBufHigh -> "HIGH"
                R.id.rbBufAuto -> "AUTO"
                else -> "CUSTOM"
            })
            put("custom_buffer_ms", binding.sliderTargetBuffer.value.toDouble())
            put("auto_buffer", binding.rbBufAuto.isChecked)
            put("volume_gain", binding.sliderGain.value.toDouble())
            put("muted", AudioNative.nativeIsMuted())
            put("device", ControlChannel.getPhoneModelName())
            put("audio_output_engine", if (binding.rbEngineAudioTrack.isChecked) "AUDIOTRACK_STANDARD" else "OBOE_LOW_LATENCY")
            put("enable_audio_effects", binding.switchAudioFx.isChecked)
            put("exclusive_audio_mode", when (binding.rgExclusiveMode.checkedRadioButtonId) {
                R.id.rbFocusExclusive -> "EXCLUSIVE"
                R.id.rbFocusAlways -> "ALWAYS_PLAY"
                else -> "MIX_WITH_OTHERS"
            })
            put("stop_on_headset_disconnect", binding.switchHeadsetDisconnect.isChecked)
        }

        val lowLatency = binding.rbEngineOboe.isChecked && !binding.switchAudioFx.isChecked
        AudioNative.nativeSetLowLatencyMode(lowLatency)

        val destIp = binding.etPcIp.text.toString().trim().ifEmpty { null }
        (AudioStreamService.activeControlChannel ?: preStreamDiscoveryChannel)?.sendConfigSync(json, destIp)
    }

    private fun applyIncomingConfigSync(json: JSONObject) {
        val incomingRev = json.optInt("rev", 0)
        val incomingSyncId = if (json.has("sync_id")) json.getInt("sync_id") else null

        if (incomingSyncId != null && incomingSyncId == lastSyncId) {
            return
        }

        if (incomingSyncId != null) {
            lastSyncId = incomingSyncId
        } else if (incomingRev <= localRev) {
            return
        }

        suppressSync = true
        try {
            localRev = maxOf(localRev, incomingRev) + 1
            binding.tvSyncRev.text = "REV: $localRev"

            if (json.has("custom_buffer_ms")) {
                val buf = json.getDouble("custom_buffer_ms").toFloat()
                binding.sliderTargetBuffer.value = quantizeTarget(buf)
                binding.tvTargetBufferValue.text = "${buf.toInt()} ms"
                AudioNative.nativeSetTargetBufferMs(buf)
            }
            if (json.has("buffer_mode")) {
                when (json.getString("buffer_mode")) {
                    "LOW" -> binding.rbBufLow.isChecked = true
                    "MEDIUM" -> binding.rbBufMed.isChecked = true
                    "HIGH" -> binding.rbBufHigh.isChecked = true
                    "AUTO" -> binding.rbBufAuto.isChecked = true
                    else -> binding.rbBufCustom.isChecked = true
                }
            }
            if (json.has("auto_buffer")) {
                AudioNative.nativeSetAutoMode(json.getBoolean("auto_buffer"))
            }
            if (json.has("volume_gain")) {
                val g = json.getDouble("volume_gain").toFloat()
                try {
                    binding.sliderGain.value = quantizeGain(g)
                    binding.tvGainValue.text = "${(g * 100).toInt()}%"
                } catch (e: Exception) {
                    Log.w("PhoneSpeaker", "Could not set sliderGain value: ${e.message}")
                }
                AudioNative.nativeSetVolumeGain(g)
            }
            if (json.has("muted")) {
                val m = json.getBoolean("muted")
                AudioNative.nativeSetMuted(m)
                binding.btnMuteToggle.text = if (m) "MUTED" else "MUTE"
            }
            if (json.has("output_targets")) {
                when (json.getString("output_targets")) {
                    "BOTH_SYNCED" -> binding.rbTargetBoth.isChecked = true
                    "PC_ONLY" -> binding.rbTargetPc.isChecked = true
                    else -> binding.rbTargetPhone.isChecked = true
                }
            }
            if (json.has("codec")) {
                val isOpus = (json.getInt("codec") == 1)
                binding.rbCodecOpus.isChecked = isOpus
                binding.rbCodecPcm.isChecked = !isOpus
                binding.rbPlayerCodecOpus.isChecked = isOpus
                binding.rbPlayerCodecPcm.isChecked = !isOpus
            }
            if (json.has("opus_bitrate_kbps")) {
                val kbps = json.getInt("opus_bitrate_kbps").toString()
                val idx = bitrateValues.indexOf(kbps)
                if (idx >= 0) {
                    binding.spinnerBitrate.setSelection(idx)
                    binding.spPlayerBitrate.setSelection(idx)
                }
            }
            updateExpectedDelayUI()

            if (json.has("direction")) {
                if (json.getString("direction") == "FULL_DUPLEX") {
                    binding.rbFullDuplex.isChecked = true
                } else {
                    binding.rbMicOnly.isChecked = true
                }
            }
            if (json.has("audio_output_engine")) {
                if (json.getString("audio_output_engine") == "AUDIOTRACK_STANDARD") {
                    binding.rbEngineAudioTrack.isChecked = true
                } else {
                    binding.rbEngineOboe.isChecked = true
                }
            }
            if (json.has("enable_audio_effects")) {
                binding.switchAudioFx.isChecked = json.getBoolean("enable_audio_effects")
            }
            if (json.has("exclusive_audio_mode")) {
                when (json.getString("exclusive_audio_mode")) {
                    "EXCLUSIVE" -> binding.rbFocusExclusive.isChecked = true
                    "ALWAYS_PLAY" -> binding.rbFocusAlways.isChecked = true
                    else -> binding.rbFocusMix.isChecked = true
                }
            }
            if (json.has("stop_on_headset_disconnect")) {
                binding.switchHeadsetDisconnect.isChecked = json.getBoolean("stop_on_headset_disconnect")
            }

            val lowLatency = binding.rbEngineOboe.isChecked && !binding.switchAudioFx.isChecked
            AudioNative.nativeSetLowLatencyMode(lowLatency)
        } finally {
            suppressSync = false
        }
    }

    // ------------------------------------------------------------------
    // Telemetry & UI Refresh
    // ------------------------------------------------------------------

    private fun toggleMicStreaming() {
        if (AudioStreamService.isMicStreaming) {
            val intent = Intent(this, AudioStreamService::class.java).apply {
                action = AudioStreamService.ACTION_STOP_MIC
            }
            startService(intent)
        } else {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.RECORD_AUDIO), 201)
                return
            }
            val discoveredIp = binding.etPcIp.text.toString().trim().ifEmpty {
                preStreamDiscoveryChannel?.getActivePcIp()
                    ?: AudioStreamService.activeControlChannel?.getActivePcIp()
                    ?: AudioNative.nativeGetSenderIp()
                    ?: ""
            }
            if (discoveredIp.isNotEmpty() && binding.etPcIp.text.isNullOrBlank()) {
                binding.etPcIp.setText(discoveredIp)
            }
            // Release port 5005 from pre-stream discovery before service binds it
            stopPreStreamDiscovery()

            val intent = Intent(this, AudioStreamService::class.java).apply {
                action = AudioStreamService.ACTION_START_MIC
                if (discoveredIp.isNotEmpty()) putExtra(AudioStreamService.EXTRA_TARGET_IP, discoveredIp)
                putExtra(AudioStreamService.EXTRA_MIC_FX, binding.switchMicFx.isChecked)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent)
            } else {
                startService(intent)
            }
        }
        handler.postDelayed({ refreshServiceButtonState() }, 200L)
    }

    private fun refreshServiceButtonState() {
        if (AudioStreamService.isStreaming) {
            binding.btnToggleStream.text = "[ ■ STOP SPEAKER PLAYER (PORT 5004) ]"
            binding.btnToggleStream.setTextColor(0xFFFF4444.toInt())
        } else {
            binding.btnToggleStream.text = "[ ▶ START SPEAKER PLAYER (PORT 5004) ]"
            binding.btnToggleStream.setTextColor(0xFF00FF00.toInt())
        }

        if (AudioStreamService.isMicStreaming) {
            binding.btnToggleMic.text = "[ ■ STOP MIC STREAM (PORT 5006) ]"
            binding.btnToggleMicMain.text = "[ ■ STOP MIC STREAM (PORT 5006) ]"
            binding.btnToggleMicMain.setTextColor(0xFFFF4444.toInt())
            binding.tvMicStatus.text = "MIC SERVER: STREAMING TO PC (PORT 5006)"
            binding.tvMicStatus.setTextColor(0xFF00FF00.toInt())
        } else {
            binding.btnToggleMic.text = "[ 🎤 START MIC SERVER (PORT 5006) ]"
            binding.btnToggleMicMain.text = "[ 🎤 START MIC STREAM TO PC (PORT 5006) ]"
            binding.btnToggleMicMain.setTextColor(0xFF00EEEE.toInt())
            binding.tvMicStatus.text = "MIC SERVER: STOPPED"
            binding.tvMicStatus.setTextColor(0xFF888888.toInt())
        }

        val statusParts = mutableListOf<String>()
        if (AudioStreamService.isStreaming) statusParts.add("SPEAKER: ON (5004)")
        if (AudioStreamService.isMicStreaming) statusParts.add("MIC: ON (5006)")
        if (statusParts.isEmpty()) {
            binding.tvStatus.text = "STATUS: STOPPED"
            binding.tvStatus.setTextColor(0xFF888888.toInt())
            startPreStreamDiscoveryIfNeeded()
        } else {
            binding.tvStatus.text = "ACTIVE: " + statusParts.joinToString(" | ")
            binding.tvStatus.setTextColor(0xFF00FF00.toInt())
        }
    }

    private fun updateStatsUI() {
        refreshServiceButtonState()
        try {
            val stats = AudioNative.getStats()
            val safeBuffer = if (stats.bufferMs.isFinite()) stats.bufferMs else 0f
            val target = quantizeTarget(stats.targetMs)
            val (estTotal, _) = computeExpectedDelayMs()
            binding.latencyGraph.pushSample(safeBuffer, target, estTotal.toFloat())

            binding.tvBufferLevel.text = String.format("%.1f ms", safeBuffer)
            binding.tvActiveTarget.text = "${target.toInt()} ms"
            binding.tvPps.text = "${stats.rxPps} pps"
            binding.tvUnderruns.text = "${stats.underruns}"
            binding.tvLostPackets.text = "${stats.lostPackets}"
            binding.tvLatePackets.text = "${stats.latePackets}"
            binding.tvCodec.text = if (stats.codec == 1) "Opus" else "PCM16"

            // Auto-update connected PC IP if empty
            val pcIp = AudioStreamService.activeControlChannel?.getActivePcIp()
                ?: preStreamDiscoveryChannel?.getActivePcIp()
                ?: AudioNative.nativeGetSenderIp()
            val pcName = AudioStreamService.activeControlChannel?.getActivePcName()
                ?: preStreamDiscoveryChannel?.getActivePcName()
                ?: "PC"

            if (!pcIp.isNullOrBlank() && binding.etPcIp.text.isNullOrBlank()) {
                binding.etPcIp.setText(pcIp)
            }

            binding.tvConnectedDeviceTitle.text = if (!pcIp.isNullOrBlank()) "💻 Connected to: $pcName ($pcIp)" else "💻 Waiting for PC connection..."
            binding.tvConnectedDeviceSub.text = "📱 This Phone: ${ControlChannel.getPhoneModelName()}"

            // Mic TX PPS
            val micPps = AudioStreamService.micStreamerInstance?.txPps?.get() ?: 0
            binding.tvMicPps.text = "TX PPS: $micPps pps"

            // Keep mute button text synced with native engine mute state
            binding.btnMuteToggle.text = if (AudioNative.nativeIsMuted()) "MUTED" else "MUTE"

        } catch (e: Exception) {
            Log.e("PhoneSpeaker", "updateStatsUI error", e)
        }
    }

    private fun refreshBatteryOptimizationStatus() {
        try {
            val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
            val isIgnoring = powerManager.isIgnoringBatteryOptimizations(packageName)
            if (isIgnoring) {
                binding.btnBatteryOpt.text = "BATTERY OPTIMIZATION: EXEMPT (OK)"
                binding.btnBatteryOpt.isEnabled = false
            } else {
                binding.btnBatteryOpt.text = "REQUEST BATTERY EXEMPTION (RECOMMENDED)"
                binding.btnBatteryOpt.isEnabled = true
            }
        } catch (_: Exception) {}
    }

    private fun requestBatteryOptimizationExemption() {
        try {
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:$packageName")
            }
            startActivity(intent)
        } catch (_: Exception) {}
    }

    private fun refreshLocalIps() {
        val sb = StringBuilder()
        try {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces.hasMoreElements()) {
                val iface = interfaces.nextElement()
                if (iface.isLoopback || !iface.isUp) continue
                val name = iface.name
                val lower = name.lowercase()
                if (lower.startsWith("rmnet") || lower.startsWith("ccmni") || lower.startsWith("dummy")) {
                    continue
                }
                val addresses = iface.inetAddresses
                while (addresses.hasMoreElements()) {
                    val addr = addresses.nextElement()
                    if (!addr.isLoopbackAddress && addr is Inet4Address) {
                        sb.append(String.format("%-10s : %s\n", name, addr.hostAddress))
                    }
                }
            }
        } catch (e: Exception) {
            sb.append("Error reading network interfaces: ${e.message}")
        }
        if (sb.isEmpty()) sb.append("No active Wi-Fi / Hotspot / Tether IPv4 found")
        binding.tvIpList.text = sb.toString().trimEnd()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 201 && grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            toggleMicStreaming()
        }
    }

    private fun checkPermissions() {
        val permissions = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                permissions.add(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
        if (permissions.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, permissions.toTypedArray(), 100)
        }
    }
}
