package com.hasan.phonespeaker

data class AudioStats(
    val bufferMs: Float = 0f,
    val targetMs: Float = 30f,
    val underruns: Int = 0,
    val lostPackets: Int = 0,
    val latePackets: Int = 0,
    val rxPps: Int = 0,
    val codec: Int = 0,
    val autoMode: Boolean = true
)

object AudioNative {
    init {
        System.loadLibrary("phonespeaker_native")
    }

    external fun nativeStart(port: Int): Boolean
    external fun nativeStop()
    external fun nativeSetTargetBufferMs(targetMs: Float)
    external fun nativeGetTargetBufferMs(): Float
    external fun nativeSetAutoMode(enabled: Boolean)
    external fun nativeIsAutoMode(): Boolean
    external fun nativeSetMuted(muted: Boolean)
    external fun nativeIsMuted(): Boolean
    external fun nativeSetVolumeGain(gain: Float)
    external fun nativeGetVolumeGain(): Float
    external fun nativeSetLowLatencyMode(lowLatency: Boolean)
    external fun nativeGetAudioSessionId(): Int
    external fun nativeGetStats(): FloatArray?
    external fun nativeGetSenderIp(): String?

    fun getStats(): AudioStats {
        val arr = nativeGetStats() ?: return AudioStats()
        if (arr.size < 8) return AudioStats()
        return AudioStats(
            bufferMs = arr[0],
            targetMs = arr[1],
            underruns = arr[2].toInt(),
            lostPackets = arr[3].toInt(),
            latePackets = arr[4].toInt(),
            rxPps = arr[5].toInt(),
            codec = arr[6].toInt(),
            autoMode = arr[7] > 0.5f
        )
    }
}

