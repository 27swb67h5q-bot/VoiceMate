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
    
    private var baseURL: String {
        "http://\(serverHost):\(serverPort)"
    }
    
    // MARK: - State
    @Published var isProcessing = false
    @Published var errorMessage: String?
    
    private var audioPlayer: AVAudioPlayer?
    private let session: URLSession
    
    init() {
        // Load saved config or use defaults
        self.serverHost = UserDefaults.standard.string(forKey: "server_host") ?? "192.168.1.100"
        self.serverPort = UserDefaults.standard.string(forKey: "server_port") ?? "8000"
        
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
        
        let body = ChatRequest(text: text, conversationId: conversationId)
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
        
        // Create a temp file and play it
        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mp3")
        
        try data.write(to: tempURL)
        
        await MainActor.run {
            self.audioPlayer = try? AVAudioPlayer(contentsOf: tempURL)
            self.audioPlayer?.prepareToPlay()
            self.audioPlayer?.play()
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
