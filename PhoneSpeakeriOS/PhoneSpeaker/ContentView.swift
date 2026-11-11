//
//  ContentView.swift
//  PhoneSpeaker
//
//  4-Tab Studio UI for PhoneSpeaker v2.0 on iOS:
//  PLAYER, SERVER, 🎉 PARTY (with 2-Button Chooser, QR URIs & Daisy-Chain Extend), SETTINGS.
//

import SwiftUI
import UIKit

public struct ContentView: View {
    @StateObject private var engine = PhoneSpeakerEngine.shared
    @State private var selectedTab: Int = 0

    // Accent Colors matching Android & PC Studio Dark Theme
    private let bgDark = Color(red: 0.07, green: 0.08, blue: 0.10)
    private let cardDark = Color(red: 0.12, green: 0.14, blue: 0.18)
    private let accentCyan = Color(red: 0.0, green: 0.85, blue: 0.85)
    private let accentGreen = Color(red: 0.15, green: 0.85, blue: 0.45)
    private let accentRed = Color(red: 0.95, green: 0.25, blue: 0.3)
    private let accentGold = Color(red: 1.0, green: 0.78, blue: 0.2)

    public var body: some View {
        ZStack {
            bgDark.ignoresSafeArea()

            TabView(selection: $selectedTab) {
                // Tab 1: PLAYER
                PlayerTabView(engine: engine)
                    .tabItem {
                        Label("Player", systemImage: "speaker.wave.3.fill")
                    }
                    .tag(0)

                // Tab 2: SERVER
                ServerTabView(engine: engine)
                    .tabItem {
                        Label("Server", systemImage: "antenna.radiowaves.left.and.right")
                    }
                    .tag(1)

                // Tab 3: PARTY
                PartyTabView(engine: engine)
                    .tabItem {
                        Label("Party", systemImage: "party.popper.fill")
                    }
                    .tag(2)

                // Tab 4: SETTINGS
                SettingsTabView(engine: engine)
                    .tabItem {
                        Label("Settings", systemImage: "gearshape.fill")
                    }
                    .tag(3)
            }
            .accentColor(accentCyan)
        }
    }
}

// MARK: - Tab 1: PLAYER
struct PlayerTabView: View {
    @ObservedObject var engine: PhoneSpeakerEngine
    @State private var manualIpInput: String = ""

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                // Header Banner
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("PhoneSpeaker v2.0")
                            .font(.title2).bold()
                            .foregroundColor(.white)
                        Text(engine.statusMessage)
                            .font(.subheadline)
                            .foregroundColor(engine.isAudioPlaying ? .green : .gray)
                    }
                    Spacer()
                    Image(systemName: engine.isAudioPlaying ? "waveform" : "waveform.slash")
                        .font(.title)
                        .foregroundColor(engine.isAudioPlaying ? .cyan : .gray)
                }
                .padding()
                .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                .cornerRadius(12)

                // Quick Stats Card
                HStack(spacing: 12) {
                    StatBox(title: "Latency", value: String(format: "%.1f ms", engine.latencyMs), icon: "gauge.with.needle")
                    StatBox(title: "Loss / RX", value: "\(String(format: "%.1f%%", engine.packetLossPercent)) (\(engine.rxPps) pps)", icon: "chart.bar.fill")
                    StatBox(title: "Local IP", value: engine.localIpAddress, icon: "network")
                }

                // Big Action Button
                Button(action: {
                    if engine.isAudioPlaying {
                        engine.stopSpeakerPlayer()
                    } else {
                        engine.startSpeakerPlayer(manualHost: manualIpInput)
                    }
                }) {
                    HStack {
                        Image(systemName: engine.isAudioPlaying ? "stop.fill" : "play.fill")
                        Text(engine.isAudioPlaying ? "STOP SPEAKER PLAYER" : "START SPEAKER PLAYER")
                            .bold()
                    }
                    .frame(maxWidth: .infinity)
                    .padding()
                    .background(engine.isAudioPlaying ? Color.red : Color.green)
                    .foregroundColor(.white)
                    .cornerRadius(12)
                }

                // Manual Host IP Bar
                HStack {
                    TextField("Server PC IP (e.g. 192.168.1.10)", text: $manualIpInput)
                        .textFieldStyle(PlainTextFieldStyle())
                        .padding(10)
                        .background(Color(red: 0.18, green: 0.20, blue: 0.24))
                        .foregroundColor(.white)
                        .cornerRadius(8)
                    if !manualIpInput.isEmpty {
                        Button("Apply") {
                            engine.connectedServerIp = manualIpInput
                        }
                        .foregroundColor(.cyan)
                    }
                }
                .padding()
                .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                .cornerRadius(12)

                // Audio Controls (Volume & Sound Mode)
                VStack(alignment: .leading, spacing: 12) {
                    Text("Audio Output Controls").font(.headline).foregroundColor(.white)

                    HStack(spacing: 12) {
                        Button(action: {
                            engine.isMuted.toggle()
                            engine.sendConfigSync()
                        }) {
                            Image(systemName: engine.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                                .foregroundColor(engine.isMuted ? .red : .cyan)
                                .font(.title3)
                        }

                        Slider(
                            value: Binding(
                                get: { engine.volume },
                                set: { newVal in
                                    engine.volume = newVal
                                    engine.sendConfigSync()
                                }
                            ),
                            in: 0...2.0
                        )
                        .accentColor(.cyan)

                        Text("\(Int(engine.volume * 100))%")
                            .foregroundColor(.white)
                            .font(.callout).bold()
                            .frame(width: 48, alignment: .trailing)
                    }

                    Picker("Sound Mode", selection: $engine.soundMode) {
                        Text("Balanced").tag("BALANCED")
                        Text("Gaming").tag("GAMING")
                        Text("Quality").tag("QUALITY")
                        Text("Custom").tag("CUSTOM")
                    }
                    .pickerStyle(SegmentedPickerStyle())
                    .onChange(of: engine.soundMode) { _ in
                        engine.sendConfigSync()
                    }
                }
                .padding()
                .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                .cornerRadius(12)

                // Wireless Virtual Mic Card
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Image(systemName: "mic.fill")
                            .foregroundColor(engine.isMicStreaming ? .green : .cyan)
                        Text("Wireless PC Microphone").font(.headline).foregroundColor(.white)
                        Spacer()
                        if engine.isMicStreaming {
                            Text("\(engine.micTxPps) pps")
                                .font(.caption).foregroundColor(.green)
                        }
                    }
                    Text("Streams your iPhone microphone to your PC virtual mic device over UDP port 5006.")
                        .font(.caption)
                        .foregroundColor(.gray)

                    Button(action: {
                        engine.toggleMicStreaming()
                    }) {
                        HStack {
                            Image(systemName: engine.isMicStreaming ? "mic.slash.fill" : "mic.fill")
                            Text(engine.isMicStreaming ? "STOP MIC STREAM" : "START MIC STREAM (Phone → PC)")
                                .bold()
                        }
                        .frame(maxWidth: .infinity)
                        .padding()
                        .background(engine.isMicStreaming ? Color.red : Color.cyan)
                        .foregroundColor(.white)
                        .cornerRadius(10)
                    }
                }
                .padding()
                .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                .cornerRadius(12)
            }
            .padding()
        }
    }
}

// MARK: - Tab 2: SERVER
struct ServerTabView: View {
    @ObservedObject var engine: PhoneSpeakerEngine

    var body: some View {
        VStack(spacing: 20) {
            Text("Audio Broadcaster & Studio Link")
                .font(.title2).bold().foregroundColor(.white)

            VStack(alignment: .leading, spacing: 12) {
                Text("Broadcast Network Endpoints").font(.headline).foregroundColor(.cyan)
                Text("• Audio Receiver: UDP 5004 (Lossless PCM16 / Opus 48kHz)")
                Text("• Control Protocol: UDP 5005 (JSON Discovery)")
                Text("• Mic Streamer: UDP 5006 (Phone → PC 5ms 972-byte frames)")
                Text("• Local IP Address: \(engine.localIpAddress)")
            }
            .foregroundColor(.white)
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(red: 0.12, green: 0.14, blue: 0.18))
            .cornerRadius(12)

            Spacer()
        }
        .padding()
    }
}

// MARK: - Tab 3: 🎉 PARTY
struct PartyTabView: View {
    @ObservedObject var engine: PhoneSpeakerEngine
    @State private var showScanner: Bool = false
    @State private var manualJoinIp: String = ""
    @State private var manualPeerInput: String = ""

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if engine.partyScreen == "CHOOSER" {
                    // Clean 2-Button Chooser Screen
                    VStack(spacing: 24) {
                        Text("Multi-Device Party Mode")
                            .font(.title).bold().foregroundColor(.white)
                        Text("Play synchronized audio across multiple iPhones, Androids, and PCs simultaneously over Wi-Fi, Hotspot, or Daisy-Chain.")
                            .font(.subheadline)
                            .foregroundColor(.gray)
                            .multilineTextAlignment(.center)

                        // Button 1: HOST A PARTY
                        Button(action: {
                            engine.hostParty()
                        }) {
                            VStack(spacing: 8) {
                                Image(systemName: "crown.fill")
                                    .font(.system(size: 36))
                                    .foregroundColor(.yellow)
                                Text("👑 HOST A PARTY")
                                    .font(.headline).bold()
                                    .foregroundColor(.white)
                                Text("Broadcast audio to all nearby devices")
                                    .font(.caption).foregroundColor(.gray)
                            }
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 24)
                            .background(Color(red: 0.15, green: 0.17, blue: 0.22))
                            .cornerRadius(16)
                            .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.yellow.opacity(0.6), lineWidth: 1.5))
                        }

                        // Button 2: JOIN A PARTY
                        Button(action: {
                            engine.partyScreen = "JOIN"
                        }) {
                            VStack(spacing: 8) {
                                Image(systemName: "headphones")
                                    .font(.system(size: 36))
                                    .foregroundColor(.cyan)
                                Text("🎧 JOIN A PARTY")
                                    .font(.headline).bold()
                                    .foregroundColor(.white)
                                Text("Connect to an existing Party Host")
                                    .font(.caption).foregroundColor(.gray)
                            }
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 24)
                            .background(Color(red: 0.15, green: 0.17, blue: 0.22))
                            .cornerRadius(16)
                            .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.cyan.opacity(0.6), lineWidth: 1.5))
                        }
                    }
                    .padding(.top, 40)
                    .padding(.horizontal)

                } else if engine.partyScreen == "HOST" {
                    // Host Screen
                    VStack(spacing: 16) {
                        HStack {
                            Text("👑 Party Host Mode").font(.title2).bold().foregroundColor(.yellow)
                            Spacer()
                            Button("Exit Host") {
                                engine.leaveParty()
                            }
                            .foregroundColor(.red)
                        }

                        // QR Code Card with phonespeaker://join URI
                        let hostQrUri = engine.makePartyQrUri(isExtended: false)
                        if let qrImg = QRCodeHelper.generateQRCode(from: hostQrUri) {
                            VStack(spacing: 8) {
                                Image(uiImage: qrImg)
                                    .interpolation(.none)
                                    .resizable()
                                    .scaledToFit()
                                    .frame(width: 180, height: 180)
                                    .padding(8)
                                    .background(Color.white)
                                    .cornerRadius(12)

                                Text("Scan with iPhone, Android, or PC Studio")
                                    .font(.caption).foregroundColor(.gray)

                                Text("Host IP: \(engine.localIpAddress):5004")
                                    .font(.footnote).bold().foregroundColor(.yellow)

                                HStack(spacing: 12) {
                                    Button("↻ Refresh Host IP & QR") {
                                        engine.broadcastBeacon()
                                    }
                                    .font(.caption)
                                    .foregroundColor(.cyan)

                                    Button("📋 Copy Link") {
                                        UIPasteboard.general.string = hostQrUri
                                    }
                                    .font(.caption)
                                    .foregroundColor(.yellow)
                                }
                            }
                            .padding()
                            .frame(maxWidth: .infinity)
                            .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                            .cornerRadius(12)
                        }

                        // Manual Add Peer Bar
                        HStack {
                            TextField("Peer IP (e.g. 192.168.1.55)", text: $manualPeerInput)
                                .textFieldStyle(PlainTextFieldStyle())
                                .padding(10)
                                .background(Color(red: 0.18, green: 0.20, blue: 0.24))
                                .foregroundColor(.white)
                                .cornerRadius(8)
                            Button("Add Peer") {
                                if !manualPeerInput.isEmpty {
                                    engine.addManualPeer(ip: manualPeerInput)
                                    manualPeerInput = ""
                                }
                            }
                            .foregroundColor(.cyan)
                        }
                        .padding()
                        .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                        .cornerRadius(12)

                        // Connected Party Members
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Connected Party Members (\(engine.partyMembers.count))")
                                .font(.headline).foregroundColor(.white)

                            if engine.partyMembers.isEmpty {
                                Text("Waiting for friends to join...").font(.caption).foregroundColor(.gray)
                            } else {
                                ForEach(engine.partyMembers) { member in
                                    HStack {
                                        Image(systemName: "iphone").foregroundColor(.cyan)
                                        VStack(alignment: .leading) {
                                            Text(member.name).foregroundColor(.white).bold()
                                            Text(member.ip).font(.caption).foregroundColor(.gray)
                                        }
                                        Spacer()
                                        Button("Kick") {
                                            engine.kickMember(ip: member.ip)
                                        }
                                        .font(.caption)
                                        .foregroundColor(.red)
                                    }
                                    .padding(8)
                                    .background(Color(red: 0.18, green: 0.20, blue: 0.25))
                                    .cornerRadius(8)
                                }
                            }
                        }
                        .padding()
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                        .cornerRadius(12)
                    }
                    .padding()

                } else if engine.partyScreen == "JOIN" {
                    // Join Screen
                    VStack(spacing: 16) {
                        HStack {
                            Text("🎧 Join A Party").font(.title2).bold().foregroundColor(.cyan)
                            Spacer()
                            Button("Back") {
                                engine.leaveParty()
                            }
                            .foregroundColor(.gray)
                        }

                        // Live Discovered Party Hosts Card
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Text("Discovered Party Hosts").font(.headline).foregroundColor(.white)
                                Spacer()
                                Button("↻ Refresh") {
                                    engine.broadcastBeacon()
                                }
                                .font(.caption).foregroundColor(.cyan)
                            }

                            if engine.discoveredPartyHosts.isEmpty {
                                Text("Scanning local network for Party Hosts...").font(.caption).foregroundColor(.gray)
                            } else {
                                ForEach(engine.discoveredPartyHosts) { host in
                                    Button(action: {
                                        engine.joinParty(hostIp: host.ip, port: host.port)
                                    }) {
                                        HStack {
                                            Image(systemName: host.isExtended ? "antenna.radiowaves.left.and.right" : "crown.fill")
                                                .foregroundColor(host.isExtended ? .yellow : .green)
                                            VStack(alignment: .leading) {
                                                Text(host.name).foregroundColor(.white).bold()
                                                Text("\(host.ip):\(host.port) \(host.isExtended ? "(Extended Repeater)" : "(Host)")")
                                                    .font(.caption).foregroundColor(.gray)
                                            }
                                            Spacer()
                                            Text("TAP TO JOIN")
                                                .font(.caption).bold()
                                                .foregroundColor(.cyan)
                                                .padding(6)
                                                .background(Color.cyan.opacity(0.2))
                                                .cornerRadius(6)
                                        }
                                        .padding(8)
                                        .background(Color(red: 0.18, green: 0.20, blue: 0.25))
                                        .cornerRadius(8)
                                    }
                                }
                            }
                        }
                        .padding()
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                        .cornerRadius(12)

                        // Action Buttons: Scanner & Paste
                        HStack(spacing: 12) {
                            Button(action: {
                                showScanner = true
                            }) {
                                HStack {
                                    Image(systemName: "camera.viewfinder")
                                    Text("SCAN QR CODE").bold()
                                }
                                .frame(maxWidth: .infinity)
                                .padding()
                                .background(Color.cyan)
                                .foregroundColor(.white)
                                .cornerRadius(12)
                            }

                            Button(action: {
                                if let pasted = UIPasteboard.general.string {
                                    let (ip, port) = PhoneSpeakerEngine.parseHostIpFromUri(pasted)
                                    if !ip.isEmpty {
                                        engine.joinParty(hostIp: ip, port: port)
                                    }
                                }
                            }) {
                                HStack {
                                    Image(systemName: "doc.on.clipboard")
                                    Text("PASTE LINK").bold()
                                }
                                .frame(maxWidth: .infinity)
                                .padding()
                                .background(Color(red: 0.2, green: 0.24, blue: 0.3))
                                .foregroundColor(.white)
                                .cornerRadius(12)
                            }
                        }

                        // Manual Join Bar
                        HStack {
                            TextField("Host IP (e.g. 192.168.1.50)", text: $manualJoinIp)
                                .textFieldStyle(PlainTextFieldStyle())
                                .padding(10)
                                .background(Color(red: 0.18, green: 0.20, blue: 0.24))
                                .foregroundColor(.white)
                                .cornerRadius(8)
                            Button("Connect") {
                                if !manualJoinIp.isEmpty {
                                    let (ip, port) = PhoneSpeakerEngine.parseHostIpFromUri(manualJoinIp)
                                    engine.joinParty(hostIp: ip, port: port)
                                }
                            }
                            .foregroundColor(.cyan)
                        }
                        .padding()
                        .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                        .cornerRadius(12)

                        // Post-Join: Extend This Party (Hotspot / Daisy-Chain Repeater)
                        if engine.isPartyJoined {
                            VStack(alignment: .leading, spacing: 12) {
                                Toggle(isOn: Binding(
                                    get: { engine.isPartyExtending },
                                    set: { _ in engine.toggleExtendParty() }
                                )) {
                                    VStack(alignment: .leading) {
                                        Text("📡 EXTEND THIS PARTY")
                                            .font(.headline).bold().foregroundColor(.yellow)
                                        Text("Turn on Personal Hotspot or Wi-Fi Repeater to daisy-chain audio to other nearby devices!")
                                            .font(.caption).foregroundColor(.gray)
                                    }
                                }

                                if engine.isPartyExtending {
                                    let extQrUri = engine.makePartyQrUri(isExtended: true)
                                    if let repQr = QRCodeHelper.generateQRCode(from: extQrUri) {
                                        VStack(spacing: 6) {
                                            Image(uiImage: repQr)
                                                .interpolation(.none)
                                                .resizable()
                                                .scaledToFit()
                                                .frame(width: 140, height: 140)
                                                .padding(6)
                                                .background(Color.white)
                                                .cornerRadius(10)

                                            Text("Repeater IP: \(engine.localIpAddress)")
                                                .font(.caption).bold().foregroundColor(.yellow)

                                            Text("Downstream Peers: \(engine.partyMembers.count)")
                                                .font(.caption2).foregroundColor(.gray)
                                        }
                                        .frame(maxWidth: .infinity)
                                    }
                                }
                            }
                            .padding()
                            .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                            .cornerRadius(12)
                        }
                    }
                    .padding()
                }
            }
        }
        .sheet(isPresented: $showScanner) {
            QRScannerView { code in
                let (hostIp, port) = PhoneSpeakerEngine.parseHostIpFromUri(code)
                if !hostIp.isEmpty {
                    engine.joinParty(hostIp: hostIp, port: port)
                }
            }
        }
    }
}

// MARK: - Tab 4: SETTINGS
struct SettingsTabView: View {
    @ObservedObject var engine: PhoneSpeakerEngine

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Studio Settings").font(.title2).bold().foregroundColor(.white)

                VStack(alignment: .leading, spacing: 12) {
                    Text("Jitter Buffer Target").font(.headline).foregroundColor(.white)
                    Picker("Buffer Target", selection: $engine.bufferTargetMs) {
                        Text("10 ms (Ultra Low)").tag(10)
                        Text("20 ms (Default)").tag(20)
                        Text("40 ms (Safe)").tag(40)
                        Text("80 ms (High Jitter)").tag(80)
                    }
                    .pickerStyle(SegmentedPickerStyle())
                    .onChange(of: engine.bufferTargetMs) { _ in
                        engine.sendConfigSync()
                    }
                }
                .padding()
                .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                .cornerRadius(12)

                VStack(alignment: .leading, spacing: 10) {
                    Text("About PhoneSpeaker v2.0").font(.headline).foregroundColor(.white)
                    Text("Cross-platform Ultra Low-Latency Audio Streaming Studio.")
                        .font(.caption).foregroundColor(.gray)
                    Text("Supported: Windows (WASAPI Loopback), Linux (PulseAudio/PipeWire), macOS (CoreAudio/BlackHole), Android (Oboe/AAudio), iOS (AVAudioEngine).")
                        .font(.caption2).foregroundColor(.gray)
                }
                .padding()
                .background(Color(red: 0.12, green: 0.14, blue: 0.18))
                .cornerRadius(12)
            }
            .padding()
        }
    }
}

// MARK: - Reusable StatBox View
struct StatBox: View {
    let title: String
    let value: String
    let icon: String

    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: icon).foregroundColor(.cyan).font(.subheadline)
            Text(value).bold().foregroundColor(.white).font(.callout)
            Text(title).font(.caption2).foregroundColor(.gray)
        }
        .frame(maxWidth: .infinity)
        .padding(10)
        .background(Color(red: 0.12, green: 0.14, blue: 0.18))
        .cornerRadius(10)
    }
}
