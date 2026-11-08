package com.hasan.phonespeaker

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import kotlin.concurrent.thread

class ControlChannel(
    private val audioPort: Int = 5004,
    private val controlPort: Int = 5005,
    private val onConfigSyncReceived: ((JSONObject) -> Unit)? = null,
    private val onMicControlReceived: ((Boolean) -> Unit)? = null
) {
    private val TAG = "ControlChannel"
    @Volatile
    private var isRunning = false
    private var beaconThread: Thread? = null
    private var statsThread: Thread? = null
    private var listenerThread: Thread? = null

    @Volatile
    private var listenerSocket: DatagramSocket? = null
    @Volatile
    private var beaconSocket: DatagramSocket? = null
    @Volatile
    private var statsSocket: DatagramSocket? = null

    @Volatile
    private var activePcIp: String? = null
    @Volatile
    private var activePcName: String = "PC"

    fun getActivePcIp(): String? = activePcIp
    fun getActivePcName(): String = activePcName

    fun setActivePcIp(ip: String?) {
        activePcIp = ip
    }

    @Synchronized
    fun start() {
        if (isRunning || listenerThread != null || beaconThread != null || statsThread != null) return
        isRunning = true

        startListener()
        startBeaconBroadcaster()
        startStatsSender()
    }

    fun stop() {
        val listener: Thread?
        val beacon: Thread?
        val stats: Thread?

        synchronized(this) {
            if (!isRunning && listenerThread == null && beaconThread == null && statsThread == null) {
                return
            }

            isRunning = false

            try { listenerSocket?.close() } catch (_: Exception) {}
            try { beaconSocket?.close() } catch (_: Exception) {}
            try { statsSocket?.close() } catch (_: Exception) {}

            listenerSocket = null
            beaconSocket = null
            statsSocket = null

            listener = listenerThread
            beacon = beaconThread
            stats = statsThread

            listenerThread = null
            beaconThread = null
            statsThread = null
        }

        listener?.interrupt()
        beacon?.interrupt()
        stats?.interrupt()
    }

    fun sendConfigSync(json: JSONObject, targetIp: String? = null) {
        val dest = targetIp ?: activePcIp
        val senderIp = AudioNative.nativeGetSenderIp()
        thread(name = "ConfigSyncSender", isDaemon = true) {
            try {
                val data = json.toString().toByteArray(Charsets.UTF_8)
                val sock = DatagramSocket().apply { broadcast = true }
                val targetList = mutableSetOf<String>()
                if (!dest.isNullOrBlank()) targetList.add(dest)
                if (!senderIp.isNullOrBlank()) targetList.add(senderIp)
                targetList.add("255.255.255.255")

                val localIps = getLocalIpv4Addresses()
                for (ip in localIps) {
                    val parts = ip.split(".")
                    if (parts.size == 4) {
                        targetList.add("${parts[0]}.${parts[1]}.${parts[2]}.255")
                    }
                }

                for (tIp in targetList) {
                    try {
                        sock.send(DatagramPacket(data, data.size, InetAddress.getByName(tIp), controlPort))
                    } catch (_: Exception) {}
                }
                sock.close()
            } catch (e: Exception) {
                Log.w(TAG, "Error sending CONFIG_SYNC", e)
            }
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

    private fun getLocalIpv4Addresses(): List<String> {
        val prioritized = mutableListOf<String>()
        val fallback = mutableListOf<String>()
        try {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces.hasMoreElements()) {
                val iface = interfaces.nextElement()
                if (iface.isLoopback || !iface.isUp) continue
                val name = iface.name.lowercase()
                if (isCellularOrDummyInterface(name)) continue

                val addresses = iface.inetAddresses
                while (addresses.hasMoreElements()) {
                    val addr = addresses.nextElement()
                    if (!addr.isLoopbackAddress && addr is Inet4Address) {
                        val ip = addr.hostAddress ?: continue
                        if (name.startsWith("ap") || name.startsWith("wlan") ||
                            name.startsWith("swlan") || name.startsWith("rndis") ||
                            name.startsWith("usb") || name.startsWith("ncm") ||
                            name.startsWith("bt-pan") || name.startsWith("bnep")) {
                            prioritized.add(ip)
                        } else {
                            fallback.add(ip)
                        }
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error enumerating IP addresses", e)
        }
        return prioritized + fallback
    }

    private fun startListener() {
        listenerThread = thread(name = "ControlListener", isDaemon = true) {
            try {
                val sock = DatagramSocket(null).apply {
                    reuseAddress = true
                    bind(InetSocketAddress(controlPort))
                }
                listenerSocket = sock
                Log.i(TAG, "Control listener bound to UDP $controlPort")

                val buffer = ByteArray(4096)
                while (isRunning) {
                    val packet = DatagramPacket(buffer, buffer.size)
                    sock.receive(packet)
                    val senderIp = packet.address.hostAddress
                    if (!senderIp.isNullOrBlank()) {
                        activePcIp = senderIp
                    }

                    try {
                        val text = String(packet.data, packet.offset, packet.length, Charsets.UTF_8)
                        val json = JSONObject(text)
                        val msgType = json.optString("type")
                        val pcName = json.optString("pc_name", "")
                        if (pcName.isNotBlank()) {
                            activePcName = pcName
                        }
                        if (msgType == "PC_ANNOUNCE") {
                            activePcIp = senderIp
                            AudioStreamService.micStreamerInstance?.updateTargetIp(senderIp)
                            Log.i(TAG, "Discovered active PC IP via PC_ANNOUNCE: $senderIp ($activePcName)")
                        } else if (msgType == "CONFIG_SYNC") {
                            onConfigSyncReceived?.invoke(json)
                        } else if (msgType == "MIC_CONTROL") {
                            val wantActive = json.optBoolean("active", false)
                            onMicControlReceived?.invoke(wantActive)
                        }
                    } catch (_: Exception) {}
                }
            } catch (e: Exception) {
                if (isRunning) Log.e(TAG, "Failed to bind UDP $controlPort", e)
            } finally {
                try { listenerSocket?.close() } catch (_: Exception) {}
            }
        }
    }

    private fun startBeaconBroadcaster() {
        beaconThread = thread(name = "BeaconBroadcaster", isDaemon = true) {
            try {
                val sock = DatagramSocket().apply { broadcast = true }
                beaconSocket = sock

                while (isRunning) {
                    val ips = getLocalIpv4Addresses()
                    val json = JSONObject().apply {
                        put("type", "BEACON")
                        put("app", "PhoneSpeaker")
                        put("version", 2)
                        put("ips", JSONArray(ips))
                        put("audio_port", audioPort)
                        put("device", getPhoneModelName())
                    }
                    val data = json.toString().toByteArray(Charsets.UTF_8)
                    try {
                        val broadcastAddr = InetAddress.getByName("255.255.255.255")
                        sock.send(DatagramPacket(data, data.size, broadcastAddr, controlPort))
                    } catch (_: Exception) {}

                    try {
                        for (ip in ips) {
                            val parts = ip.split(".")
                            if (parts.size == 4) {
                                val subnetBroadcast = "${parts[0]}.${parts[1]}.${parts[2]}.255"
                                val addr = InetAddress.getByName(subnetBroadcast)
                                sock.send(DatagramPacket(data, data.size, addr, controlPort))
                            }
                        }
                    } catch (_: Exception) {}

                    Thread.sleep(1000)
                }
            } catch (e: Exception) {
                if (isRunning) Log.w(TAG, "Beacon broadcaster error: ${e.message}")
            } finally {
                try { beaconSocket?.close() } catch (_: Exception) {}
            }
        }
    }

    private fun startStatsSender() {
        statsThread = thread(name = "StatsSender", isDaemon = true) {
            try {
                val sock = DatagramSocket()
                statsSocket = sock
                while (isRunning) {
                    Thread.sleep(500)
                    val targetIp = activePcIp ?: AudioNative.nativeGetSenderIp() ?: continue

                    val stats = AudioNative.getStats()
                    val json = JSONObject().apply {
                        put("type", "STATS")
                        put("buffer_ms", stats.bufferMs.toDouble())
                        put("target_ms", stats.targetMs.toDouble())
                        put("underruns", stats.underruns)
                        put("lost_packets", stats.lostPackets)
                        put("late_packets", stats.latePackets)
                        put("auto_mode", stats.autoMode)
                        put("rx_pps", stats.rxPps)
                        put("active_codec", stats.codec)
                        put("device", getPhoneModelName())
                        put("mic_streaming", AudioStreamService.isMicStreaming)
                        put("mic_pps", AudioStreamService.micStreamerInstance?.txPps?.get() ?: 0)
                    }

                    val data = json.toString().toByteArray(Charsets.UTF_8)
                    val dest = InetAddress.getByName(targetIp)
                    sock.send(DatagramPacket(data, data.size, dest, controlPort))
                }
            } catch (e: Exception) {
                if (isRunning) Log.w(TAG, "Stats sender error: ${e.message}")
            } finally {
                try { statsSocket?.close() } catch (_: Exception) {}
            }
        }
    }

    companion object {
        fun getPhoneModelName(): String {
            val manufacturer = android.os.Build.MANUFACTURER?.trim().orEmpty()
            val model = android.os.Build.MODEL?.trim().orEmpty()
            return if (model.startsWith(manufacturer, ignoreCase = true)) {
                model
            } else {
                "${manufacturer.replaceFirstChar { it.uppercase() }} $model".trim()
            }
        }
    }
}
