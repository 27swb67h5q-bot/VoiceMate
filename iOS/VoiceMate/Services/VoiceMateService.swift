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
        // Load saved config or use defaults (persists across reboots)
        self.serverHost = UserDefaults.standard.string(forKey: "server_host") ?? "192.168.10.227"
        self.serverPort = UserDefaults.standard.string(forKey: "server_port") ?? "8000"
        self.selectedVoice = UserDefaults.standard.string(forKey: "selected_voice") ?? "zh-CN-XiaoxiaoNeural"
        
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 60
        self.session = URLSession(configuration: config)
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
            voice: selectedVoice
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
        
        var req: [String: Any] = ["text": text, "voice": selectedVoice]
        if let cid = conversationId { req["conversation_id"] = cid }
        let reqData = try JSONSerialization.data(withJSONObject: req)
        try await task.send(URLSessionWebSocketTask.Message.data(reqData))
        
        var fullText = ""
        var audioUrl = ""
        var resultConvId = conversationId ?? ""
        var durationMs = 0
        
        while true {
            let message = try await task.receive()
            switch message {
            case URLSessionWebSocketTask.Message.data(let data):
                if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let type = json["type"] as? String {
                    switch type {
                    case "token":
                        if let content = json["content"] as? String {
                            fullText += content
                            await MainActor.run { [fullText] in streamingText = fullText }
                        }
                    case "done":
                        audioUrl = json["audio_url"] as? String ?? ""
                        resultConvId = json["conversation_id"] as? String ?? resultConvId
                        durationMs = json["duration_ms"] as? Int ?? 0
                        task.cancel(with: .normalClosure, reason: nil)
                        self.wsTask = nil
                        return ChatResponse(replyText: fullText, audioUrl: audioUrl, conversationId: resultConvId, durationMs: durationMs)
                    case "error":
                        throw VoiceMateError.serverError(statusCode: 0, body: json["message"] as? String ?? "WS error")
                    default:
                        break
                    }
                }
            case URLSessionWebSocketTask.Message.string(let string):
                if let data = string.data(using: .utf8),
                   let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                   (json["type"] as? String) == "done" {
                    audioUrl = json["audio_url"] as? String ?? ""
                    resultConvId = json["conversation_id"] as? String ?? resultConvId
                    durationMs = json["duration_ms"] as? Int ?? 0
                    fullText = json["full_text"] as? String ?? fullText
                    task.cancel(with: .normalClosure, reason: nil)
                    self.wsTask = nil
                    return ChatResponse(replyText: fullText, audioUrl: audioUrl, conversationId: resultConvId, durationMs: durationMs)
                }
            @unknown default:
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
