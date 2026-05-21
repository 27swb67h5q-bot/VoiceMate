import Foundation
import AVFoundation
import os.log

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
    private let logger = Logger(subsystem: "com.voicemate", category: "WebSocket")
    
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
    
    /// Play an audio file — uses local cache if available, otherwise downloads + caches
    func playAudio(from url: URL, remotePath: String? = nil) async throws {
        let localURL: URL
        
        if let path = remotePath, AudioCache.isCached(remotePath: path) {
            // Play from local cache
            localURL = AudioCache.localURL(for: path)
        } else if let path = remotePath {
            // Download and cache
            localURL = try await AudioCache.cache(from: url, remotePath: path)
        } else {
            // No remotePath given — download to temp (legacy path)
            let (data, _) = try await session.data(from: url)
            let ext = url.lastPathComponent.hasSuffix(".wav") ? "wav" : "mp3"
            localURL = FileManager.default.temporaryDirectory
                .appendingPathComponent(UUID().uuidString)
                .appendingPathExtension(ext)
            try data.write(to: localURL)
        }
        
        await MainActor.run {
            // Route audio to speaker (not earpiece)
            try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
            try? AVAudioSession.sharedInstance().setActive(true)
            
            self.audioPlayer = try? AVAudioPlayer(contentsOf: localURL)
            self.audioPlayer?.prepareToPlay()
            self.audioPlayer?.play()
        }
    }
    
    /// Send message via WebSocket streaming (token by token)
    func sendMessageStream(text: String, conversationId: String?) async throws -> ChatResponse {
        // Retry loop: try WebSocket up to 2 times, then fall back to REST
        let maxRetries = 2
        
    wsRetryLoop:
        for attempt in 0..<maxRetries {
            await MainActor.run {
                isProcessing = true
                streamingText = ""
            }
            
            let wsURL = URL(string: "ws://\(serverHost):\(serverPort)/v1/ws/chat")!
            let task = session.webSocketTask(with: wsURL)
            self.wsTask = task
            task.resume()
            
            var req: [String: Any] = ["text": text, "voice": selectedVoice, "persona": selectedPersona, "speed": speechSpeed]
            if let cid = conversationId { req["conversation_id"] = cid }
            let reqData = try JSONSerialization.data(withJSONObject: req)
            
            do {
                try await task.send(URLSessionWebSocketTask.Message.data(reqData))
            } catch {
                // Send failed — retry or fall back
                self.wsTask = nil
                if attempt < maxRetries - 1 {
                    print("[VoiceMate] WS send failed (attempt \(attempt+1)/\(maxRetries)), retrying...")
                    try? await Task.sleep(nanoseconds: 1_000_000_000)  // 1s delay before retry
                    continue wsRetryLoop
                }
                // Last attempt failed — fall back to REST
                print("[VoiceMate] WS send failed after \(maxRetries) attempts, falling back to REST")
                defer { Task { @MainActor in isProcessing = false } }
                return try await restChat(text: text, conversationId: conversationId)
            }
            
            var fullText = ""
            var audioUrl = ""
            var resultConvId = conversationId ?? ""
            var durationMs = 0
            var receivedDone = false
            
            // Start a heartbeat timer to ping the connection status
            let heartbeatTask = Task {
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 15_000_000_000)  // 15s interval
                    if Task.isCancelled { break }
                    // Send a lightweight ping by checking the connection
                    task.sendPing { _ in }
                }
            }
            
            defer {
                heartbeatTask.cancel()
            }
            
            while !receivedDone {
                let message: URLSessionWebSocketTask.Message
                do {
                    // Race receive() against a 30s timeout
                    let wrappedReceive = Task { () -> URLSessionWebSocketTask.Message in
                        try await task.receive()
                    }
                    let wrappedTimeout = Task { () throws -> URLSessionWebSocketTask.Message in
                        try await Task.sleep(nanoseconds: 30_000_000_000)
                        wrappedReceive.cancel()
                        throw VoiceMateError.serverError(statusCode: 0, body: "WebSocket receive timed out")
                    }
                    
                    message = try await wrappedReceive.value
                    wrappedTimeout.cancel()
                    
                } catch {
                    self.wsTask = nil
                    if error is CancellationError {
                        // Receive timed out after 30s — retry if attempts remain
                        if attempt < maxRetries - 1 {
                            print("[VoiceMate] WS receive timed out (attempt \(attempt+1)/\(maxRetries)), reconnecting...")
                            try? await Task.sleep(nanoseconds: 1_000_000_000)
                            continue wsRetryLoop
                        }
                        throw error
                    }
                    // Connection lost — retry or fall back
                    if attempt < maxRetries - 1 {
                        print("[VoiceMate] WS connection lost (attempt \(attempt+1)/\(maxRetries)), reconnecting...")
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                        continue wsRetryLoop
                    }
                    // All retries exhausted — fall back to REST
                    print("[VoiceMate] WS failed after \(maxRetries) attempts, falling back to REST")
                    defer { Task { @MainActor in isProcessing = false } }
                    return try await restChat(text: text, conversationId: conversationId)
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
                case "ping":
                    // Server-side keepalive, nothing to do
                    break
                case "done":
                    audioUrl = json["audio_url"] as? String ?? ""
                    resultConvId = json["conversation_id"] as? String ?? resultConvId
                    durationMs = json["duration_ms"] as? Int ?? 0
                    let emotion = json["emotion"] as? String
                    if let finalText = json["full_text"] as? String {
                        fullText = finalText
                    }
                    task.cancel(with: .normalClosure, reason: nil)
                    self.wsTask = nil
                    receivedDone = true
                    await MainActor.run { self.isProcessing = false }
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
                    await MainActor.run { self.isProcessing = false }
                    throw VoiceMateError.serverError(
                        statusCode: 0,
                        body: json["message"] as? String ?? "WebSocket error"
                    )
                default:
                    break
                }
            }
        }
        
        // Fallback: call REST API
        print("[VoiceMate] WS streaming unavailable, using REST fallback")
        defer { Task { @MainActor in isProcessing = false } }
        return try await restChat(text: text, conversationId: conversationId)
    }
    
    /// Fallback: send message via REST API when WebSocket is unavailable
    private func restChat(text: String, conversationId: String?) async throws -> ChatResponse {
        let url = URL(string: "http://\(serverHost):\(serverPort)/v1/chat")!
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
    
    // MARK: - Voice Clone API
    
    /// Upload recorded voice samples and create a cloned voice.
    /// - Parameter fileURLs: Array of local file URLs for the recorded samples (5 phrases).
    /// - Returns: Voice clone response with voice_id and status.
    func createCloneVoice(fileURLs: [URL]) async throws -> CloneVoiceResponse {
        await MainActor.run { isProcessing = true }
        defer { Task { @MainActor in isProcessing = false } }
        
        let url = URL(string: "\(baseURL)/v1/clone/upload")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        
        let boundary = UUID().uuidString
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        
        var bodyData = Data()
        for fileURL in fileURLs {
            let fileData = try Data(contentsOf: fileURL)
            bodyData.append("--\(boundary)\r\n".data(using: .utf8)!)
            bodyData.append("Content-Disposition: form-data; name=\"files\"; filename=\"\(fileURL.lastPathComponent)\"\r\n".data(using: .utf8)!)
            bodyData.append("Content-Type: audio/m4a\r\n\r\n".data(using: .utf8)!)
            bodyData.append(fileData)
            bodyData.append("\r\n".data(using: .utf8)!)
        }
        bodyData.append("--\(boundary)--\r\n".data(using: .utf8)!)
        request.httpBody = bodyData
        
        let (data, response) = try await session.data(for: request)
        
        guard let httpResponse = response as? HTTPURLResponse else {
            throw VoiceMateError.invalidResponse
        }
        guard httpResponse.statusCode == 200 else {
            let body = String(data: data, encoding: .utf8) ?? "unknown"
            throw VoiceMateError.serverError(statusCode: httpResponse.statusCode, body: body)
        }
        
        let result = try JSONDecoder().decode(CloneVoiceResponse.self, from: data)
        return result
    }
    
    /// Check the status of a voice clone training job.
    func checkCloneStatus(voiceId: String) async throws -> CloneStatusResponse {
        let url = URL(string: "\(baseURL)/v1/clone/status/\(voiceId)")!
        let (data, response) = try await session.data(from: url)
        
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw VoiceMateError.invalidResponse
        }
        return try JSONDecoder().decode(CloneStatusResponse.self, from: data)
    }
    
    /// List all cloned voices from the server.
    func listClonedVoices() async throws -> [CloneVoiceInfo] {
        let url = URL(string: "\(baseURL)/v1/clone/voices")!
        let (data, _) = try await session.data(from: url)
        let wrapper = try JSONDecoder().decode(CloneVoiceListResponse.self, from: data)
        return wrapper.voices
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
