import Foundation

/// Represents a single chat message in the conversation
struct ChatMessage: Identifiable, Codable {
    let id: UUID
    let isUser: Bool
    var text: String
    var audioURL: String?
    let timestamp: Date
    var isPlaying: Bool = false
    var duration: TimeInterval = 0
    
    enum CodingKeys: String, CodingKey {
        case id, isUser, text, audioURL, timestamp, duration
    }
}

/// Response from the backend chat API
struct ChatResponse: Codable {
    let replyText: String
    let audioUrl: String
    let conversationId: String
    let durationMs: Int
    let emotion: String?
    
    enum CodingKeys: String, CodingKey {
        case replyText = "reply_text"
        case audioUrl = "audio_url"
        case conversationId = "conversation_id"
        case durationMs = "duration_ms"
        case emotion
    }
}

/// Request body for the chat API
struct ChatRequest: Codable {
    let text: String
    let conversationId: String?
    let voice: String?  // TTS voice name
    let persona: String?  // AI personality
    let speed: Double?  // TTS speed ratio (0.5-2.0, 1.0 = normal)
    
    enum CodingKeys: String, CodingKey {
        case text
        case conversationId = "conversation_id"
        case voice
        case persona
        case speed
    }
}

// MARK: - Voice Clone API Models

/// Response from the voice clone upload endpoint
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

/// Response from the clone status endpoint
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

/// Individual cloned voice info
struct CloneVoiceInfo: Codable, Identifiable {
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

/// Wrapper for the list endpoint
struct CloneVoiceListResponse: Codable {
    let voices: [CloneVoiceInfo]
}
