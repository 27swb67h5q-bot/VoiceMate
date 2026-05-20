import SwiftUI
import AVFoundation

/// Real-time call view with pulsing animation
struct RealtimeCallView: View {
    @Environment(\.dismiss) var dismiss
    @State private var pulseScale: CGFloat = 1.0
    @State private var pulseOpacity: Double = 0.6
    @State private var callDuration: TimeInterval = 0
    @State private var timer: Timer?
    
    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            
            // Pulsing avatar circle
            ZStack {
                // Outer pulsing rings
                Circle()
                    .stroke(Color.purple.opacity(pulseOpacity), lineWidth: 3)
                    .frame(width: 160, height: 160)
                    .scaleEffect(pulseScale)
                
                Circle()
                    .stroke(Color.purple.opacity(pulseOpacity * 0.5), lineWidth: 2)
                    .frame(width: 180, height: 180)
                    .scaleEffect(pulseScale * 1.1)
                
                // Center avatar
                Circle()
                    .fill(
                        LinearGradient(
                            colors: [.purple, .pink],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                    .frame(width: 120, height: 120)
                    .overlay(
                        Image(systemName: "phone.fill")
                            .font(.system(size: 48))
                            .foregroundColor(.white)
                    )
            }
            
            Text("实时通话中...")
                .font(.title2)
                .fontWeight(.semibold)
                .foregroundColor(.white)
            
            // Call duration
            Text(formatDuration(callDuration))
                .font(.subheadline)
                .foregroundColor(.gray)
            
            Spacer()
            
            // End call button
            Button(action: {
                dismiss()
            }) {
                ZStack {
                    Circle()
                        .fill(Color.red)
                        .frame(width: 72, height: 72)
                    
                    Image(systemName: "phone.down.fill")
                        .font(.title)
                        .foregroundColor(.white)
                }
            }
            
            Text("点击挂断")
                .font(.caption)
                .foregroundColor(.gray.opacity(0.7))
                .padding(.bottom, 40)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black.opacity(0.9))
        .ignoresSafeArea()
        .onAppear {
            startPulsing()
            startTimer()
        }
        .onDisappear {
            timer?.invalidate()
        }
    }
    
    private func startPulsing() {
        withAnimation(
            Animation.easeInOut(duration: 1.5).repeatForever(autoreverses: true)
        ) {
            pulseScale = 1.15
            pulseOpacity = 0.2
        }
    }
    
    private func startTimer() {
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            callDuration += 1
        }
    }
    
    private func formatDuration(_ interval: TimeInterval) -> String {
        let minutes = Int(interval) / 60
        let seconds = Int(interval) % 60
        return String(format: "%02d:%02d", minutes, seconds)
    }
}

#Preview {
    RealtimeCallView()
}
