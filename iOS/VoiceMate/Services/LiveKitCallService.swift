import Foundation
import LiveKit
import SwiftUI

/// Manages a real-time voice conversation via LiveKit.
///
/// Replaces the previous WebSocket + AVAudioEngine approach.
/// LiveKit handles: audio capture, playback, VAD, AEC, noise suppression, network transport.
///
/// Flow:
/// 1. User joins a LiveKit room
/// 2. The backend VoiceMate agent (also in the room) processes speech
/// 3. LiveKit pipes bidirectional audio — no manual PCM handling needed
/// 4. The agent detects silence (VAD), generates AI responses, and speaks back
/// 5. Barge-in is handled automatically by the agent tracking user speech while speaking
///
class LiveKitCallService: NSObject, ObservableObject {
    // MARK: - Published State
    @Published var isCallActive = false
    @Published var isAISpeaking = false
    @Published var isUserSpeaking = false
    @Published var currentText: String = ""
    @Published var callDuration: TimeInterval = 0
    @Published var errorMessage: String?
    @Published var transcript: [(isUser: Bool, text: String)] = []
    @Published var pendingUserText: String = ""
    
    // MARK: - Configuration
    private let serverHost: String
    private let serverPort: String
    private let voice: String
    private let persona: String
    private let speed: Double
    
    // MARK: - LiveKit
    private var room: Room?
    private var localParticipant: LocalParticipant?
    
    /// Token endpoint on our backend — returns a LiveKit join token
    private var tokenEndpoint: String {
        "http://\(serverHost):\(serverPort)/v1/livekit/token"
    }
    
    /// WebSocket URL for LiveKit server
    private var liveKitURL: String {
        "ws://\(serverHost):7880"
    }
    
    // MARK: - Timer
    private var durationTimer: Timer?
    
    // MARK: - Callback
    var onTurnCompleted: ((_ isUser: Bool, _ text: String) -> Void)?
    
    init(serverHost: String, serverPort: String, voice: String, persona: String, speed: Double) {
        self.serverHost = serverHost
        self.serverPort = serverPort
        self.voice = voice
        self.persona = persona
        self.speed = speed
        super.init()
    }
    
    // MARK: - Public API
    
    func startCall() {
        Task {
            await joinRoom()
        }
    }
    
    func endCall() {
        durationTimer?.invalidate()
        durationTimer = nil
        
        Task {
            await room?.disconnect()
            room = nil
            localParticipant = nil
        }
        
        DispatchQueue.main.async {
            self.isCallActive = false
            self.isAISpeaking = false
            self.isUserSpeaking = false
            self.callDuration = 0
        }
        
        logger("Call ended")
    }
    
    // MARK: - LiveKit Room
    
    private func joinRoom() async {
        do {
            // 1. Get token from backend
            let token = try await fetchToken()
            
            // 2. Configure LiveKit room
            let room = Room(delegate: self)
            self.room = room
            
            // 3. Configure audio — LiveKit handles mic capture and playback
            // Use default audio configuration; LiveKit manages VAD, AEC, etc.
            let audioOptions = AudioCaptureOptions(
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true
            )
            
            // 4. Connect with room options
            let roomOptions = RoomOptions(
                defaultAudioCaptureOptions: audioOptions,
                // Enable adaptive audio (LiveKit manages bitrate based on network)
                adaptiveStream: true,
                // Subscribe to all tracks automatically
                defaultSubscribeOptions: SubscribeOptions(participantPublished: true)
            )
            
            try await room.connect(
                url: liveKitURL,
                token: token,
                roomOptions: roomOptions
            )
            
            // 5. Publish microphone
            try await room.localParticipant?.setMicrophone(enabled: true)
            
            // Mark the call as active
            DispatchQueue.main.async {
                self.isCallActive = true
                self.startDurationTimer()
            }
            
            logger("Connected to LiveKit room")
            
        } catch {
            logger("Failed to join room: \(error.localizedDescription)")
            DispatchQueue.main.async {
                self.errorMessage = "连接失败: \(error.localizedDescription)"
                self.isCallActive = false
            }
        }
    }
    
    /// Fetch a LiveKit join token from the VoiceMate backend.
    /// The backend generates a token scoped to a unique room for this session.
    private func fetchToken() async throws -> String {
        let url = URL(string: tokenEndpoint)!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        // Send optional config so the backend agent knows our voice/persona preferences
        let body: [String: Any] = [
            "voice": voice,
            "persona": persona,
            "speed": speed,
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.timeoutInterval = 10
        
        let (data, response) = try await URLSession.shared.data(for: request)
        
        guard let httpResponse = response as? HTTPURLResponse,
              httpResponse.statusCode == 200 else {
            throw LiveKitError.invalidTokenResponse
        }
        
        struct TokenResponse: Codable {
            let token: String
            let room: String
        }
        
        let tokenResponse = try JSONDecoder().decode(TokenResponse.self, from: data)
        logger("Got LiveKit token for room: \(tokenResponse.room)")
        return tokenResponse.token
    }
    
    // MARK: - Timer
    
    private func startDurationTimer() {
        callDuration = 0
        durationTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self = self, self.isCallActive else { return }
            DispatchQueue.main.async {
                self.callDuration += 1
            }
        }
    }
    
    // MARK: - Logging
    
    private func logger(_ message: String) {
        print("[LiveKitCall] \(message)")
    }
}

// MARK: - LiveKit RoomDelegate

extension LiveKitCallService: RoomDelegate {
    
    func room(_ room: Room, didConnect isReconnect: Bool) {
        logger("Room connected (reconnect=\(isReconnect))")
    }
    
    func room(_ room: Room, didDisconnect error: LiveKitError?) {
        logger("Room disconnected: \(error?.localizedDescription ?? "unknown")")
        DispatchQueue.main.async {
            self.isCallActive = false
            self.isAISpeaking = false
            self.isUserSpeaking = false
        }
    }
    
    func room(_ room: Room, participantDidConnect participant: RemoteParticipant) {
        logger("Remote participant joined: \(participant.identity ?? "unknown")")
    }
    
    func room(_ room: Room, participantDidDisconnect participant: RemoteParticipant) {
        logger("Remote participant left: \(participant.identity ?? "unknown")")
    }
    
    func room(_ room: Room, participant: RemoteParticipant, didSubscribeToTrack publication: RemoteTrackPublication) {
        logger("Subscribed to track: \(publication.sid)")
        
        if publication.track is AudioTrack {
            // When an audio track from the agent starts playing:
            // the agent is speaking
            DispatchQueue.main.async {
                self.isAISpeaking = true
                self.currentText = "AI 说话中..."
            }
        }
    }
    
    func room(_ room: Room, participant: RemoteParticipant, didUnsubscribeFromTrack publication: RemoteTrackPublication) {
        logger("Unsubscribed from track: \(publication.sid)")
        
        DispatchQueue.main.async {
            self.isAISpeaking = false
            self.currentText = "💬 实时通话中..."
        }
    }
    
    func room(_ room: Room, participant: RemoteParticipant, publication: RemoteTrackPublication, didReceive data: Data) {
        // Handle data messages from the agent (text transcripts, metadata, etc.)
        guard let message = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        
        handleAgentMessage(message)
    }
    
    // MARK: - Local Participant (Microphone) Monitoring
    
    func room(_ room: Room, localParticipant: LocalParticipant, didPublishAudioTrack publication: LocalTrackPublication) {
        logger("Microphone track published")
        
        DispatchQueue.main.async {
            self.isUserSpeaking = false // will update when audio levels change
        }
    }
    
    // MARK: - Agent Message Handling
    
    private func handleAgentMessage(_ message: [String: Any]) {
        guard let type = message["type"] as? String else { return }
        
        DispatchQueue.main.async {
            switch type {
            case "user_transcript":
                // Partial or final ASR of user's speech (from agent-side ASR)
                if let text = message["text"] as? String {
                    self.pendingUserText = text
                    if message["is_final"] as? Bool == true {
                        // User utterance completed
                        self.isUserSpeaking = false
                        let transcriptEntry = (isUser: true, text: text)
                        self.transcript.append(transcriptEntry)
                        self.onTurnCompleted?(true, text)
                        self.pendingUserText = ""
                    } else {
                        self.isUserSpeaking = true
                    }
                }
                
            case "ai_text":
                // AI streaming text
                if let text = message["text"] as? String {
                    self.currentText = text
                }
                
            case "ai_turn_complete":
                // AI finished speaking
                if let text = message["text"] as? String {
                    let transcriptEntry = (isUser: false, text: text)
                    self.transcript.append(transcriptEntry)
                    self.onTurnCompleted?(false, text)
                }
                self.isAISpeaking = false
                self.currentText = "💬 实时通话中..."
                
            case "ai_speaking":
                self.isAISpeaking = true
                self.currentText = "🎙️ AI 说话中..."
                
            case "listening":
                self.isAISpeaking = false
                self.isUserSpeaking = false
                self.currentText = "🎤 正在听你说话..."
                
            case "error":
                if let text = message["text"] as? String {
                    self.errorMessage = text
                }
                
            default:
                break
            }
        }
    }
}

// MARK: - Errors

enum LiveKitError: LocalizedError {
    case invalidTokenResponse
    
    var errorDescription: String? {
        switch self {
        case .invalidTokenResponse:
            return "无法获取 LiveKit 连接令牌，请检查服务器配置"
        }
    }
}
