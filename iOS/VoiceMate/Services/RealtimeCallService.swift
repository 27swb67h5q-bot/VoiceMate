import Foundation
import Speech
import AVFoundation
import Combine

/// Manages a real-time voice call WebSocket connection to the VoiceMate backend.
///
/// Architecture:
/// - iOS records audio via AVAudioEngine, uses on-device SFSpeechRecognizer for ASR
/// - Recognized text is sent as JSON to the backend WebSocket
/// - Backend streams LLM tokens (JSON) and TTS audio (binary PCM16 24kHz)
/// - iOS plays audio chunks immediately for low-latency conversation
class RealtimeCallService: NSObject, ObservableObject {
    // MARK: - Published State
    @Published var isCallActive = false
    @Published var isAISpeaking = false
    @Published var isUserSpeaking = false
    @Published var currentText: String = ""
    @Published var callDuration: TimeInterval = 0
    @Published var errorMessage: String?
    
    // MARK: - Configuration
    private var serverHost: String
    private var serverPort: String
    private var voice: String
    private var persona: String
    private var speed: Double
    private var conversationId: String?
    
    // MARK: - WebSocket
    private var wsTask: URLSessionWebSocketTask?
    private let session: URLSession
    private var pingTimer: Timer?
    private var reconnectTimer: Timer?
    
    // MARK: - Audio Recording (User input)
    private var audioEngine = AVAudioEngine()
    private var inputNode: AVAudioInputNode?
    private let speechRecognizer: SFSpeechRecognizer?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    
    // MARK: - Audio Playback (AI response)
    private var audioPlayer: AVAudioPlayer?
    private var audioBuffer = Data()
    private var isReceivingAudio = false
    
    // Call duration timer
    private var durationTimer: Timer?
    
    // Cancellables
    private var cancellables = Set<AnyCancellable>()
    
    init(serverHost: String, serverPort: String, voice: String, persona: String, speed: Double) {
        self.serverHost = serverHost
        self.serverPort = serverPort
        self.voice = voice
        self.persona = persona
        self.speed = speed
        
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 300
        config.timeoutIntervalForResource = 600
        self.session = URLSession(configuration: config)
        
        self.speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN"))
        
        super.init()
        
        requestPermissions()
    }
    
    deinit {
        endCall()
    }
    
    private func requestPermissions() {
        AVAudioSession.sharedInstance().requestRecordPermission { _ in }
        SFSpeechRecognizer.requestAuthorization { _ in }
    }
    
    // MARK: - Call Lifecycle
    
    func startCall() {
        guard !isCallActive else { return }
        
        isCallActive = true
        errorMessage = nil
        currentText = ""
        callDuration = 0
        
        // Start call duration timer
        durationTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.callDuration += 1
        }
        
        // Connect WebSocket
        connectWebSocket()
        
        // Start recording and ASR
        startRecording()
    }
    
    func endCall() {
        isCallActive = false
        isAISpeaking = false
        isUserSpeaking = false
        
        // Stop recording
        stopRecording()
        
        // Stop playback
        stopPlayback()
        
        // Close WebSocket
        wsTask?.cancel(with: .normalClosure, reason: nil)
        wsTask = nil
        
        // Stop timers
        durationTimer?.invalidate()
        durationTimer = nil
        pingTimer?.invalidate()
        pingTimer = nil
        reconnectTimer?.invalidate()
        reconnectTimer = nil
        
        // Reset audio session
        try? AVAudioSession.sharedInstance().setActive(false)
    }
    
    // MARK: - WebSocket Connection
    
    private func connectWebSocket() {
        let urlStr = "ws://\(serverHost):\(serverPort)/v1/ws/voice"
        guard let url = URL(string: urlStr) else {
            errorMessage = "无效的服务器地址"
            return
        }
        
        wsTask = session.webSocketTask(with: url)
        wsTask?.resume()
        
        // Start ping keepalive
        pingTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.sendPing()
        }
        
        // Start receiving messages
        receiveMessage()
    }
    
    private func reconnect() {
        guard isCallActive else { return }
        
        logger("Reconnecting in 2s...")
        reconnectTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: false) { [weak self] _ in
            self?.connectWebSocket()
        }
    }
    
    private func sendPing() {
        guard isCallActive else { return }
        sendJson(["type": "ping"])
    }
    
    // MARK: - Sending
    
    private func sendJson(_ dict: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              let task = wsTask else {
            return
        }
        task.send(.data(data)) { error in
            if let error = error {
                self.logger("Send error: \(error.localizedDescription)")
            }
        }
    }
    
    /// Send recognized user text to the backend
    private func sendUserText(_ text: String) {
        guard isCallActive, !text.isEmpty else { return }
        
        var payload: [String: Any] = [
            "type": "text",
            "text": text,
            "voice": voice,
            "persona": persona,
            "speed": speed,
        ]
        if let convId = conversationId {
            payload["conversation_id"] = convId
        }
        
        sendJson(payload)
    }
    
    // MARK: - Receiving
    
    private func receiveMessage() {
        wsTask?.receive { [weak self] result in
            guard let self = self else { return }
            
            switch result {
            case .success(let message):
                self.handleMessage(message)
                // Continue receiving
                self.receiveMessage()
                
            case .failure(let error):
                self.logger("WebSocket receive error: \(error.localizedDescription)")
                if self.isCallActive {
                    self.reconnect()
                }
            }
        }
    }
    
    private func handleMessage(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .data(let data):
            // Binary data = PCM audio chunk
            handleAudioData(data)
            
        case .string(let string):
            guard let jsonData = string.data(using: .utf8),
                  let json = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any],
                  let type = json["type"] as? String else {
                return
            }
            
            switch type {
            case "token":
                if let content = json["content"] as? String {
                    DispatchQueue.main.async {
                        self.currentText += content
                    }
                }
                
            case "audio_start":
                isReceivingAudio = true
                audioBuffer = Data()
                DispatchQueue.main.async {
                    self.isAISpeaking = true
                    self.currentText = ""
                }
                
            case "audio_end":
                isReceivingAudio = false
                playAudioBuffer()
                
            case "done":
                if let convId = json["conversation_id"] as? String {
                    conversationId = convId
                }
                DispatchQueue.main.async {
                    self.isAISpeaking = false
                }
                // After AI finishes, start recording again for next turn
                if self.isCallActive {
                    self.startRecording()
                }
                
            case "pong":
                break // Keepalive acknowledged
                
            case "error":
                if let msg = json["message"] as? String {
                    DispatchQueue.main.async {
                        self.errorMessage = msg
                    }
                }
                
            default:
                break
            }
            
        @unknown default:
            break
        }
    }
    
    // MARK: - Audio Playback
    
    private func handleAudioData(_ data: Data) {
        guard isReceivingAudio else { return }
        audioBuffer.append(data)
    }
    
    private func stopPlayback() {
        audioPlayer?.stop()
        audioPlayer = nil
        audioBuffer = Data()
        isReceivingAudio = false
    }
    
    private func playAudioBuffer() {
        guard !audioBuffer.isEmpty else { return }
        
        do {
            // Route to speaker
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .voiceChat)
            try AVAudioSession.sharedInstance().setActive(true)
            
            // Play PCM data as audio
            // Since we're receiving raw PCM16 24kHz mono, we need to wrap it in a WAV header
            // or use AVAudioPlayer with proper format
            let wavData = createWAV(from: audioBuffer, sampleRate: 24000)
            
            self.audioPlayer = try AVAudioPlayer(data: wavData)
            self.audioPlayer?.delegate = self
            self.audioPlayer?.volume = 1.0
            self.audioPlayer?.prepareToPlay()
            self.audioPlayer?.play()
            
        } catch {
            logger("Playback error: \(error.localizedDescription)")
        }
    }
    
    /// Wrap raw PCM16 data in a WAV header so AVAudioPlayer can play it
    private func createWAV(from pcmData: Data, sampleRate: Int) -> Data {
        var wav = Data()
        let numChannels: UInt16 = 1
        let bitsPerSample: UInt16 = 16
        let byteRate = UInt32(sampleRate) * UInt32(numChannels) * UInt32(bitsPerSample) / 8
        let blockAlign = numChannels * bitsPerSample / 8
        let dataSize = UInt32(pcmData.count)
        let fileSize = 36 + dataSize
        
        // RIFF header
        wav.append(contentsOf: [0x52, 0x49, 0x46, 0x46]) // "RIFF"
        wav.append(contentsOf: fileSize.littleEndian.bytes)
        wav.append(contentsOf: [0x57, 0x41, 0x56, 0x45]) // "WAVE"
        
        // fmt chunk
        wav.append(contentsOf: [0x66, 0x6D, 0x74, 0x20]) // "fmt "
        wav.append(contentsOf: UInt32(16).littleEndian.bytes) // chunk size
        wav.append(contentsOf: UInt16(1).littleEndian.bytes) // PCM format
        wav.append(contentsOf: numChannels.littleEndian.bytes)
        wav.append(contentsOf: UInt32(sampleRate).littleEndian.bytes)
        wav.append(contentsOf: byteRate.littleEndian.bytes)
        wav.append(contentsOf: blockAlign.littleEndian.bytes)
        wav.append(contentsOf: bitsPerSample.littleEndian.bytes)
        
        // data chunk
        wav.append(contentsOf: [0x64, 0x61, 0x74, 0x61]) // "data"
        wav.append(contentsOf: dataSize.littleEndian.bytes)
        wav.append(pcmData)
        
        return wav
    }
    
    // MARK: - Audio Recording & ASR
    
    private func startRecording() {
        guard isCallActive, !isAISpeaking else { return }
        guard let speechRecognizer = speechRecognizer, speechRecognizer.isAvailable else {
            logger("Speech recognizer not available")
            return
        }
        
        // Cancel any existing task
        recognitionTask?.cancel()
        recognitionTask = nil
        
        // Configure audio session for recording
        do {
            try AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .voiceChat)
            try AVAudioSession.sharedInstance().setActive(true, options: .notifyOthersOnDeactivation)
        } catch {
            logger("Failed to set audio session: \(error)")
            return
        }
        
        // Setup recognition
        recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
        guard let recognitionRequest = recognitionRequest else { return }
        recognitionRequest.shouldReportPartialResults = true
        
        let inputNode = audioEngine.inputNode
        self.inputNode = inputNode
        
        recognitionTask = speechRecognizer.recognitionTask(with: recognitionRequest) { [weak self] result, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                
                if let result = result {
                    let transcribed = result.bestTranscription.formattedString
                    self.currentText = transcribed
                    self.isUserSpeaking = true
                    
                    // If final, send to backend
                    if result.isFinal && !transcribed.isEmpty {
                        self.isUserSpeaking = false
                        self.sendUserText(transcribed)
                        // Stop recording to avoid duplicate sends
                        self.stopRecording()
                    }
                }
                
                if error != nil {
                    self.isUserSpeaking = false
                }
            }
        }
        
        // Install tap on audio engine
        let recordingFormat = inputNode.outputFormat(forBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { [weak self] buffer, _ in
            self?.recognitionRequest?.append(buffer)
        }
        
        audioEngine.prepare()
        do {
            try audioEngine.start()
            logger("Recording started")
        } catch {
            logger("Failed to start audio engine: \(error)")
        }
    }
    
    private func stopRecording() {
        if audioEngine.isRunning {
            audioEngine.stop()
            audioEngine.inputNode.removeTap(onBus: 0)
        }
        recognitionRequest?.endAudio()
        recognitionRequest = nil
        inputNode = nil
    }
    
    // MARK: - Helpers
    
    private func logger(_ message: String) {
        print("[RealtimeCall] \(message)")
    }
}

// MARK: - AVAudioPlayerDelegate

extension RealtimeCallService: AVAudioPlayerDelegate {
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        DispatchQueue.main.async {
            self.isAISpeaking = false
            // Start recording for next user turn
            if self.isCallActive {
                self.startRecording()
            }
        }
    }
}

// MARK: - LittleEndian bytes helper

extension UInt16 {
    var bytes: [UInt8] {
        withUnsafeBytes(of: self.littleEndian) { Array($0) }
    }
}

extension UInt32 {
    var bytes: [UInt8] {
        withUnsafeBytes(of: self.littleEndian) { Array($0) }
    }
}
