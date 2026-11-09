package com.hasan.phonespeaker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import org.json.JSONObject

class AudioStreamService : Service() {

    companion object {
        const val TAG = "AudioStreamService"
        const val ACTION_START = "com.hasan.phonespeaker.START"
        const val ACTION_STOP = "com.hasan.phonespeaker.STOP"
        const val ACTION_START_MIC = "com.hasan.phonespeaker.START_MIC"
        const val ACTION_STOP_MIC = "com.hasan.phonespeaker.STOP_MIC"
        const val ACTION_STATE_CHANGED = "com.hasan.phonespeaker.STATE_CHANGED"
        const val ACTION_CONFIG_SYNC_RECEIVED = "com.hasan.phonespeaker.CONFIG_SYNC"

        const val EXTRA_STREAMING = "streaming"
        const val EXTRA_MIC_STREAMING = "mic_streaming"
        const val EXTRA_CONFIG_JSON = "config_json"
        const val EXTRA_TARGET_IP = "target_ip"
        const val EXTRA_MIC_FX = "extra_mic_fx"
        const val EXTRA_AUDIO_ENGINE = "extra_audio_engine"
        const val EXTRA_AUDIO_FX = "extra_audio_fx"

        const val CHANNEL_ID = "PhoneSpeakerServiceChannel"
        const val NOTIFICATION_ID = 1001

        var isStreaming = false
            private set

        var isMicStreaming = false
            private set

        var micStreamerInstance: MicStreamer? = null
            private set

        var activeControlChannel: ControlChannel? = null
            private set
    }

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var audioManager: AudioManager? = null
    private var audioFocusRequest: AudioFocusRequest? = null

    private var micStreamer: MicStreamer? = null
    private var exclusiveAudioMode = "MIX_WITH_OTHERS"
    private var stopOnHeadsetDisconnect = false
    private var audioOutputEngine = "OBOE_LOW_LATENCY"
    private var enableAudioEffects = false
    private var userMuted = false
    private var outputTargets = "PHONE_ONLY"
    private var pausedByHeadset = false
    private var pausedByFocus = false
    private var savedVolumeGain = 1.0f
    private var loudnessEnhancer: android.media.audiofx.LoudnessEnhancer? = null
    private var lastAttachedSessionId = 0
    private val serviceHandler = android.os.Handler(android.os.Looper.getMainLooper())

    private fun applyEffectiveMuteAndGain() {
        val effectiveMute = userMuted || (outputTargets == "PC_ONLY") || pausedByHeadset || pausedByFocus
        AudioNative.nativeSetMuted(effectiveMute)
    }

    private val noisyReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                AudioManager.ACTION_AUDIO_BECOMING_NOISY -> {
                    if (stopOnHeadsetDisconnect && isStreaming) {
                        Log.i(TAG, "Headset disconnected — pausing stream")
                        pausedByHeadset = true
                        applyEffectiveMuteAndGain()
                        notifyStateChanged()
                    }
                }
                Intent.ACTION_HEADSET_PLUG -> {
                    val state = intent.getIntExtra("state", -1)
                    if (state == 0 && stopOnHeadsetDisconnect && isStreaming) {
                        pausedByHeadset = true
                        applyEffectiveMuteAndGain()
                        notifyStateChanged()
                    } else if (state == 1 && pausedByHeadset) {
                        Log.i(TAG, "Headset reconnected — resuming stream")
                        pausedByHeadset = false
                        applyEffectiveMuteAndGain()
                        notifyStateChanged()
                    }
                }
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        Log.d(TAG, "AudioStreamService.onCreate")
        createNotificationChannel()
        audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val noisyFilter = IntentFilter().apply {
            addAction(AudioManager.ACTION_AUDIO_BECOMING_NOISY)
            addAction(Intent.ACTION_HEADSET_PLUG)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(noisyReceiver, noisyFilter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(noisyReceiver, noisyFilter)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        try { unregisterReceiver(noisyReceiver) } catch (_: Exception) {}
        stopAll()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action
        Log.d(TAG, "AudioStreamService.onStartCommand action=$action")
        if (action == null) {
            if (!isStreaming && !isMicStreaming) {
                stopSelf()
            }
            return START_NOT_STICKY
        }

        val targetIp = intent.getStringExtra(EXTRA_TARGET_IP)

        when (action) {
            ACTION_STOP -> {
                stopAudioPlayer()
                if (!isMicStreaming) stopSelf()
                return START_NOT_STICKY
            }
            ACTION_START -> {
                intent.getStringExtra(EXTRA_AUDIO_ENGINE)?.let { audioOutputEngine = it }
                if (intent.hasExtra(EXTRA_AUDIO_FX)) {
                    enableAudioEffects = intent.getBooleanExtra(EXTRA_AUDIO_FX, false)
                }
                if (!isStreaming) startAudioPlayer()
            }
            ACTION_START_MIC -> {
                val enableFx = intent.getBooleanExtra(EXTRA_MIC_FX, true)
                if (isMicStreaming) {
                    micStreamer?.stop()
                    micStreamer = null
                    micStreamerInstance = null
                }
                startMicServer(targetIp, enableFx)
            }
            ACTION_STOP_MIC -> {
                stopMicServer()
                if (!isStreaming) stopSelf()
                return START_NOT_STICKY
            }
        }

        return START_STICKY
    }

    private fun notifyStateChanged() {
        sendBroadcast(
            Intent(ACTION_STATE_CHANGED)
                .setPackage(packageName)
                .putExtra(EXTRA_STREAMING, isStreaming)
                .putExtra(EXTRA_MIC_STREAMING, isMicStreaming)
        )
    }

    private fun updateForegroundState() {
        val notif = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            var serviceType = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            if (isMicStreaming && Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                serviceType = serviceType or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            }
            startForeground(NOTIFICATION_ID, notif, serviceType)
        } else {
            startForeground(NOTIFICATION_ID, notif)
        }
    }

    private fun startAudioPlayer() {
        Log.d(TAG, "startAudioPlayer entry")
        pausedByHeadset = false
        pausedByFocus = false
        applyEffectiveMuteAndGain()
        isStreaming = true
        updateForegroundState()

        requestAudioFocus()
        acquireLocks()

        val lowLatency = (audioOutputEngine == "OBOE_LOW_LATENCY" && !enableAudioEffects)
        AudioNative.nativeSetLowLatencyMode(lowLatency)

        val started = AudioNative.nativeStart(5004)
        if (!started) {
            Log.e(TAG, "Failed to start native audio engine on port 5004")
            isStreaming = false
            releaseAudioFocus()
            releaseLocks()
            stopForeground(STOP_FOREGROUND_REMOVE)
            notifyStateChanged()
            return
        }

        updateAudioEffects()
        serviceHandler.postDelayed({ updateAudioEffects() }, 280L)

        ensureControlChannel()
        notifyStateChanged()
    }

    private fun stopAudioPlayer() {
        isStreaming = false
        try {
            loudnessEnhancer?.release()
        } catch (_: Exception) {}
        loudnessEnhancer = null
        lastAttachedSessionId = 0

        AudioNative.nativeStop()
        releaseAudioFocus()
        if (!isMicStreaming) {
            releaseLocks()
            activeControlChannel?.stop()
            activeControlChannel = null
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            updateForegroundState()
        }
        notifyStateChanged()
    }

    private fun startMicServer(targetIp: String?, enableFx: Boolean = true) {
        ensureControlChannel()
        if (!targetIp.isNullOrBlank()) {
            activeControlChannel?.setActivePcIp(targetIp)
        }
        val ip = targetIp?.takeIf { it.isNotBlank() }
            ?: activeControlChannel?.getActivePcIp()
            ?: AudioNative.nativeGetSenderIp()
            ?: "255.255.255.255"

        // CRITICAL for Android 11-14: promote to foreground with MICROPHONE type BEFORE starting AudioRecord
        isMicStreaming = true
        updateForegroundState()
        acquireLocks()

        if (micStreamer == null) {
            micStreamer = MicStreamer()
            micStreamerInstance = micStreamer
        }
        val ok = micStreamer!!.start(ip, 5006, enableFx = enableFx)
        if (!ok) {
            Log.e(TAG, "MicStreamer failed to start")
            isMicStreaming = false
            micStreamer = null
            micStreamerInstance = null
            if (!isStreaming) {
                releaseLocks()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            } else {
                updateForegroundState()
            }
        }
        notifyStateChanged()
    }

    private fun stopMicServer() {
        isMicStreaming = false
        micStreamer?.stop()
        micStreamer = null
        micStreamerInstance = null

        if (!isStreaming) {
            releaseLocks()
            activeControlChannel?.stop()
            activeControlChannel = null
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            updateForegroundState()
        }
        notifyStateChanged()
    }

    private fun stopAll() {
        stopAudioPlayer()
        stopMicServer()
        activeControlChannel?.stop()
        activeControlChannel = null
    }

    private fun ensureControlChannel() {
        if (activeControlChannel == null) {
            activeControlChannel = ControlChannel(
                audioPort = 5004,
                controlPort = 5005,
                onConfigSyncReceived = { configJson -> handleConfigSync(configJson) },
                onMicControlReceived = { wantActive ->
                    if (wantActive && !isMicStreaming) {
                        val hasPerm = androidx.core.content.ContextCompat.checkSelfPermission(
                            this, android.Manifest.permission.RECORD_AUDIO
                        ) == android.content.pm.PackageManager.PERMISSION_GRANTED
                        if (hasPerm) {
                            startMicServer(activeControlChannel?.getActivePcIp(), true)
                        }
                    } else if (!wantActive && isMicStreaming) {
                        stopMicServer()
                    }
                }
            )
            activeControlChannel?.start()
        }
    }

    private fun handleConfigSync(json: JSONObject) {
        try {
            sendBroadcast(
                Intent(ACTION_CONFIG_SYNC_RECEIVED)
                    .setPackage(packageName)
                    .putExtra(EXTRA_CONFIG_JSON, json.toString())
            )

            // Apply direct engine settings immediately
            if (json.has("custom_buffer_ms")) {
                AudioNative.nativeSetTargetBufferMs(json.getDouble("custom_buffer_ms").toFloat())
            }
            if (json.has("auto_buffer")) {
                AudioNative.nativeSetAutoMode(json.getBoolean("auto_buffer"))
            }
            if (json.has("volume_gain")) {
                savedVolumeGain = json.getDouble("volume_gain").toFloat().coerceIn(0f, 2f)
                // Stream is already scaled by PC UDPSender relative to phone hardware volume
                AudioNative.nativeSetVolumeGain(1.0f)
            }
            if (json.has("muted")) {
                userMuted = json.getBoolean("muted")
                if (!userMuted) {
                    pausedByHeadset = false
                    pausedByFocus = false
                }
            }
            if (json.has("output_targets")) {
                outputTargets = json.optString("output_targets", "PHONE_ONLY")
            }
            applyEffectiveMuteAndGain()
            if (json.has("exclusive_audio_mode")) {
                exclusiveAudioMode = json.getString("exclusive_audio_mode")
            }
            if (json.has("stop_on_headset_disconnect")) {
                stopOnHeadsetDisconnect = json.getBoolean("stop_on_headset_disconnect")
            }
            if (json.has("audio_output_engine")) {
                audioOutputEngine = json.getString("audio_output_engine")
            }
            if (json.has("enable_audio_effects")) {
                enableAudioEffects = json.getBoolean("enable_audio_effects")
            }
            val lowLatency = (audioOutputEngine == "OBOE_LOW_LATENCY" && !enableAudioEffects)
            AudioNative.nativeSetLowLatencyMode(lowLatency)
            updateAudioEffects()
            serviceHandler.postDelayed({ updateAudioEffects() }, 280L)
        } catch (e: Exception) {
            Log.e(TAG, "Error applying CONFIG_SYNC in service", e)
        }
    }

    private fun updateAudioEffects() {
        if (enableAudioEffects && isStreaming) {
            val sessionId = AudioNative.nativeGetAudioSessionId()
            if (sessionId > 0 && (loudnessEnhancer == null || sessionId != lastAttachedSessionId)) {
                try { loudnessEnhancer?.release() } catch (_: Exception) {}
                loudnessEnhancer = null
                try {
                    loudnessEnhancer = android.media.audiofx.LoudnessEnhancer(sessionId).apply {
                        setTargetGain(600)
                        enabled = true
                    }
                    lastAttachedSessionId = sessionId
                    Log.i(TAG, "Attached LoudnessEnhancer to session $sessionId")
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to attach LoudnessEnhancer: ${e.message}")
                }
            }
        } else {
            try {
                loudnessEnhancer?.release()
            } catch (_: Exception) {}
            loudnessEnhancer = null
            lastAttachedSessionId = 0
        }
    }

    private fun acquireLocks() {
        if (wakeLock == null) {
            val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PhoneSpeaker::WakeLock").apply {
                setReferenceCounted(false)
                acquire(24 * 60 * 60 * 1000L)
            }
        }

        if (wifiLock == null) {
            val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            wifiLock = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                wifiManager.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "PhoneSpeaker::WifiLock")
            } else {
                @Suppress("DEPRECATION")
                wifiManager.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "PhoneSpeaker::WifiLock")
            }.apply {
                setReferenceCounted(false)
                acquire()
            }
        }
    }

    private fun releaseLocks() {
        try { if (wakeLock?.isHeld == true) wakeLock?.release() } catch (_: Exception) {}
        wakeLock = null
        try { if (wifiLock?.isHeld == true) wifiLock?.release() } catch (_: Exception) {}
        wifiLock = null
    }

    private fun requestAudioFocus() {
        if (exclusiveAudioMode == "ALWAYS_PLAY") return

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val playbackAttributes = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build()

            audioFocusRequest = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(playbackAttributes)
                .setOnAudioFocusChangeListener { focusChange ->
                    try {
                        when (focusChange) {
                            AudioManager.AUDIOFOCUS_LOSS_TRANSIENT -> {
                                if (exclusiveAudioMode == "EXCLUSIVE") {
                                    pausedByFocus = true
                                    applyEffectiveMuteAndGain()
                                }
                            }
                            AudioManager.AUDIOFOCUS_LOSS -> {
                                if (exclusiveAudioMode == "EXCLUSIVE") {
                                    pausedByFocus = true
                                    applyEffectiveMuteAndGain()
                                }
                            }
                            AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK -> {
                                if (exclusiveAudioMode == "EXCLUSIVE") {
                                    AudioNative.nativeSetVolumeGain(0.25f)
                                }
                            }
                            AudioManager.AUDIOFOCUS_GAIN -> {
                                pausedByFocus = false
                                AudioNative.nativeSetVolumeGain(1.0f)
                                applyEffectiveMuteAndGain()
                            }
                        }
                    } catch (e: Exception) {
                        Log.e(TAG, "Error in onAudioFocusChangeListener: ${e.message}")
                    }
                }
                .build()

            audioManager?.requestAudioFocus(audioFocusRequest!!)
        }
    }

    private fun releaseAudioFocus() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && audioFocusRequest != null) {
            audioManager?.abandonAudioFocusRequest(audioFocusRequest!!)
            audioFocusRequest = null
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "PhoneSpeaker Studio Service",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Bidirectional audio & settings sync service"
            }
            getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val modeText = when {
            isStreaming && isMicStreaming -> "FULL-DUPLEX (Speaker 5004 + Mic 5006)"
            isStreaming -> "PLAYER (Receiving on Port 5004)"
            isMicStreaming -> "MIC SERVER (Streaming on Port 5006)"
            else -> "IDLE"
        }

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("PhoneSpeaker v2.0 Studio")
            .setContentText(modeText)
            .setSmallIcon(android.R.drawable.ic_lock_silent_mode_off)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setSilent(true)
            .build()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
