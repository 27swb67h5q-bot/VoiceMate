import SwiftUI

/// Simplified real-time voice call view using LiveKit.
/// Clean phone-call style: avatar, status, duration, hang up button.
/// LiveKit handles all audio capture/playback/VAD — we just manage UI.
struct RealtimeCallView: View {
    @Environment(\.dismiss) var dismiss
    
    // Configuration from parent
    let serverHost: String
    let serverPort: String
    let voice: String
    let persona: String
    let speed: Double
    let onTranscript: ([(isUser: Bool, text: String)]) -> Void
    let onTurnCompleted: (_ isUser: Bool, _ text: String) -> Void
    
    @StateObject private var callService: RealtimeCallService
    
    @State private var pulseScale: CGFloat = 1.0
    @State private var pulseOpacity: Double = 0.6
    
    init(serverHost: String, serverPort: String, voice: String, persona: String, speed: Double, onTranscript: @escaping ([(isUser: Bool, text: String)]) -> Void = { _ in }, onTurnCompleted: @escaping (_ isUser: Bool, _ text: String) -> Void = { _, _ in }) {
        self.serverHost = serverHost
        self.serverPort = serverPort
        self.voice = voice
        self.persona = persona
        self.speed = speed
        self.onTranscript = onTranscript
        self.onTurnCompleted = onTurnCompleted
        
        let service = RealtimeCallService(
            serverHost: serverHost,
            serverPort: serverPort,
            voice: voice,
            persona: persona,
            speed: speed
        )
        service.onTurnCompleted = onTurnCompleted
        _callService = StateObject(wrappedValue: service)
    }
    
    var body: some View {
        VStack(spacing: 20) {
            Spacer()
            
            // Pulsing avatar circle
            ZStack {
                // Outer pulsing rings
                Circle()
                    .stroke(pulseRingColor.opacity(pulseOpacity), lineWidth: 3)
                    .frame(width: 160, height: 160)
                    .scaleEffect(pulseScale)
                
                Circle()
                    .stroke(pulseRingColor.opacity(pulseOpacity * 0.5), lineWidth: 2)
                    .frame(width: 180, height: 180)
                    .scaleEffect(pulseScale * 1.1)
                
                // Center avatar
                Circle()
                    .fill(
                        LinearGradient(
                            colors: avatarGradientColors,
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                    .frame(width: 120, height: 120)
                    .overlay(
                        Image(systemName: avatarIcon)
                            .font(.system(size: 48))
                            .foregroundColor(.white)
                    )
            }
            
            // Status text
            Text(statusText)
                .font(.title2)
                .fontWeight(.semibold)
                .foregroundColor(.white)
                .padding(.top, 8)
            
            // Call duration
            Text(formatDuration(callService.callDuration))
                .font(.subheadline)
                .foregroundColor(.gray)
            
            Spacer()
            
            // Error message
            if let error = callService.errorMessage {
                Text(error)
                    .font(.caption)
                    .foregroundColor(.red.opacity(0.8))
                    .padding(.bottom, 8)
            }
            
            // End call button
            Button(action: endCall) {
                ZStack {
                    Circle()
                        .fill(Color.red)
                        .frame(width: 72, height: 72)
                    
                    Image(systemName: "phone.down.fill")
                        .font(.title)
                        .foregroundColor(.white)
                }
            }
            .disabled(!callService.isCallActive)
            .opacity(callService.isCallActive ? 1.0 : 0.5)
            
            Text(callService.isCallActive ? "点击挂断" : "通话已结束")
                .font(.caption)
                .foregroundColor(.gray.opacity(0.7))
                .padding(.bottom, 40)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black.opacity(0.9))
        .ignoresSafeArea()
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true
            startPulsing()
            callService.startCall()
        }
        .onDisappear {
            UIApplication.shared.isIdleTimerDisabled = false
            let transcript = callService.transcript
            callService.endCall()
            // Pass transcript back to ContentView
            if !transcript.isEmpty {
                onTranscript(transcript)
            }
        }
        .onChange(of: callService.isCallActive) { active in
            if !active {
                dismiss()
            }
        }
    }
    
    // MARK: - Computed Properties
    
    private var statusText: String {
        if callService.isAISpeaking {
            return "🎙️ AI 说话中..."
        } else if callService.isUserSpeaking {
            return "🎤 正在听你说话..."
        } else if callService.isCallActive {
            return "💬 实时通话中..."
        } else {
            return "通话结束"
        }
    }
    
    private var avatarIcon: String {
        if callService.isAISpeaking {
            return "waveform"
        } else if callService.isUserSpeaking {
            return "mic.fill"
        } else {
            return "phone.fill"
        }
    }
    
    private var avatarGradientColors: [Color] {
        if callService.isAISpeaking {
            return [.green, .teal]
        } else if callService.isUserSpeaking {
            return [.blue, .purple]
        } else {
            return [.purple, .pink]
        }
    }
    
    private var pulseRingColor: Color {
        if callService.isAISpeaking {
            return .green
        } else if callService.isUserSpeaking {
            return .blue
        } else {
            return .purple
        }
    }
    
    // MARK: - Actions
    
    private func endCall() {
        callService.endCall()
        dismiss()
    }
    
    // MARK: - Animations
    
    private func startPulsing() {
        withAnimation(
            Animation.easeInOut(duration: 1.5).repeatForever(autoreverses: true)
        ) {
            pulseScale = 1.15
            pulseOpacity = 0.2
        }
    }
    
    private func formatDuration(_ interval: TimeInterval) -> String {
        let minutes = Int(interval) / 60
        let seconds = Int(interval) % 60
        return String(format: "%02d:%02d", minutes, seconds)
    }
}
