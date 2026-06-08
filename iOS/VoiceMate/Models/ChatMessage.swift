import Foundation

struct ChatMessage: Identifiable, Codable, Equatable {
    var id: UUID = UUID()
    var role: Role
    var text: String
    var audioURL: String?
    var timestamp: Date = Date()
    var isPlaying: Bool = false

    enum Role: String, Codable {
        case user
        case assistant
    }

    var isUser: Bool { role == .user }
}

struct ChatRequest: Codable {
    let text: String
    let conversationId: String?
    let voice: String?
    let persona: String?
    let speed: Double?

    enum CodingKeys: String, CodingKey {
        case text
        case conversationId = "conversation_id"
        case voice
        case persona
        case speed
    }
}

struct ChatResponse: Codable {
    let replyText: String
    let audioUrl: String
    let conversationId: String
    let durationMs: Int
    let emotion: String?
    let emotionLabel: String?
    let emotionIntensity: Int?
    let need: String?

    enum CodingKeys: String, CodingKey {
        case replyText = "reply_text"
        case audioUrl = "audio_url"
        case conversationId = "conversation_id"
        case durationMs = "duration_ms"
        case emotion
        case emotionLabel = "emotion_label"
        case emotionIntensity = "emotion_intensity"
        case need
    }
}

struct CloneVoiceResponse: Codable {
    let voiceId: String
    let status: String
    let message: String?

    enum CodingKeys: String, CodingKey {
        case voiceId = "voice_id"
        case status
        case message
    }
}

struct CloneStatusResponse: Codable {
    let voiceId: String
    let status: String
    let name: String?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case voiceId = "voice_id"
        case status
        case name
        case createdAt = "created_at"
    }
}

struct CloneVoiceInfo: Codable, Identifiable, Equatable {
    let voiceId: String
    let name: String?
    let status: String
    let createdAt: String?

    var id: String { voiceId }

    enum CodingKeys: String, CodingKey {
        case voiceId = "voice_id"
        case name
        case status
        case createdAt = "created_at"
    }
}

struct CloneVoiceListResponse: Codable {
    let voices: [CloneVoiceInfo]
}

struct LiveKitTokenResponse: Codable {
    let token: String
    let room: String
    let url: String?
}

struct RTCSessionResponse: Codable {
    let provider: String
    let token: String
    let room: String?
    let url: String?
    let appId: String?
    let roomId: String?
    let userId: String?
    let taskId: String?
    let businessId: String?
    let configured: Bool?
    let needs: [String]?

    enum CodingKeys: String, CodingKey {
        case provider
        case token
        case room
        case url
        case appId = "app_id"
        case roomId = "room_id"
        case userId = "user_id"
        case taskId = "task_id"
        case businessId = "business_id"
        case configured
        case needs
    }
}
