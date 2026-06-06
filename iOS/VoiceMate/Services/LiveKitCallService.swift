import Foundation
import AVFoundation
import LiveKit
import SwiftUI

final class LiveKitCallService: NSObject, ObservableObject {
    @Published var isCallActive = false
    @Published var isConnecting = false
    @Published var isAISpeaking = false
    @Published var statusText = "准备连接"
    @Published var callDuration: TimeInterval = 0
    @Published var errorMessage: String?
    @Published var transcript: [(isUser: Bool, text: String)] = []
    @Published var metricsText: String?
    @Published var emotionText: String?

    private let serverHost: String
    private let serverPort: String
    private let voice: String
    private let persona: String
    private let speed: Double
    private var room: Room?
    private var durationTimer: Timer?

    var onTurnCompleted: ((_ isUser: Bool, _ text: String) -> Void)?

    init(serverHost: String, serverPort: String, voice: String, persona: String, speed: Double) {
        self.serverHost = serverHost
        self.serverPort = serverPort
        self.voice = voice
        self.persona = persona
        self.speed = speed
        super.init()
    }

    func startCall() {
        guard !isConnecting && !isCallActive else { return }
        isConnecting = true
        statusText = "正在接通..."
        errorMessage = nil
        Task { await connect() }
    }

    func endCall() {
        durationTimer?.invalidate()
        durationTimer = nil
        let room = room
        self.room = nil
        Task { await room?.disconnect() }
        isCallActive = false
        isConnecting = false
        isAISpeaking = false
        statusText = "通话结束"
        callDuration = 0
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func connect() async {
        do {
            try configureCallAudioSession()
            let token = try await fetchToken()
            let room = Room(delegate: self)
            self.room = room

            let capture = AudioCaptureOptions(
                echoCancellation: true,
                autoGainControl: true,
                noiseSuppression: true
            )
            let options = RoomOptions(
                defaultAudioCaptureOptions: capture,
                adaptiveStream: true
            )
            try await room.connect(
                url: token.url ?? "ws://\(serverHost):7880",
                token: token.token,
                roomOptions: options
            )
            try await room.localParticipant.setMicrophone(enabled: true)
            await MainActor.run {
                self.isConnecting = false
                self.isCallActive = true
                self.statusText = "正在通话"
                self.startTimer()
            }
        } catch {
            await MainActor.run {
                self.isConnecting = false
                self.isCallActive = false
                self.statusText = "连接失败"
                self.errorMessage = error.localizedDescription
            }
        }
    }

    private func configureCallAudioSession() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(
            .playAndRecord,
            mode: .voiceChat,
            options: [.defaultToSpeaker, .allowBluetooth, .allowBluetoothA2DP]
        )
        try session.setPreferredIOBufferDuration(0.01)
        try session.setActive(true)
    }

    private func fetchToken() async throws -> LiveKitTokenResponse {
        let url = URL(string: "http://\(serverHost):\(serverPort)/v1/livekit/token")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "voice": voice,
            "persona": persona,
            "speed": speed,
        ])
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw VoiceMateLiveKitError.invalidTokenResponse
        }
        return try JSONDecoder().decode(LiveKitTokenResponse.self, from: data)
    }

    private func startTimer() {
        callDuration = 0
        durationTimer?.invalidate()
        durationTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.isCallActive else { return }
                self.callDuration += 1
            }
        }
    }

    private func handleData(_ data: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = object["type"] as? String else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            switch type {
            case "user_transcript":
                if let text = object["text"] as? String, object["is_final"] as? Bool == true {
                    self.transcript.append((true, text))
                    self.onTurnCompleted?(true, text)
                }
            case "ai_text":
                if let text = object["text"] as? String {
                    self.statusText = text
                }
            case "ai_speaking":
                self.isAISpeaking = true
                self.statusText = "AI 正在说话"
            case "call_state":
                if let state = object["state"] as? String {
                    self.statusText = self.label(for: state)
                    self.isAISpeaking = state == "speaking"
                }
            case "metrics":
                if let label = object["label"] as? String,
                   let value = object["value_ms"] as? Double {
                    self.metricsText = "\(label) \(Int(value))ms"
                }
            case "emotion_state":
                if let label = object["label"] as? String {
                    self.emotionText = label
                    self.statusText = label
                }
            case "ai_turn_complete":
                self.isAISpeaking = false
                self.statusText = "正在通话"
                if let text = object["text"] as? String {
                    self.transcript.append((false, text))
                    self.onTurnCompleted?(false, text)
                }
            case "error":
                self.errorMessage = object["text"] as? String
            default:
                break
            }
        }
    }

    private func label(for state: String) -> String {
        switch state {
        case "listening":
            return "我在听"
        case "thinking":
            return "正在想怎么回应"
        case "speaking":
            return "正在回应"
        case "interrupted":
            return "已打断，继续听你说"
        default:
            return "正在通话"
        }
    }
}

extension LiveKitCallService: RoomDelegate {
    func roomDidConnect(_ room: Room) {
        DispatchQueue.main.async {
            self.statusText = "已接通"
        }
    }

    func room(_ room: Room, didDisconnect error: LiveKitError?) {
        DispatchQueue.main.async {
            self.isCallActive = false
            self.isConnecting = false
            self.isAISpeaking = false
            self.statusText = "通话结束"
            if let error {
                self.errorMessage = error.localizedDescription
            }
        }
    }

    func room(_ room: Room, participant: RemoteParticipant, didSubscribeTrack publication: RemoteTrackPublication) {
        if publication.track is AudioTrack {
            DispatchQueue.main.async {
                self.isAISpeaking = true
                self.statusText = "AI 正在说话"
            }
        }
    }

    func room(_ room: Room, participant: RemoteParticipant, didUnsubscribeTrack publication: RemoteTrackPublication) {
        DispatchQueue.main.async {
            self.isAISpeaking = false
            self.statusText = "正在通话"
        }
    }

    func room(
        _ room: Room,
        participant: RemoteParticipant?,
        didReceiveData data: Data,
        forTopic topic: String,
        encryptionType: EncryptionType
    ) {
        handleData(data)
    }
}

enum VoiceMateLiveKitError: LocalizedError {
    case invalidTokenResponse

    var errorDescription: String? {
        "无法获取实时通话令牌"
    }
}
