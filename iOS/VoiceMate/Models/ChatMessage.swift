import Foundation

/// Represents a single chat message in the conversation
struct ChatMessage: Identifiable, Codable {
    let id: UUID
    let isUser: Bool          // true = user sent, false = AI replied
    let text: String          // The transcribed text (user) or AI reply text
    let audioURL: String?     // URL to the AI's voice audio (nil for user messages)
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
    
    enum CodingKeys: String, CodingKey {
        case replyText = "reply_text"
        case audioUrl = "audio_url"
        case conversationId = "conversation_id"
        case durationMs = "duration_ms"
    }
}

/// Request body for the chat API
struct ChatRequest: Codable {
    let text: String
    let conversationId: String?
    
    enum CodingKeys: String, CodingKey {
        case text
        case conversationId = "conversation_id"
    }
}
