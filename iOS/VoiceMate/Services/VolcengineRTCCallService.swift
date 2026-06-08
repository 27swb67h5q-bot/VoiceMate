import Foundation
import AVFoundation
import SwiftUI

#if canImport(VolcEngineRTC)
import VolcEngineRTC
#endif

@MainActor
final class VolcengineRTCCallService: NSObject, ObservableObject {
    @Published var isCallActive = false
    @Published var isConnecting = false
    @Published var isAISpeaking = false
    @Published var statusText = "Ready"
    @Published var callDuration: TimeInterval = 0
    @Published var errorMessage: String?
    @Published var transcript: [(isUser: Bool, text: String)] = []
    @Published var metricsText: String?
    @Published var emotionText: String?
    @Published var audioEmotionText: String?
    @Published var moodHintText: String?

    private let serverHost: String
    private let serverPort: String
    private let voice: String
    private let persona: String
    private let speed: Double
    private var liveKitService: LiveKitCallService?
    private var durationTimer: Timer?
    private var activeSession: RTCSessionResponse?

    #if canImport(VolcEngineRTC)
    private var engine: ByteRTCEngine?
    private var room: ByteRTCRoom?
    #endif

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
        statusText = "Connecting..."
        errorMessage = nil
        Task { await connect() }
    }

    func endCall() {
        durationTimer?.invalidate()
        durationTimer = nil
        liveKitService?.endCall()
        liveKitService = nil
        Task { await stopVolcengineCall() }
        isCallActive = false
        isConnecting = false
        isAISpeaking = false
        statusText = "Call ended"
        callDuration = 0
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func connect() async {
        do {
            try configureCallAudioSession()
            let session = try await fetchSession()
            activeSession = session

            if session.provider == "livekit" {
                await startLiveKitFallback()
                return
            }

            guard session.provider == "volcengine" else {
                throw VoiceMateRTCError.unsupportedProvider(session.provider)
            }

            try await startVolcengineCall(session: session)
        } catch {
            isConnecting = false
            isCallActive = false
            statusText = "Connection failed"
            errorMessage = error.localizedDescription
        }
    }

    private func startLiveKitFallback() async {
        let service = LiveKitCallService(
            serverHost: serverHost,
            serverPort: serverPort,
            voice: voice,
            persona: persona,
            speed: speed
        )
        service.onTurnCompleted = onTurnCompleted
        liveKitService = service
        bindLiveKit(service)
        service.startCall()
    }

    private func bindLiveKit(_ service: LiveKitCallService) {
        // LiveKit keeps its own state object; mirror the user-facing fields.
        Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self, weak service] timer in
            Task { @MainActor in
                guard let self, let service, self.liveKitService === service else {
                    timer.invalidate()
                    return
                }
                self.isConnecting = service.isConnecting
                self.isCallActive = service.isCallActive
                self.isAISpeaking = service.isAISpeaking
                self.statusText = service.statusText
                self.callDuration = service.callDuration
                self.errorMessage = service.errorMessage
                self.transcript = service.transcript
                self.metricsText = service.metricsText
                self.emotionText = service.emotionText
                self.audioEmotionText = service.audioEmotionText
                self.moodHintText = service.moodHintText
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
        try session.setPreferredSampleRate(48_000)
        try session.setPreferredIOBufferDuration(0.005)
        try session.setActive(true)
    }

    private func fetchSession() async throws -> RTCSessionResponse {
        let url = URL(string: "http://\(serverHost):\(serverPort)/v1/rtc/session")!
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
            throw VoiceMateRTCError.invalidSessionResponse
        }
        return try JSONDecoder().decode(RTCSessionResponse.self, from: data)
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

    private func setError(_ message: String) {
        isConnecting = false
        isCallActive = false
        statusText = "Connection failed"
        errorMessage = message
    }
}

#if canImport(VolcEngineRTC)
extension VolcengineRTCCallService {
    private func startVolcengineCall(session: RTCSessionResponse) async throws {
        guard let appId = session.appId, !appId.isEmpty,
              let roomId = session.roomId ?? session.room, !roomId.isEmpty,
              let userId = session.userId, !userId.isEmpty else {
            throw VoiceMateRTCError.invalidSessionResponse
        }

        let config = ByteRTCEngineConfig()
        config.appID = appId
        let engine = ByteRTCEngine.createRTCEngine(config, delegate: self)
        guard let engine else {
            throw VoiceMateRTCError.sdkUnavailable
        }
        self.engine = engine

        _ = engine.setBusinessId(session.businessId ?? "voicemate")
        _ = engine.setRuntimeParameters([
            "rtc.audio.enable_aec": true,
            "rtc.audio.enable_agc": true,
            "rtc.audio.enable_ans": true,
            "rtc.audio.low_latency": true
        ])

        guard let room = engine.createRTCRoom(roomId) else {
            throw VoiceMateRTCError.roomCreateFailed
        }
        self.room = room
        _ = room.setRTCRoomDelegate(self)

        let userInfo = ByteRTCUserInfo()
        userInfo.userId = userId

        let roomConfig = ByteRTCRoomConfig()
        roomConfig.profile = .chatRoom
        roomConfig.isPublishAudio = true
        roomConfig.isPublishVideo = false
        roomConfig.isAutoSubscribeAudio = true
        roomConfig.isAutoSubscribeVideo = false

        let result = room.joinRoom(session.token, userInfo: userInfo, userVisibility: true, roomConfig: roomConfig)
        guard result == 0 else {
            throw VoiceMateRTCError.joinFailed(result)
        }

        _ = engine.startAudioCapture()
        _ = room.publishStreamAudio(true)

        isConnecting = false
        isCallActive = true
        statusText = "In call"
        startTimer()
    }

    private func stopVolcengineCall() async {
        room?.publishStreamAudio(false)
        room?.leaveRoom()
        room?.destroy()
        room = nil
        engine?.stopAudioCapture()
        if let engine {
            ByteRTCEngine.destroyRTCEngineMulti(engine)
        }
        engine = nil
        activeSession = nil
    }
}

extension VolcengineRTCCallService: ByteRTCEngineDelegate {
    nonisolated func rtcEngine(_ engine: ByteRTCEngine, onError errorCode: ByteRTCErrorCode) {
        Task { @MainActor in
            self.setError("Volcengine RTC error: \(errorCode.rawValue)")
        }
    }

    nonisolated func rtcEngine(_ engine: ByteRTCEngine, onWarning code: ByteRTCWarningCode) {
        Task { @MainActor in
            self.metricsText = "RTC warning \(code.rawValue)"
        }
    }

    nonisolated func rtcEngine(_ engine: ByteRTCEngine, onFirstRemoteAudioFrame streamKey: ByteRTCRemoteStreamKey) {
        Task { @MainActor in
            self.isAISpeaking = true
            self.statusText = "AI speaking"
        }
    }
}

extension VolcengineRTCCallService: ByteRTCRoomDelegate {
    nonisolated func rtcRoom(
        _ rtcRoom: ByteRTCRoom,
        onRoomStateChanged roomId: String,
        withUid uid: String,
        state: Int,
        extraInfo: String
    ) {
        Task { @MainActor in
            if state == 0 {
                self.statusText = "Connected"
            } else {
                self.setError("Volcengine RTC join failed: \(state)")
            }
        }
    }

    nonisolated func rtcRoom(
        _ rtcRoom: ByteRTCRoom,
        onUserPublishStreamAudio userId: String,
        uid: String,
        isPublish: Bool
    ) {
        Task { @MainActor in
            self.isAISpeaking = isPublish
            self.statusText = isPublish ? "AI speaking" : "In call"
        }
    }

    nonisolated func rtcRoom(
        _ rtcRoom: ByteRTCRoom,
        onAudioPublishStateChanged roomId: String,
        userId uid: String,
        state: ByteRTCPublishState,
        reason: ByteRTCPublishStateChangeReason
    ) {
        Task { @MainActor in
            self.metricsText = "mic \(state.rawValue)/\(reason.rawValue)"
        }
    }

    nonisolated func rtcRoom(
        _ rtcRoom: ByteRTCRoom,
        onLeaveRoom stats: ByteRTCRoomStats
    ) {
        Task { @MainActor in
            self.isCallActive = false
            self.isAISpeaking = false
            self.statusText = "Call ended"
        }
    }
}
#else
extension VolcengineRTCCallService {
    private func startVolcengineCall(session: RTCSessionResponse) async throws {
        throw VoiceMateRTCError.sdkUnavailable
    }

    private func stopVolcengineCall() async {
        activeSession = nil
    }
}
#endif

enum VoiceMateRTCError: LocalizedError {
    case invalidSessionResponse
    case unsupportedProvider(String)
    case sdkUnavailable
    case roomCreateFailed
    case joinFailed(Int32)

    var errorDescription: String? {
        switch self {
        case .invalidSessionResponse:
            return "Unable to get realtime call session"
        case .unsupportedProvider(let provider):
            return "Unsupported realtime provider: \(provider)"
        case .sdkUnavailable:
            return "Volcengine RTC SDK is not linked into this build"
        case .roomCreateFailed:
            return "Volcengine RTC room creation failed"
        case .joinFailed(let code):
            return "Volcengine RTC join failed: \(code)"
        }
    }
}
