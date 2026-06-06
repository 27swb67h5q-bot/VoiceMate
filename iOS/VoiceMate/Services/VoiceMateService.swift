import AVFoundation
import Foundation

@MainActor
final class VoiceMateService: NSObject, ObservableObject {
    @Published var serverHost: String {
        didSet { UserDefaults.standard.set(serverHost, forKey: "server_host") }
    }
    @Published var serverPort: String {
        didSet { UserDefaults.standard.set(serverPort, forKey: "server_port") }
    }
    @Published var selectedVoice: String {
        didSet { UserDefaults.standard.set(selectedVoice, forKey: "selected_voice") }
    }
    @Published var selectedPersona: String {
        didSet { UserDefaults.standard.set(selectedPersona, forKey: "selected_persona") }
    }
    @Published var speechSpeed: Double {
        didSet { UserDefaults.standard.set(speechSpeed, forKey: "speech_speed") }
    }
    @Published var isBusy = false
    @Published var errorMessage: String?

    private var player: AVAudioPlayer?
    private let session: URLSession

    var baseURL: URL {
        URL(string: "http://\(serverHost):\(serverPort)")!
    }

    override init() {
        self.serverHost = UserDefaults.standard.string(forKey: "server_host") ?? "192.168.10.233"
        self.serverPort = UserDefaults.standard.string(forKey: "server_port") ?? "8000"
        let savedVoice = UserDefaults.standard.string(forKey: "selected_voice")
        self.selectedVoice = VoiceMateService.normalizedVoice(savedVoice)
        self.selectedPersona = UserDefaults.standard.string(forKey: "selected_persona") ?? "love"
        let savedSpeed = UserDefaults.standard.double(forKey: "speech_speed")
        self.speechSpeed = savedSpeed == 0 ? 1.0 : savedSpeed
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 120
        self.session = URLSession(configuration: config)
        super.init()
    }

    private static func normalizedVoice(_ voice: String?) -> String {
        guard let voice, voice.hasPrefix("zh_female_") else { return "zh_female_wanqudashu_moon_bigtts" }
        return voice
    }

    func sendMessage(_ text: String, conversationId: String?) async throws -> ChatResponse {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw VoiceMateError.emptyText }

        isBusy = true
        errorMessage = nil
        defer { isBusy = false }

        var request = URLRequest(url: endpoint("v1/chat"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            ChatRequest(
                text: trimmed,
                conversationId: conversationId,
                voice: selectedVoice,
                persona: selectedPersona,
                speed: speechSpeed
            )
        )

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw VoiceMateError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else {
            throw VoiceMateError.serverError(String(data: data, encoding: .utf8) ?? "HTTP \(http.statusCode)")
        }
        return try JSONDecoder().decode(ChatResponse.self, from: data)
    }

    func playAudio(path: String) async throws {
        let url = absoluteAudioURL(path)
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw VoiceMateError.audioPlaybackFailed
        }

        try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: [.duckOthers])
        try AVAudioSession.sharedInstance().setActive(true)
        player = try AVAudioPlayer(data: data)
        player?.prepareToPlay()
        if player?.play() != true {
            throw VoiceMateError.audioPlaybackFailed
        }
    }

    func stopAudio() {
        player?.stop()
        player = nil
    }

    func checkHealth() async -> Bool {
        do {
        let (_, response) = try await session.data(from: endpoint("v1/health"))
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }

    func fetchProactiveText() async -> String? {
        var components = URLComponents(url: endpoint("v1/proactive"), resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "persona", value: selectedPersona)]
        guard let url = components?.url else { return nil }
        guard let (data, _) = try? await session.data(from: url),
              let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return payload["text"] as? String
    }

    func createCloneVoice(fileURLs: [URL]) async throws -> CloneVoiceResponse {
        var request = URLRequest(url: endpoint("v1/clone/upload"))
        request.httpMethod = "POST"
        let boundary = "VoiceMate-\(UUID().uuidString)"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = try multipartBody(files: fileURLs, boundary: boundary)

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw VoiceMateError.serverError(String(data: data, encoding: .utf8) ?? "Clone upload failed")
        }
        return try JSONDecoder().decode(CloneVoiceResponse.self, from: data)
    }

    func listClonedVoices() async throws -> [CloneVoiceInfo] {
        let (data, response) = try await session.data(from: endpoint("v1/clone/voices"))
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw VoiceMateError.invalidResponse
        }
        return try JSONDecoder().decode(CloneVoiceListResponse.self, from: data).voices
    }

    func liveKitToken() async throws -> LiveKitTokenResponse {
        var request = URLRequest(url: endpoint("v1/livekit/token"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "voice": selectedVoice,
            "persona": selectedPersona,
            "speed": speechSpeed,
        ])
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw VoiceMateError.invalidResponse
        }
        return try JSONDecoder().decode(LiveKitTokenResponse.self, from: data)
    }

    func absoluteAudioURL(_ path: String) -> URL {
        if let url = URL(string: path), url.scheme != nil {
            return url
        }
        let normalized = path.hasPrefix("/") ? String(path.dropFirst()) : path
        return baseURL.appendingPathComponent(normalized)
    }

    private func endpoint(_ path: String) -> URL {
        path.split(separator: "/").reduce(baseURL) { url, component in
            url.appendingPathComponent(String(component))
        }
    }

    private func multipartBody(files: [URL], boundary: String) throws -> Data {
        var data = Data()
        for (index, fileURL) in files.enumerated() {
            let name = "files"
            let filename = fileURL.lastPathComponent.isEmpty ? "sample\(index).m4a" : fileURL.lastPathComponent
            data.append("--\(boundary)\r\n")
            data.append("Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\n")
            data.append("Content-Type: audio/m4a\r\n\r\n")
            data.append(try Data(contentsOf: fileURL))
            data.append("\r\n")
        }
        data.append("--\(boundary)--\r\n")
        return data
    }
}

enum VoiceMateError: LocalizedError {
    case emptyText
    case invalidResponse
    case serverError(String)
    case audioPlaybackFailed

    var errorDescription: String? {
        switch self {
        case .emptyText:
            return "消息不能为空"
        case .invalidResponse:
            return "服务器响应异常"
        case .serverError(let message):
            return message
        case .audioPlaybackFailed:
            return "语音播放失败"
        }
    }
}

private extension Data {
    mutating func append(_ string: String) {
        append(string.data(using: .utf8)!)
    }
}
