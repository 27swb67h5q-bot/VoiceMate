import SwiftUI
import AVFoundation

/// Full-duplex real-time voice call view.
///
/// Shows:
/// - Pulsating avatar with status indicators
/// - Live user transcription
/// - AI streaming text
/// - Call duration
/// - End call button
/// - Waveform visualization for voice activity
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
    @State private var showCopied = false
    
    init(serverHost: String, serverPort: String, voice: String, persona: String, speed: Double, onTranscript: @escaping ([(isUser: Bool, text: String)]) -> Void = { _ in }, onTurnCompleted: @escaping (_ isUser: Bool, _ text: String) -> Void = { _, _ in }) {
        self.serverHost = serverHost
        self.serverPort = serverPort
        self.voice = voice
        self.persona = persona
        self.speed = speed
        self.onTranscript = onTranscript
        
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
            .overlay(
                // Waveform while user is speaking
                Group {
                    if callService.isUserSpeaking {
                        WaveformView()
                            .frame(width: 200, height: 40)
                            .offset(y: 90)
                    }
                }
            )
            
            // Status text
            Text(statusText)
                .font(.title2)
                .fontWeight(.semibold)
                .foregroundColor(.white)
                .padding(.top, 8)
            
            // AI streaming text
            if !callService.aiText.isEmpty {
                VStack(spacing: 4) {
                    Text("AI:")
                        .font(.caption)
                        .foregroundColor(.green.opacity(0.7))
                        .frame(maxWidth: .infinity, alignment: .leading)
                    
                    Text(callService.aiText)
                        .font(.body)
                        .foregroundColor(.white)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.horizontal, 32)
                .padding(.vertical, 12)
                .background(Color.white.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .padding(.horizontal, 16)
            }
            
            // Live user transcription
            if !callService.currentText.isEmpty {
                VStack(spacing: 4) {
                    Text("你:")
                        .font(.caption)
                        .foregroundColor(.blue.opacity(0.7))
                        .frame(maxWidth: .infinity, alignment: .trailing)
                    
                    Text(callService.currentText)
                        .font(.body)
                        .foregroundColor(.white.opacity(0.9))
                        .multilineTextAlignment(.trailing)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                .padding(.horizontal, 32)
                .padding(.vertical, 12)
                .background(Color.white.opacity(0.05))
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .padding(.horizontal, 16)
            }
            
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

// MARK: - Simple Waveform View

struct WaveformView: View {
    @State private var animating = false
    
    var body: some View {
        HStack(spacing: 3) {
            ForEach(0..<20) { i in
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.blue.opacity(0.7))
                    .frame(width: 3, height: animating ? CGFloat.random(in: 8...32) : 8)
                    .animation(
                        Animation.easeInOut(duration: 0.3).repeatForever().delay(Double(i) * 0.05),
                        value: animating
                    )
            }
        }
        .onAppear { animating = true }
    }
}

#Preview {
    RealtimeCallView(
        serverHost: "192.168.10.227",
        serverPort: "8000",
        voice: "zh-CN-XiaoxiaoNeural",
        persona: "love",
        speed: 1.0
    )
}
