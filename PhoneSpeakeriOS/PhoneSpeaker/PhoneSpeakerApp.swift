//
//  PhoneSpeakerApp.swift
//  PhoneSpeaker
//
//  Created for PhoneSpeaker v2.0.
//

import SwiftUI

@main
struct PhoneSpeakerApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                .preferredColorScheme(.dark)
                .onAppear {
                    // Keep screen awake while studio is active
                    UIApplication.shared.isIdleTimerDisabled = true
                }
        }
    }
}

