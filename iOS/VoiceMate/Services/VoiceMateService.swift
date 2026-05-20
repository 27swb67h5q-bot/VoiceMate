import Foundation
import AVFoundation

/// Service that communicates with the VoiceMate backend
class VoiceMateService: ObservableObject {
    // MARK: - Configuration
    @Published var serverHost: String {
        didSet { UserDefaults.standard.set(serverHost, forKey: "server_host") }
    }
    @Published var serverPort: String {
        didSet { UserDefaults.standard.set(serverPort, forKey: "server_port") }
    }
    @Published var selectedVoice: String {
        didSet { UserDefaults.standard.set(selectedVoice, forKey: "selected_voice") }
    }
    @Published var selectedPersona: String = "love" {
        didSet { UserDefaults.standard.set(selectedPersona, forKey: "selected_persona") }
    }
    @Published var speechSpeed: Double = 1.0 {
        didSet { UserDefaults.standard.set(speechSpeed, forKey: "speech_speed") }
    }
    
    private var baseURL: String {
        "http://\(serverHost):\(serverPort)"
    }
    
    // MARK: - State
    @Published var isProcessing = false
    @Published var errorMessage: String?
    
    private var audioPlayer: AVAudioPlayer?
    private let session: URLSession
    @Published var streamingText: String = ""
    private var wsTask: URLSessionWebSocketTask?
    
    init() {
        // Initialize session first (required before accessing any @Published properties)
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 60
        self.session = URLSession(configuration: config)
        
        // Load saved config or use defaults (persists across reboots)
        self.serverHost = UserDefaults.standard.string(forKey: "server_host") ?? "192.168.10.227"
        self.serverPort = UserDefaults.standard.string(forKey: "server_port") ?? "8000"
        self.selectedVoice = UserDefaults.standard.string(forKey: "selected_voice") ?? "zh-CN-XiaoxiaoNeural"
        self.selectedPersona = UserDefaults.standard.string(forKey: "selected_persona") ?? "love"
        self.speechSpeed = UserDefaults.standard.double(forKey: "speech_speed")
        if self.speechSpeed == 0 { self.speechSpeed = 1.0 }
    }
    
    // MARK: - API Calls
    
    /// Send a text message to the AI and get back a voice reply
    func sendMessage(text: String, conversationId: String? = nil) async throws -> ChatResponse {
        await MainActor.run { isProcessing = true }
        defer { Task { @MainActor in isProcessing = false } }
        
        let url = URL(string: "\(baseURL)/v1/chat")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        let body = ChatRequest(
            text: text,
            conversationId: conversationId,
            voice: selectedVoice,
            persona: selectedPersona,
            speed: speechSpeed
        )
        request.httpBody = try JSONEncoder().encode(body)
        
        let (data, response) = try await session.data(for: request)
        
        guard let httpResponse = response as? HTTPURLResponse else {
            throw VoiceMateError.invalidResponse
        }
        
        guard httpResponse.statusCode == 200 else {
            let body = String(data: data, encoding: .utf8) ?? "unknown"
            throw VoiceMateError.serverError(statusCode: httpResponse.statusCode, body: body)
        }
        
        let chatResponse = try JSONDecoder().decode(ChatResponse.self, from: data)
        return chatResponse
    }
    
    /// Get the full URL for an audio file
    func audioURL(for path: String) -> URL? {
        URL(string: "\(baseURL)\(path)")
    }
    
    /// Play an audio file from the server
    func playAudio(from url: URL) async throws {
        let (data, _) = try await session.data(from: url)
        
        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mp3")
        
        try data.write(to: tempURL)
        
        await MainActor.run {
            // Route audio to speaker (not earpiece)
            try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
            try? AVAudioSession.sharedInstance().setActive(true)
            
            self.audioPlayer = try? AVAudioPlayer(contentsOf: tempURL)
            self.audioPlayer?.prepareToPlay()
            self.audioPlayer?.play()
        }
    }
    
    /// Send message via WebSocket streaming (token by token)
    func sendMessageStream(text: String, conversationId: String?) async throws -> ChatResponse {
        await MainActor.run {
            isProcessing = true
            streamingText = ""
        }
        defer { Task { @MainActor in isProcessing = false } }
        
        let wsURL = URL(string: "ws://\(serverHost):\(serverPort)/v1/ws/chat")!
        let task = session.webSocketTask(with: wsURL)
        self.wsTask = task
        task.resume()
        
        var req: [String: Any] = ["text": text, "voice": selectedVoice, "persona": selectedPersona, "speed": speechSpeed]
        if let cid = conversationId { req["conversation_id"] = cid }
        let reqData = try JSONSerialization.data(withJSONObject: req)
        try await task.send(URLSessionWebSocketTask.Message.data(reqData))
        
        var fullText = ""
        var audioUrl = ""
        var resultConvId = conversationId ?? ""
        var durationMs = 0
        
        while true {
            let message: URLSessionWebSocketTask.Message
            do {
                message = try await task.receive()
            } catch {
                // WebSocket connection closed or failed
                self.wsTask = nil
                throw error
            }
            
            let json: [String: Any]
            switch message {
            case .data(let data):
                guard let parsed = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    continue
                }
                json = parsed
            case .string(let string):
                guard let data = string.data(using: .utf8),
                      let parsed = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    continue
                }
                json = parsed
            @unknown default:
                continue
            }
            
            guard let type = json["type"] as? String else { continue }
            
            switch type {
            case "token":
                if let content = json["content"] as? String {
                    fullText += content
                    let text = fullText
                    await MainActor.run { self.streamingText = text }
                }
            case "done":
                audioUrl = json["audio_url"] as? String ?? ""
                resultConvId = json["conversation_id"] as? String ?? resultConvId
                durationMs = json["duration_ms"] as? Int ?? 0
                let emotion = json["emotion"] as? String
                // Prefer full_text if available (string messages), otherwise use accumulated fullText
                if let finalText = json["full_text"] as? String {
                    fullText = finalText
                }
                task.cancel(with: .normalClosure, reason: nil)
                self.wsTask = nil
                return ChatResponse(
                    replyText: fullText,
                    audioUrl: audioUrl,
                    conversationId: resultConvId,
                    durationMs: durationMs,
                    emotion: emotion
                )
            case "error":
                task.cancel(with: .normalClosure, reason: nil)
                self.wsTask = nil
                throw VoiceMateError.serverError(
                    statusCode: 0,
                    body: json["message"] as? String ?? "WebSocket error"
                )
            default:
                break
            }
        }
    }
    
    /// Health check
    func checkHealth() async -> Bool {
        guard let url = URL(string: "\(baseURL)/v1/health") else { return false }
        do {
            let (_, response) = try await session.data(from: url)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}

// MARK: - Errors

enum VoiceMateError: LocalizedError {
    case invalidResponse
    case serverError(statusCode: Int, body: String)
    case audioPlaybackFailed
    
    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "无效的服务器响应"
        case .serverError(let code, let body):
            return "服务器错误 (\(code)): \(body)"
        case .audioPlaybackFailed:
            return "语音播放失败"
        }
    }
}
