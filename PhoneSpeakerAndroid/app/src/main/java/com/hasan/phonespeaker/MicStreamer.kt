package com.hasan.phonespeaker

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.SystemClock
import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.NetworkInterface
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

class MicStreamer {
    private val TAG = "MicStreamer"

    private val sampleRate = 48000
    private val channels = 2
    private val frameMs = 5
    private val samplesPerCh = (sampleRate * frameMs) / 1000 // 240 samples
    private val payloadSize = samplesPerCh * channels * 2    // 960 bytes

    private val isRunning = AtomicBoolean(false)
    private var workerThread: Thread? = null
    private var audioRecord: AudioRecord? = null
    private var noiseSuppressor: NoiseSuppressor? = null
    private var echoCanceler: AcousticEchoCanceler? = null

    val txPps = AtomicInteger(0)
    private val ppsCounter = AtomicInteger(0)
    private var actualInputChannels = 2

    @Volatile
    var dynamicTargetIp: String = "255.255.255.255"

    fun updateTargetIp(ip: String?) {
        if (!ip.isNullOrBlank()) {
            dynamicTargetIp = ip.trim()
        }
    }

    @SuppressLint("MissingPermission")
    fun start(targetIp: String, targetPort: Int = 5006, enableFx: Boolean = true): Boolean {
        if (isRunning.get()) {
            updateTargetIp(targetIp)
            return true
        }

        val audioFormat = AudioFormat.ENCODING_PCM_16BIT
        var record: AudioRecord? = null
        val audioSources = listOf(
            MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.DEFAULT,
            MediaRecorder.AudioSource.VOICE_COMMUNICATION
        )
        for (source in audioSources) {
            for (chCount in listOf(2, 1)) {
                val chConfig = if (chCount == 2) AudioFormat.CHANNEL_IN_STEREO else AudioFormat.CHANNEL_IN_MONO
                val minBuf = AudioRecord.getMinBufferSize(sampleRate, chConfig, audioFormat)
                if (minBuf <= 0) continue
                val frameBytes = samplesPerCh * chCount * 2
                val bufferSize = maxOf(minBuf * 2, frameBytes * 10)
                try {
                    val candidate = AudioRecord(
                        source,
                        sampleRate,
                        chConfig,
                        audioFormat,
                        bufferSize
                    )
                    if (candidate.state == AudioRecord.STATE_INITIALIZED) {
                        record = candidate
                        actualInputChannels = chCount
                        break
                    } else {
                        candidate.release()
                    }
                } catch (_: Exception) {}
            }
            if (record != null) break
        }

        if (record == null) {
            Log.e(TAG, "AudioRecord failed to initialize in both stereo and mono")
            return false
        }

        try {
            audioRecord = record
            val sessionId = audioRecord!!.audioSessionId
            if (enableFx) {
                try {
                    if (NoiseSuppressor.isAvailable()) {
                        noiseSuppressor = NoiseSuppressor.create(sessionId)?.apply { enabled = true }
                    }
                    if (AcousticEchoCanceler.isAvailable()) {
                        echoCanceler = AcousticEchoCanceler.create(sessionId)?.apply { enabled = true }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "Could not attach audio effects: ${e.message}")
                }
            }

            audioRecord!!.startRecording()
            if (audioRecord!!.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                Log.e(TAG, "AudioRecord failed to enter RECORDSTATE_RECORDING")
                stop()
                return false
            }

            isRunning.set(true)
            dynamicTargetIp = targetIp.ifBlank { "255.255.255.255" }

            workerThread = thread(name = "MicStreamerThread", isDaemon = true) {
                runLoop(targetPort)
            }
            Log.i(TAG, "MicStreamer started streaming to $dynamicTargetIp:$targetPort (channels=$actualInputChannels)")
            return true
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start MicStreamer", e)
            stop()
            return false
        }
    }

    private fun isCellularOrDummyInterface(name: String): Boolean {
        val lower = name.lowercase()
        return lower.startsWith("rmnet") ||
               lower.startsWith("ccmni") ||
               lower.startsWith("dummy") ||
               lower.startsWith("lo") ||
               lower.startsWith("p2p")
    }

    private fun getSubnetBroadcasts(): List<InetAddress> {
        val results = mutableListOf<InetAddress>()
        try {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces.hasMoreElements()) {
                val iface = interfaces.nextElement()
                if (iface.isLoopback || !iface.isUp || isCellularOrDummyInterface(iface.name)) continue
                for (ia in iface.interfaceAddresses) {
                    val addr = ia.address
                    val bcast = ia.broadcast
                    if (addr is Inet4Address && bcast != null) {
                        results.add(bcast)
                    }
                }
            }
        } catch (_: Exception) {}
        if (results.isEmpty()) {
            try { results.add(InetAddress.getByName("255.255.255.255")) } catch (_: Exception) {}
        }
        return results
    }

    private fun readFullBuffer(rec: AudioRecord, buf: ByteArray, size: Int): Boolean {
        var offset = 0
        while (offset < size && isRunning.get()) {
            val r = rec.read(buf, offset, size - offset, AudioRecord.READ_BLOCKING)
            if (r <= 0) return false
            offset += r
        }
        return offset == size
    }

    private fun runLoop(targetPort: Int) {
        val socket = DatagramSocket().apply { broadcast = true }
        val headerBuffer = ByteBuffer.allocate(12).order(ByteOrder.LITTLE_ENDIAN)
        val readBuffer = ByteArray(payloadSize)
        val monoBuffer = if (actualInputChannels == 1) ByteArray(samplesPerCh * 2) else null
        val packetData = ByteArray(12 + payloadSize)

        var seq = 0
        var lastPpsTime = SystemClock.elapsedRealtime()
        var cachedIp = ""
        var cachedInet: InetAddress? = null

        while (isRunning.get()) {
            val rec = audioRecord ?: break

            // Resolve PC target IP dynamically
            if (dynamicTargetIp == "255.255.255.255" || dynamicTargetIp.isBlank()) {
                val discovered = AudioStreamService.activeControlChannel?.getActivePcIp()
                    ?: AudioNative.nativeGetSenderIp()
                if (!discovered.isNullOrBlank()) {
                    dynamicTargetIp = discovered
                    Log.i(TAG, "MicStreamer locked onto discovered PC IP: $discovered")
                }
            }

            val ok: Boolean = if (actualInputChannels == 1 && monoBuffer != null) {
                if (readFullBuffer(rec, monoBuffer, monoBuffer.size)) {
                    val sampleCount = samplesPerCh
                    for (i in 0 until sampleCount) {
                        val b0 = monoBuffer[i * 2]
                        val b1 = monoBuffer[i * 2 + 1]
                        readBuffer[i * 4] = b0
                        readBuffer[i * 4 + 1] = b1
                        readBuffer[i * 4 + 2] = b0
                        readBuffer[i * 4 + 3] = b1
                    }
                    true
                } else false
            } else {
                readFullBuffer(rec, readBuffer, payloadSize)
            }

            if (ok) {
                val nowMs = (SystemClock.elapsedRealtime() and 0xFFFFFFFFL).toInt()
                headerBuffer.clear()
                headerBuffer.putInt(seq++)
                headerBuffer.putInt(nowMs)
                headerBuffer.put(0.toByte())         // CODEC_PCM16
                headerBuffer.put(channels.toByte())  // 2 channels
                headerBuffer.putShort(480.toShort()) // 48000 / 100

                System.arraycopy(headerBuffer.array(), 0, packetData, 0, 12)
                System.arraycopy(readBuffer, 0, packetData, 12, payloadSize)

                try {
                    val currentIp = dynamicTargetIp
                    if (currentIp != "255.255.255.255" && currentIp.isNotBlank()) {
                        if (currentIp != cachedIp || cachedInet == null) {
                            cachedInet = InetAddress.getByName(currentIp)
                            cachedIp = currentIp
                        }
                        socket.send(DatagramPacket(packetData, packetData.size, cachedInet, targetPort))
                    } else {
                        for (bcast in getSubnetBroadcasts()) {
                            socket.send(DatagramPacket(packetData, packetData.size, bcast, targetPort))
                        }
                    }
                    ppsCounter.incrementAndGet()
                } catch (_: Exception) {}

                val now = SystemClock.elapsedRealtime()
                if (now - lastPpsTime >= 1000) {
                    txPps.set(ppsCounter.getAndSet(0))
                    lastPpsTime = now
                }
            } else {
                SystemClock.sleep(2)
            }
        }

        try { socket.close() } catch (_: Exception) {}
    }

    fun stop() {
        isRunning.set(false)
        workerThread?.interrupt()
        workerThread = null

        try {
            noiseSuppressor?.release()
            echoCanceler?.release()
        } catch (_: Exception) {}
        noiseSuppressor = null
        echoCanceler = null

        try {
            audioRecord?.stop()
            audioRecord?.release()
        } catch (_: Exception) {}
        audioRecord = null
        txPps.set(0)
        Log.i(TAG, "MicStreamer stopped")
    }
}
