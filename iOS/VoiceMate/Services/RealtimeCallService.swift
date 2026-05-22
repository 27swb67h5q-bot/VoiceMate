import Foundation
import AVFoundation
import Accelerate
import Speech

/// Manages a true full-duplex real-time voice conversation with the VoiceMate backend.
///
/// Architecture (Full-Duplex):
/// ┌──────────────────────────────────────────────────────────────┐
/// │  iOS                                                        │
/// │  ┌──────────┐    PCM16 16kHz chunks    ┌───────────────┐   │
/// │  │  Mic     │ ──────────────────────────▶  WebSocket    │   │
/// │  │  (Always │    (binary frames, 50ms)  │  Client       │   │
/// │  │   Open)  │                          └───────┬───────┘   │
/// │  └──────────┘                                  │           │
/// │                                                 │           │
/// │  ┌──────────┐    PCM16 24kHz chunks    ┌───────▼───────┐   │
/// │  │  Speaker │ ◀──────────────────────────│  WebSocket    │   │
/// │  │  (Stream │    (binary frames, 50ms)   │  Client       │   │
/// │  │   Play)  │                           └───────────────┘   │
/// │  └──────────┘                                              │
/// │                                                            │
/// │  Features:                                                 │
/// │  • Mic always open — no push-to-talk                      │
/// │  • Server-side VAD detects silence → triggers response     │
/// │  • Barge-in: user speaks during AI reply → interrupt TTS   │
/// │  • Streaming audio playback — no waiting for full TTS      │
/// └──────────────────────────────────────────────────────────────┘
///
class RealtimeCallService: NSObject, ObservableObject {
    // MARK: - Published State
    @Published var isCallActive = false
    @Published var isAISpeaking = false
    @Published var isUserSpeaking = false
    @Published var currentText: String = ""
    @Published var aiText: String = ""
    @Published var callDuration: TimeInterval = 0
    @Published var errorMessage: String?
    /// Transcript collected during the call (user + AI message pairs)
    @Published var transcript: [(isUser: Bool, text: String)] = []
    /// Current user utterance being built by on-device ASR
    @Published var pendingUserText: String = ""
    /// Lock flag to avoid sending duplicate text messages
    private var isProcessingUtterance = false
    private var lastSentUserText: String = ""
    /// Accumulated utterance text from local ASR for the current turn
    private var currentUtteranceText: String = ""
    /// Set to true once ASR produces first partial result
    private var hasASRStarted = false
    
    // MARK: - Configuration
    private var serverHost: String
    private var serverPort: String
    private var voice: String
    private var persona: String
    private var speed: Double
    
    // MARK: - WebSocket
    private var webSocket: URLSessionWebSocketTask?
    private let urlSession: URLSession
    private var pingTimer: Timer?
    private var reconnectTimer: Timer?
    
    // MARK: - Audio Recording (Always-on mic)
    private let audioEngine = AVAudioEngine()
    private let bus = 0
    /// Queue for sending raw PCM data to WebSocket
    private let audioSendQueue = DispatchQueue(label: "com.voicemate.audioSend", qos: .userInitiated)
    /// Format: 16kHz mono PCM16
    private var recordingFormat: AVAudioFormat?
    
    // MARK: - Audio Playback (Streaming)
    private var audioPlayerNode: AVAudioPlayerNode?
    private var audioEngineP: AVAudioEngine?  // playback-only engine

    /// Callback invoked per-turn when a user utterance or AI reply completes
    var onTurnCompleted: ((_ isUser: Bool, _ text: String) -> Void)?

    private var playbackFormat: AVAudioFormat?
    private var audioSampleRate: Double = 24000
    
    // MARK: - VAD (Simple energy-based for client-side barge-in)
    private let vadThreshold: Float = 0.015  // normalized RMS threshold
    private var isMicActive = false
    
    // Call duration timer
    private var durationTimer: Timer?
    private var durationStartTime: Date?
    
    // MARK: - On-Device Speech Recognition (SFSpeechRecognizer)
    private var speechRecognizer: SFSpeechRecognizer?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    
    // MARK: - Init
    
    init(serverHost: String, serverPort: String, voice: String, persona: String, speed: Double) {
        self.serverHost = serverHost
        self.serverPort = serverPort
        self.voice = voice
        self.persona = persona
        self.speed = speed
        
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 300
        config.timeoutIntervalForResource = 600
        self.urlSession = URLSession(configuration: config)
        
        super.init()
        requestPermissions()
        setupSpeechRecognizer()
    }
    
    deinit {
        endCall()
    }
    
    private func setupSpeechRecognizer() {
        self.speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN"))
        // Pre-create the recognition request so startAudioCapture() can feed it buffers
        self.recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
        self.recognitionRequest?.shouldReportPartialResults = true
    }
    
    private func requestPermissions() {
        AVAudioSession.sharedInstance().requestRecordPermission { _ in }
        SFSpeechRecognizer.requestAuthorization { status in
            print("[RealtimeCall] Speech recognition auth: \(status.rawValue)")
        }
    }
    
    // MARK: - Call Lifecycle
    
    func startCall() {
        guard !isCallActive else { return }
        
        isCallActive = true
        errorMessage = nil
        currentText = ""
        aiText = ""
        callDuration = 0
        
        // Start call duration timer
        durationTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.callDuration += 1
        }
        
        // Start WebSocket
        connectWebSocket()
        
        // Start always-on mic
        startAudioCapture()
    }
    
    func endCall() {
        isCallActive = false
        isAISpeaking = false
        isUserSpeaking = false
        isMicActive = false
        
        stopAudioCapture()
        stopASR()
        stopAudioPlayback()
        disconnectWebSocket()
        
        durationTimer?.invalidate()
        durationTimer = nil
        pingTimer?.invalidate()
        pingTimer = nil
        reconnectTimer?.invalidate()
        reconnectTimer = nil
        
        try? AVAudioSession.sharedInstance().setActive(false)
        
        logger("Call ended. Transcript count: \(transcript.count)")
        // transcript is kept so the view can read it after endCall
    }
    
    // MARK: - WebSocket Connection
    
    private func connectWebSocket() {
        let cleanHost = serverHost.trimmingCharacters(in: .whitespaces)
        let cleanPort = serverPort.trimmingCharacters(in: .whitespaces)
        let urlStr = "ws://\(cleanHost):\(cleanPort)/v1/ws/voice"
        
        guard let url = URL(string: urlStr) else {
            errorMessage = "Invalid server address"
            return
        }
        
        var request = URLRequest(url: url)
        request.timeoutInterval = 300
        
        webSocket = urlSession.webSocketTask(with: request)
        webSocket?.resume()
        
        // Send initial config
        sendJson([
            "type": "config",
            "voice": voice,
            "persona": persona,
            "speed": speed,
        ])
        
        // Keepalive ping
        pingTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.sendPing()
        }
        
        // Start receiving
        receiveMessage()
    }
    
    private func disconnectWebSocket() {
        webSocket?.cancel(with: .normalClosure, reason: nil)
        webSocket = nil
    }
    
    private func reconnect() {
        guard isCallActive else { return }
        logger("Reconnecting in 2s...")
        connectWebSocket()
    }
    
    private func sendPing() {
        guard isCallActive else { return }
        sendJson(["type": "ping"])
    }
    
    // MARK: - Send
    
    private func sendJson(_ dict: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              let jsonStr = String(data: data, encoding: .utf8) else { return }
        
        webSocket?.send(.string(jsonStr)) { [weak self] error in
            if let error = error {
                self?.logger("Send JSON error: \(error)")
            }
        }
    }
    
    private func sendAudioChunk(_ pcmData: Data) {
        guard isCallActive, let ws = webSocket, [URLSessionTask.State.running, URLSessionTask.State.suspended].contains(ws.state) else { return }
        
        audioSendQueue.async { [weak self] in
            guard let self = self else { return }
            let semaphore = DispatchSemaphore(value: 0)
            ws.send(.data(pcmData)) { error in
                if let error = error {
                    self.logger("Send audio chunk error: \(error)")
                }
                semaphore.signal()
            }
            _ = semaphore.wait(timeout: .now() + 1.0)
        }
    }
    
    // MARK: - Receive
    
    private func receiveMessage() {
        guard let ws = webSocket else { return }
        
        ws.receive { [weak self] result in
            guard let self = self else { return }
            
            switch result {
            case .success(let message):
                switch message {
                case .string(let text):
                    self.handleTextMessage(text)
                case .data(let data):
                    self.handleAudioData(data)
                @unknown default:
                    break
                }
                // Continue receiving
                self.receiveMessage()
                
            case .failure(let error):
                self.logger("WebSocket receive error: \(error)")
                // Don't reconnect on normal closure
                if let wsError = error as? URLError, wsError.code == .cancelled {
                    return
                }
                if self.isCallActive {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                        self.reconnect()
                    }
                }
            }
        }
    }
    
    private func handleTextMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        
        let type = json["type"] as? String ?? ""
        
        DispatchQueue.main.async {
            switch type {
            case "pong":
                break
            
            case "connected":
                self.logger("Server acknowledged connection")
            
            case "asr_partial":
                if let content = json["text"] as? String {
                    self.currentText = content
                    self.isUserSpeaking = true
                    self.hasASRStarted = true
                }
            
            case "asr_final":
                // Server detected utterance end via VAD.
                // Use local on-device ASR text, or wait for it briefly.
                if let content = json["text"] as? String {
                    self.currentText = content
                }
                self.handleUtteranceEnd()
            
            case "token":
                if let content = json["content"] as? String {
                    self.aiText += content
                }
            
            case "audio_start":
                self.isAISpeaking = true
                self.isUserSpeaking = false
                if !self.aiText.isEmpty {
                    self.transcript.append((isUser: false, text: self.aiText))
                }
                self.aiText = ""
                if let sr = json["sample_rate"] as? Double {
                    self.audioSampleRate = sr
                }
                self.setupAudioPlayback()
            
            case "audio_end":
                self.onAudioEnd()
            
            case "turn_done":
                self.handleTurnDone()
            
            case "interrupted":
                self.isAISpeaking = false
                self.stopAudioPlayback()
            
            case "timeout":
                self.errorMessage = "连接超时"
                self.endCall()
            
            case "error":
                if let msg = json["message"] as? String {
                    self.errorMessage = msg
                }
            
            default:
                break
            }
        }
    }
    
    // MARK: - Utterance & Turn Handling
    
    private func handleUtteranceEnd() {
        guard !isProcessingUtterance else {
            logger("handleUtteranceEnd: already processing, skipping")
            return
        }
        
        isProcessingUtterance = true
        
        let localText = pendingUserText.trimmingCharacters(in: .whitespacesAndNewlines)
        let accumulatedText = currentUtteranceText.trimmingCharacters(in: .whitespacesAndNewlines)
        
        if !localText.isEmpty {
            sendUtteranceText(localText)
        } else if !accumulatedText.isEmpty {
            sendUtteranceText(accumulatedText)
        } else if hasASRStarted {
            // ASR has started but may not have produced results yet — brief wait
            logger("ASR text not ready, waiting briefly...")
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                guard let self = self, self.isProcessingUtterance else { return }
                let text = self.pendingUserText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty {
                    self.sendUtteranceText(text)
                } else {
                    self.logger("Sending acknowledgment fallback")
                    self.sendUtteranceText("\u{55ef}")
                }
            }
        } else {
            // ASR hasn't started at all — send acknowledgment to keep conversation flowing
            logger("ASR not started yet, sending acknowledgment")
            sendUtteranceText("\u{55ef}")
        }
    }
    
    private func sendUtteranceText(_ text: String) {
        logger("Sending utterance text: \(text.prefix(60))")
        lastSentUserText = text
        currentUtteranceText = text
        
        transcript.append((isUser: true, text: text))
        onTurnCompleted?(true, text)
        
        sendJson([
            "type": "text",
            "text": text,
            "voice": voice,
            "persona": persona,
            "speed": speed,
        ])
        
        pendingUserText = ""
        currentUtteranceText = ""
        hasASRStarted = false
        
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            self?.isProcessingUtterance = false
        }
    }
    
    private func handleTurnDone() {
        if !aiText.isEmpty {
            transcript.append((isUser: false, text: aiText))
            onTurnCompleted?(false, aiText)
        }
        
        isAISpeaking = false
        currentText = ""
        aiText = ""
        currentUtteranceText = ""
        hasASRStarted = false
        isProcessingUtterance = false
        pendingUserText = ""
        lastSentUserText = ""
        logger("Turn completed, ready for next utterance")
    }
    
    private func onAudioEnd() {
        logger("Audio stream ended, waiting for turn_done")
    }
    

    // MARK: - On-Device Speech Recognition

    private func startASR() {
        recognitionTask?.cancel()
        recognitionTask = nil
        
        guard let recognizer = speechRecognizer, recognizer.isAvailable else {
            logger("ASR: speech recognizer not available")
            return
        }
        
        // Create fresh recognition request for each call session
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        recognitionRequest = request
        
        hasASRStarted = false
        currentUtteranceText = ""
        
        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            DispatchQueue.main.async {
                if let result = result {
                    let text = result.bestTranscription.formattedString
                    self?.pendingUserText = text
                    self?.currentText = text
                    self?.hasASRStarted = true
                    self?.currentUtteranceText = text
                }
                if let error = error {
                    self?.logger("ASR error: \(error.localizedDescription)")
                }
            }
        }
        
        logger("On-device ASR started")
    }

    private func stopASR() {
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest?.endAudio()
        recognitionRequest = nil
        hasASRStarted = false
        currentUtteranceText = ""
        logger("On-device ASR stopped and cleaned up")
    }

    private func handleAudioData(_ data: Data) {
        // PCM16 audio chunks from server — play immediately
        guard let playerNode = audioPlayerNode, let engine = audioEngineP, engine.isRunning else {
            return
        }
        
        guard let format = playbackFormat else { return }
        
        let frameLength = data.count / 2  // 16-bit samples
        guard frameLength > 0 else { return }
        
        guard let pcmBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frameLength)) else {
            return
        }
        pcmBuffer.frameLength = AVAudioFrameCount(frameLength)
        
        // Copy PCM16 data into float buffer
        data.withUnsafeBytes { (rawPtr: UnsafeRawBufferPointer) in
            guard let srcPtr = rawPtr.baseAddress?.assumingMemoryBound(to: Int16.self) else { return }
            guard let dstPtr = pcmBuffer.floatChannelData?[0] else { return }
            
            // Convert Int16 to Float32 with scaling
            var buffer = [Int16](repeating: 0, count: frameLength)
            memcpy(&buffer, srcPtr, data.count)
            
            var floatBuffer = [Float](repeating: 0, count: frameLength)
            vDSP_vflt16(buffer, 1, &floatBuffer, 1, vDSP_Length(frameLength))
            var scale = Float(Int16.max)
            vDSP_vsdiv(floatBuffer, 1, &scale, &floatBuffer, 1, vDSP_Length(frameLength))
            
            memcpy(dstPtr, floatBuffer, frameLength * MemoryLayout<Float>.size)
        }
        
        playerNode.scheduleBuffer(pcmBuffer) {
            // Buffer played — do nothing special
        }
    }
    
    // MARK: - Audio Capture (Always-on Mic)
    
    private func startAudioCapture() {
        let audioSession = AVAudioSession.sharedInstance()
        do {
            // PlayAndRecord is required for full-duplex
            try audioSession.setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetoothHFP, .defaultToSpeaker])
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)
        } catch {
            logger("Failed to set audio session: \(error)")
            errorMessage = "麦克风初始化失败"
            return
        }
        
        let inputNode = audioEngine.inputNode
        let nodeFormat = inputNode.outputFormat(forBus: bus)
        
        // We want 16kHz mono PCM16 for bandwidth efficiency
        // If hardware supports it, request 16kHz; otherwise use hardware rate and resample
        let targetSampleRate: Double = 16000
        let targetFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                         sampleRate: targetSampleRate,
                                         channels: 1,
                                         interleaved: false)!
        
        recordingFormat = targetFormat
        
        // Install tap with hardware format, convert to 16kHz manually
        inputNode.installTap(onBus: bus, bufferSize: 1024, format: nodeFormat) { [weak self] buffer, _ in
            guard let self = self, self.isCallActive else { return }
            
            // Convert buffer to 16kHz mono PCM16
            let convertedData = self.convertToPCM16(buffer: buffer, targetSampleRate: targetSampleRate)
            if !convertedData.isEmpty {
                self.sendAudioChunk(convertedData)
                
                // Also feed the original buffer to on-device ASR
                self.recognitionRequest?.append(buffer)
                
                // Quick VAD for UI state
                let rms = self.calculateRMS(from: buffer)
                DispatchQueue.main.async {
                    self.isUserSpeaking = rms > self.vadThreshold
                }
                
                // Client-side barge-in detection
                if rms > self.vadThreshold && self.isAISpeaking {
                    self.sendJson(["type": "barge_in"])
                }
            }
        }
        
        audioEngine.prepare()
        do {
            try audioEngine.start()
            isMicActive = true
            logger("Audio capture started (16kHz)")
        } catch {
            logger("Failed to start audio engine: \(error)")
            errorMessage = "麦克风启动失败"
        }
        // Start on-device ASR alongside mic capture
        startASR()
    }
    
    private func stopAudioCapture() {
        if audioEngine.isRunning {
            audioEngine.stop()
            audioEngine.inputNode.removeTap(onBus: bus)
        }
        isMicActive = false
    }
    
    /// Converts an AVAudioPCMBuffer to PCM16 16kHz mono Data
    private func convertToPCM16(buffer: AVAudioPCMBuffer, targetSampleRate: Double) -> Data {
        guard let channelData = buffer.floatChannelData else { return Data() }
        let srcSampleRate = buffer.format.sampleRate
        let srcChannels = Int(buffer.format.channelCount)
        let srcFrames = Int(buffer.frameLength)
        
        guard srcFrames > 0 else { return Data() }
        
        // Downmix to mono if needed
        let monoFloats: [Float]
        if srcChannels > 1 {
            // Average channels
            var mono = [Float](repeating: 0, count: srcFrames)
            for ch in 0..<srcChannels {
                let chData = channelData[ch]
                for i in 0..<srcFrames {
                    mono[i] += chData[i] / Float(srcChannels)
                }
            }
            monoFloats = mono
        } else {
            monoFloats = Array(UnsafeBufferPointer(start: channelData[0], count: srcFrames))
        }
        
        // Resample if needed
        let ratio = targetSampleRate / srcSampleRate
        _ = Int(Double(srcFrames) * ratio)
        
        let resampled: [Float]
        if abs(ratio - 1.0) > 0.001 {
            resampled = resampleAudio(input: monoFloats, sourceRate: srcSampleRate, targetRate: targetSampleRate)
        } else {
            resampled = monoFloats
        }
        
        // Convert Float32 [-1.0, 1.0] to Int16
        var int16Samples = [Int16](repeating: 0, count: resampled.count)
        for i in 0..<resampled.count {
            let clamped = max(-1.0, min(1.0, resampled[i]))
            int16Samples[i] = Int16(clamped * Float(Int16.max))
        }
        
        return Data(bytes: int16Samples, count: int16Samples.count * 2)
    }
    
    /// Simple linear interpolation resampling
    private func resampleAudio(input: [Float], sourceRate: Double, targetRate: Double) -> [Float] {
        let ratio = sourceRate / targetRate
        let outputLength = Int(Double(input.count) / ratio)
        var output = [Float](repeating: 0, count: outputLength)
        
        for i in 0..<outputLength {
            let srcIndex = Double(i) * ratio
            let srcIndexInt = Int(srcIndex)
            let frac = srcIndex - Double(srcIndexInt)
            
            if srcIndexInt + 1 < input.count {
                output[i] = input[srcIndexInt] * (1.0 - Float(frac)) + input[srcIndexInt + 1] * Float(frac)
            } else {
                output[i] = input[min(srcIndexInt, input.count - 1)]
            }
        }
        
        return output
    }
    
    /// Calculate RMS from audio buffer for VAD
    private func calculateRMS(from buffer: AVAudioPCMBuffer) -> Float {
        guard let channelData = buffer.floatChannelData else { return 0 }
        let frames = Int(buffer.frameLength)
        guard frames > 0 else { return 0 }
        
        var sumSq: Float = 0
        let data = channelData[0]
        
        // Downsample for performance: check every 4th sample
        var count = 0
        for i in stride(from: 0, to: frames, by: 4) {
            sumSq += data[i] * data[i]
            count += 1
        }
        
        guard count > 0 else { return 0 }
        let rms = sqrt(sumSq / Float(count))
        return rms
    }
    
    // MARK: - Audio Playback (Streaming)
    
    private func setupAudioPlayback() {
        // Create playback engine
        if audioEngineP == nil {
            audioEngineP = AVAudioEngine()
        }
        
        guard let engine = audioEngineP else { return }
        
        // If already setup, just connect
        if audioPlayerNode != nil, engine.isRunning {
            return
        }
        
        let playerNode = AVAudioPlayerNode()
        audioPlayerNode = playerNode
        engine.attach(playerNode)
        
        // Use the server's sample rate (usually 24000)
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                   sampleRate: audioSampleRate,
                                   channels: 1,
                                   interleaved: false)!
        playbackFormat = format
        
        engine.connect(playerNode, to: engine.mainMixerNode, format: format)
        engine.prepare()
        
        do {
            try engine.start()
            playerNode.play()
            logger("Audio playback started")
        } catch {
            logger("Failed to start playback engine: \(error)")
        }
    }
    
    private func stopAudioPlayback() {
        audioPlayerNode?.stop()
        audioEngineP?.stop()
        audioPlayerNode = nil
        audioEngineP = nil
        playbackFormat = nil
    }
    
    // MARK: - Helpers
    
    private func logger(_ message: String) {
        print("[RealtimeCall] \(message)")
    }
}
