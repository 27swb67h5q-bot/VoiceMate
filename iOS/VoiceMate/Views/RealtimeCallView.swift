import SwiftUI

struct RealtimeCallView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var service: LiveKitCallService

    let onTranscript: ([(isUser: Bool, text: String)]) -> Void
    let onTurnCompleted: (_ isUser: Bool, _ text: String) -> Void

    init(
        serverHost: String,
        serverPort: String,
        voice: String,
        persona: String,
        speed: Double,
        onTranscript: @escaping ([(isUser: Bool, text: String)]) -> Void = { _ in },
        onTurnCompleted: @escaping (_ isUser: Bool, _ text: String) -> Void = { _, _ in }
    ) {
        let callService = LiveKitCallService(
            serverHost: serverHost,
            serverPort: serverPort,
            voice: voice,
            persona: persona,
            speed: speed
        )
        callService.onTurnCompleted = onTurnCompleted
        _service = StateObject(wrappedValue: callService)
        self.onTranscript = onTranscript
        self.onTurnCompleted = onTurnCompleted
    }

    var body: some View {
        VStack(spacing: 28) {
            Spacer()

            ZStack {
                Circle()
                    .fill(service.isAISpeaking ? Color.green.opacity(0.18) : Color.white.opacity(0.08))
                    .frame(width: 176, height: 176)
                Circle()
                    .fill(Color.white.opacity(0.10))
                    .frame(width: 126, height: 126)
                Image(systemName: service.isAISpeaking ? "waveform" : "phone.fill")
                    .font(.system(size: 44, weight: .semibold))
                    .foregroundStyle(.white)
            }

            VStack(spacing: 8) {
                Text(service.statusText)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.white)
                Text(format(service.callDuration))
                    .font(.system(.body, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.68))
            }

            if let error = service.errorMessage {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.red.opacity(0.9))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 28)
            }

            Spacer()

            Button {
                service.endCall()
                dismiss()
            } label: {
                Image(systemName: "phone.down.fill")
                    .font(.title2.weight(.bold))
                    .foregroundStyle(.white)
                    .frame(width: 72, height: 72)
                    .background(Color.red, in: Circle())
            }
            .padding(.bottom, 42)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(red: 0.08, green: 0.09, blue: 0.10))
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true
            service.startCall()
        }
        .onDisappear {
            UIApplication.shared.isIdleTimerDisabled = false
            service.endCall()
        }
    }

    private func format(_ duration: TimeInterval) -> String {
        let value = Int(duration)
        return String(format: "%02d:%02d", value / 60, value % 60)
    }
}
