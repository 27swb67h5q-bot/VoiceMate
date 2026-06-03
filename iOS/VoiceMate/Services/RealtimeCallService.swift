import Foundation
import AVFoundation
import Accelerate
import Combine

/// Manages a true full-duplex real-time voice conversation with the VoiceMate backend.
///
/// Architecture (Full-Duplex):
/// ┌───────────────────────────────────────────────────���──────────┐
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
    /// Lock flag to avoid displaying duplicate server ASR turns
    private var isProcessingUtterance = false
    private var lastSentUserText: String = ""
    
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
    
    // MARK: - Audio Playback (Shared engine — playerNode attached to audioEngine)
    
    // MARK: - Audio Playback (Streaming)
    private var playbackPlayerNode: AVAudioPlayerNode?
    /// Playback audio buffer queue — buffers arriving chunks while player is busy
    private let playbackQueue = DispatchQueue(label: "com.voicemate.playback", qos: .userInitiated)
    private var pendingBuffers: [(AVAudioPCMBuffer, Bool)] = []
    /// Serial queue for scheduling playback buffers to avoid race conditions
    private let scheduleQueue = DispatchQueue(label: "com.voicemate.schedule", qos: .userInitiated)
    /// Tracks whether playback has ever been started (avoids stop()-before-play crash on iOS 16.x)
    private var playbackEverStarted = false

    private var playbackFormat: AVAudioFormat?
    /// Observer token for AVAudioSession route change notifications
    private var routeChangeObserver: NSObjectProtocol?

    /// Dedicated mixer node to avoid reconnecting to mainMixer on a running engine
    private var playbackMixerNode: AVAudioMixerNode?
    /// Callback invoked per-turn when a complete user or AI utterance is done
    var onTurnCompleted: ((_ isUser: Bool, _ text: String) -> Void)?
    
    // MARK: - Internal State
    private var isMicActive = false
    private var audioSampleRate: Double = 24000
    private let vadThreshold: Float = 0.008
    
    /// Number of consecutive frames above threshold to declare user speech
    private let speechDebounceFrames: Int = 3
    
    /// Counter for consecutive frames above/below VAD threshold
    private var speechFrameCount: Int = 0
    private var silenceFrameCount: Int = 0
    
    /// Frames of silence before releasing the "user speaking" state
    private let silenceReleaseFrames: Int = 6
    
    /// Running baseline RMS over the last N frames (adaptive floor)
    private var rmsHistory: [Float] = []
    private let rmsHistorySize: Int = 40  // ~2 seconds at 50ms per frame
    
    /// Number of spectral bands for voice-print comparison
    private let spectralBandCount: Int = 8
    
    /// Running average of per-band spectral energy across recent valid speech frames
    private var userSpectralProfile: [Float]?
    private var spectralProfileFrameCount: Int = 0
    
    /// Frames of user speech needed before we trust spectral matching
    private let spectralProfileWarmupFrames: Int = 10  // ~0.5 seconds of speech
    
    /// Maximum cosine distance to accept a frame as matching the user's spectral profile
    private let spectralMatchThreshold: Float = 0.6
    
    /// Minimum consecutive VAD frames required during AI playback to trigger barge-in
    private let bargeInDebounceFrames: Int = 6  // ~300ms at 50ms frames
    /// Barge-in debounce counter: consecutive VAD-positive frames during AI playback
    private var bargeInDebounceCount: Int = 0

    /// Whether voice isolation / voice processing is available
    
    /// Adaptive threshold multiplier based on mic proximity heuristic
    private var adaptiveThresholdMultiplier: Float = 1.0
    private var durationTimer: Timer?
    
    // MARK: - Playback State Lock
    /// Protects playbackPlayerNode, playbackStateValid, playbackFormat from concurrent access
    /// across URLSession delegate queue (handleAudioData) and main thread (start/stop).
    private let playbackStateLock = NSLock()
    /// When false, handleAudioData should not schedule buffers on the player node.
    /// Set to true only when startPlaybackNode() has called play().
    private var playbackStateValid = false

    // MARK: - Init
    
    init(serverHost: String, serverPort: String, voice: String, persona: String, speed: Double) {
        self.serverHost = serverHost
        self.serverPort = serverPort
        self.voice = voice
        self.persona = persona
        self.speed = speed
        
        let config = URLSessionConfiguration.default
        config.waitsForConnectivity = true
        config.timeoutIntervalForResource = 600
        self.urlSession = URLSession(configuration: config)
        
        super.init()
        
        requestPermissions()
        
        // Configure audio session once at init time — do not reconfigure during call
        configureAudioSession()
        setupRouteChangeObserver()
    }
    
    deinit {
        if audioEngine.isRunning {
            audioEngine.stop()
        }
    }
    
    private func requestPermissions() {
        AVAudioSession.sharedInstance().requestRecordPermission { _ in }
    }
    
    // MARK: - Audio Session Configuration
    
    /// Configure audio session once. Called at init time.
    /// On iOS 16.x, reconfiguring the audio session while the engine is running can cause
    /// crashes. We set everything up here and never change it during a call.
    private func configureAudioSession() {
        let audioSession = AVAudioSession.sharedInstance()
        do {
            // Use .default mode to ensure audio routes to the bottom speaker.
            // .voiceChat mode overrides .defaultToSpeaker on many iOS versions
            // and forces audio to the earpiece. We avoid it for that reason.
            try audioSession.setCategory(
                .playAndRecord,
                mode: .default,
                options: [.allowBluetoothHFP, .defaultToSpeaker]
            )
            try audioSession.overrideOutputAudioPort(.speaker)
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)
            logger("Audio session configured: .playAndRecord + .default mode + speaker override")
        } catch {
            logger("Failed to configure audio session: \(error)")
            let fallbackSession = AVAudioSession.sharedInstance()
            try? fallbackSession.setCategory(.playAndRecord, mode: .default, options: [.allowBluetoothHFP, .defaultToSpeaker])
            try? fallbackSession.overrideOutputAudioPort(.speaker)
            try? fallbackSession.setActive(true, options: .notifyOthersOnDeactivation)
        }
    }



    /// Listen for audio route changes and re-apply speaker override.
    /// iOS resets overrideOutputAudioPort(.speaker) on route changes.
    private func setupRouteChangeObserver() {
        routeChangeObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: AVAudioSession.sharedInstance(),
            queue: .main
        ) { [weak self] _ in
            // iOS resets overrideOutputAudioPort(.speaker) on route changes, so re-apply it.
            guard let self = self else { return }
            try? AVAudioSession.sharedInstance().overrideOutputAudioPort(.speaker)
            self.logger("Audio route changed, re-applied speaker output override")
        }
        logger("Route change observer registered")
    }

    // MARK: - Call Lifecycle
    
    func startCall() {
        guard !isCallActive else { return }

        AVAudioSession.sharedInstance().requestRecordPermission { [weak self] granted in
            DispatchQueue.main.async {
                guard let self = self else { return }
                guard granted else {
                    self.errorMessage = "Microphone permission is required"
                    self.isCallActive = false
                    return
                }
                self.startCallAfterPermission()
            }
        }
    }

    private func startCallAfterPermission() {
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
        
        // Reset VAD state
        speechFrameCount = 0
        silenceFrameCount = 0
        rmsHistory.removeAll()
        userSpectralProfile = nil
        spectralProfileFrameCount = 0
        adaptiveThresholdMultiplier = 1.0
        bargeInDebounceCount = 0
        
        // Stop capture first (stops the audio engine), then detach player node.
        stopAudioCapture()
        
        // Take the lock before detaching/destroying playback objects.
        // handleAudioData runs on scheduleQueue and checks under the same lock.
        playbackStateLock.lock()
        playbackStateValid = false
        playbackFormat = nil
        
        if let node = playbackPlayerNode {
            audioEngine.detach(node)
        }
        if let mixNode = playbackMixerNode {
            audioEngine.detach(mixNode)
        }
        playbackPlayerNode = nil
        playbackMixerNode = nil
        playbackEverStarted = false
        playbackStateLock.unlock()
        
        // Clear any pending buffers
        scheduleQueue.sync {
            pendingBuffers.removeAll()
        }
        
        disconnectWebSocket()
        
        durationTimer?.invalidate()
        durationTimer = nil
        pingTimer?.invalidate()
        pingTimer = nil
        reconnectTimer?.invalidate()
        reconnectTimer = nil

        // Remove route change observer
        if let observer = routeChangeObserver {
            NotificationCenter.default.removeObserver(observer)
            routeChangeObserver = nil
        }

        
        // Do NOT deactivate the audio session — it was configured at init time.
        // Deactivating can cause issues if the user starts another call quickly.
        
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
                }
            
            case "asr_final":
                // Server-side faster-whisper is now the source of truth.
                // Do not send this text back to the server, or the same turn will be answered twice.
                if let content = json["text"] as? String {
                    self.handleServerASRFinal(content)
                }
            
            case "token":
                if let content = json["content"] as? String {
                    self.aiText += content
                }
            
            case "audio_start":
                self.isAISpeaking = true
                self.bargeInDebounceCount = 0
                self.isUserSpeaking = false
                if let sr = json["sample_rate"] as? Double {
                    self.audioSampleRate = sr
                }
                // Clear pending buffers and mark playback invalid BEFORE reconfiguring.
                // This must complete before handleAudioData (on scheduleQueue) can
                // check playbackStateValid, so that no stale buffers are scheduled.
                self.scheduleQueue.sync {
                    self.pendingBuffers.removeAll()
                    self.playbackStateLock.lock()
                    self.playbackStateValid = false
                    self.playbackStateLock.unlock()
                }
                // Stop any previous playback BEFORE reconfiguring the format.
                // play() was called at least once from the previous audio_start,
                // so stop() is safe (playbackEverStarted is true).
                if self.playbackEverStarted, let node = self.playbackPlayerNode {
                    node.stop()
                }
                self.setupAudioPlayback()
                // Route audio to speaker even in .voiceChat mode (safe: does not reconfigure session)
                try? AVAudioSession.sharedInstance().overrideOutputAudioPort(.speaker)
                self.startPlaybackNode()
            
            case "audio_end":
                self.onAudioEnd()
            
            case "turn_done":
                // Reset barge-in debounce state
                self.bargeInDebounceCount = 0
                self.handleTurnDone()
            
            case "interrupted":
                self.isAISpeaking = false
                self.bargeInDebounceCount = 0
                self.stopPlaybackInternal()
            
            case "timeout":
                self.errorMessage = "连接超时"
                self.endCall()
            
            case "turn_skipped":
                self.handleTurnSkipped()
            
            case "error":
                if let msg = json["message"] as? String {
                    self.errorMessage = msg
                }
                self.isAISpeaking = false
                self.isProcessingUtterance = false
            
            default:
                break
            }
        }
    }
    
    // MARK: - Utterance & Turn Handling

    private func handleServerASRFinal(_ rawText: String) {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text != "__vad_detected__" else {
            logger("Server ASR final was empty or marker-only")
            return
        }
        guard !isFillerText(text) else {
            logger("Ignoring server filler text: \(text)")
            return
        }
        guard text != lastSentUserText else {
            logger("Ignoring duplicate server ASR text: \(text.prefix(60))")
            return
        }

        logger("Server ASR final: \(text.prefix(80))")
        currentText = text
        isUserSpeaking = false
        isProcessingUtterance = true
        lastSentUserText = text

        transcript.append((isUser: true, text: text))
        onTurnCompleted?(true, text)
    }
    
    private func handleTurnDone() {
        if !aiText.isEmpty {
            transcript.append((isUser: false, text: aiText))
            onTurnCompleted?(false, aiText)
        }
        
        isAISpeaking = false
        currentText = ""
        aiText = ""
        isProcessingUtterance = false
        lastSentUserText = ""
        logger("Turn completed, ready for next utterance")
    }
    
    private func handleTurnSkipped() {
        logger("Turn skipped by server (noise/filler detected)")
        isProcessingUtterance = false
        lastSentUserText = ""
    }
    
    private func onAudioEnd() {
        logger("Audio stream ended, waiting for turn_done")
    }
    
    private func handleAudioData(_ data: Data) {
        // PCM16 audio chunks from server — play via the single engine
        // Check format under the lock to avoid racing with stopPlayback/endCall
        playbackStateLock.lock()
        let format = playbackFormat
        let valid = playbackStateValid
        playbackStateLock.unlock()
        
        guard let format = format else { return }
        guard audioEngine.isRunning else { return }
        
        let frameLength = data.count / 2  // 16-bit samples
        guard frameLength > 0 else { return }
        
        guard let pcmBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frameLength)) else {
            return
        }
        pcmBuffer.frameLength = AVAudioFrameCount(frameLength)
        
        // Convert PCM16 -> Float32 directly in the PCM buffer's float storage,
        // avoiding intermediate Swift array allocation and unsafe memcpy on Swift Array value types.
        guard let floatPtr = pcmBuffer.floatChannelData?[0] else { return }
        data.withUnsafeBytes { (rawPtr: UnsafeRawBufferPointer) in
            guard let srcPtr = rawPtr.baseAddress?.assumingMemoryBound(to: Int16.self) else { return }
            // vDSP_vflt16: Int16 samples -> Float32 values in [-32768, 32767]
            vDSP_vflt16(srcPtr, 1, floatPtr, 1, vDSP_Length(frameLength))
            // Normalize to [-1.0, 1.0]
            var scale = Float(Int16.max)
            vDSP_vsdiv(floatPtr, 1, &scale, floatPtr, 1, vDSP_Length(frameLength))
        }
        
        // Schedule via serial queue. Inside the block we re-check playback state
        // under the lock — this ensures we never schedule on a stopped/detached node
        // on iOS 16.x (which causes EXC_BAD_ACCESS).
        scheduleQueue.async { [weak self] in
            guard let self = self else { return }
            guard self.audioEngine.isRunning else { return }
            
            self.playbackStateLock.lock()
            let stillValid = self.playbackStateValid
            let node = self.playbackPlayerNode
            self.playbackStateLock.unlock()
            
            guard let playerNode = node else { return }
            
            if stillValid && playerNode.isPlaying {
                playerNode.scheduleBuffer(pcmBuffer)
            } else if stillValid {
                // Node is valid but not yet playing (transient between audio_start and play())
                self.pendingBuffers.append((pcmBuffer, false))
            } else {
                // Playback state invalid — dropping buffer to avoid scheduling
                // on a stopped node (crashes iOS 16.x).
                self.logger("Dropping audio buffer: playback state invalid")
            }
        }
    }
    
    /// Check if the playback engine is properly configured
    private func engineSupportsPlayback() -> Bool {
        guard audioEngine.isRunning else { return false }
        guard playbackPlayerNode != nil else { return false }
        guard playbackFormat != nil else { return false }
        return true
    }
    
    // MARK: - Audio Capture (Always-on Mic)
    
    private func startAudioCapture() {
        // Audio session is already configured in init(). Do NOT reconfigure it here
        // because reconfiguring the session with overrideOutputAudioPort on a
        // running or newly-started engine can crash on iOS 16.5.
        
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
                
                // Advanced VAD for user speech detection
                let rms = self.calculateRMS(from: buffer)
                let spectralBands = self.computeSpectralBands(from: buffer)
                let vadDecision = self.evaluateVAD(rms: rms, spectralBands: spectralBands, isAISpeaking: self.isAISpeaking)
                
                DispatchQueue.main.async {
                    self.isUserSpeaking = vadDecision
                }
                
                // Client-side barge-in detection: only trigger when VAD detects speech
                // AND AI is currently speaking. During calm periods, the server handles turn-taking.
                if vadDecision && self.isAISpeaking {
                    self.bargeInDebounceCount += 1
                    if self.bargeInDebounceCount >= self.bargeInDebounceFrames {
                        self.sendJson(["type": "barge_in"])
                        self.bargeInDebounceCount = 0
                    }
                } else {
                    self.bargeInDebounceCount = 0
                }
            }
        }
        
        // Pre-attach player node and a dedicated mixer for TTS playback (must be done
        // before engine starts). The dedicated mixer avoids reconnecting the player node
        // to mainMixer on a running engine during every audio_start, which can crash.
        let mixNode = AVAudioMixerNode()
        audioEngine.attach(mixNode)
        audioEngine.connect(mixNode, to: audioEngine.mainMixerNode, format: nil)
        
        let playerNode = AVAudioPlayerNode()
        audioEngine.attach(playerNode)
        audioEngine.connect(playerNode, to: mixNode, format: nil)
        playbackPlayerNode = playerNode
        playbackMixerNode = mixNode
        do {
            // Prepare player node before engine start to init scheduler (iOS 16.x safety)
            playerNode.prepare(withFrameCount: 8820)
            try audioEngine.start()
            try? AVAudioSession.sharedInstance().overrideOutputAudioPort(.speaker)
            // Route audio to speaker (safe: overrideOutputAudioPort does not reconfigure the session)
            isMicActive = true
            logger("Audio capture started (16kHz)")
        } catch {
            logger("Failed to start audio engine: \(error)")
            errorMessage = "麦克风启动失败"
        }
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
    
    /// Compute spectral energy distribution across frequency bands.
    /// Uses simple FFT-based band energy decomposition for voice-print comparison.
    /// Returns an array of per-band energy ratios (normalized to sum = 1.0).
    private func computeSpectralBands(from buffer: AVAudioPCMBuffer) -> [Float] {
        guard let channelData = buffer.floatChannelData else {
            return [Float](repeating: 0, count: spectralBandCount)
        }
        let frames = Int(buffer.frameLength)
        guard frames > 0 else {
            return [Float](repeating: 0, count: spectralBandCount)
        }
        
        let data = channelData[0]
        let bandCount = spectralBandCount
        
        // Compute magnitude FFT. Use the nearest power of 2.
        let fftSize = 1 << (Int(log2(Float(frames))) + 1)
        guard fftSize >= 4 else {
            return [Float](repeating: 0, count: bandCount)
        }
        
        // Prepare split complex buffer
        let realPart = UnsafeMutablePointer<Float>.allocate(capacity: fftSize)
        let imagPart = UnsafeMutablePointer<Float>.allocate(capacity: fftSize)
        defer {
            realPart.deallocate()
            imagPart.deallocate()
        }
        
        // Copy with Hanning window
        for i in 0..<fftSize {
            if i < frames {
                let hann = 0.5 * (1 - cos(2 * Float.pi * Float(i) / Float(fftSize - 1)))
                realPart[i] = data[i] * hann
            } else {
                realPart[i] = 0
            }
            imagPart[i] = 0
        }
        
        // FFT
        let log2n = UInt(log2(Float(fftSize)))
        guard let fftSetup = vDSP_create_fftsetup(log2n, FFTRadix(kFFTRadix2)) else {
            return [Float](repeating: 0, count: bandCount)
        }
        defer { vDSP_destroy_fftsetup(fftSetup) }
        
        var splitComplex = DSPSplitComplex(realp: realPart, imagp: imagPart)
        vDSP_fft_zip(fftSetup, &splitComplex, 1, log2n, FFTDirection(kFFTDirection_Forward))
        
        // Compute magnitude spectrum (only first half)
        let halfSize = fftSize / 2
        var magnitudes = [Float](repeating: 0, count: halfSize)
        vDSP_zvmags(&splitComplex, 1, &magnitudes, 1, vDSP_Length(halfSize))
        // Convert to magnitude (sqrt)
        var sqrtMagnitudes = [Float](repeating: 0, count: halfSize)
        vvsqrtf(&sqrtMagnitudes, magnitudes, [Int32(halfSize)])
        
        // Divide spectrum into bands (wider bands at higher frequencies, like mel-scale)
        let binsPerBand = halfSize / bandCount
        var bands = [Float](repeating: 0, count: bandCount)
        var totalEnergy: Float = 0
        
        for b in 0..<bandCount {
            let startBin = b * binsPerBand
            let endBin = (b == bandCount - 1) ? halfSize : (b + 1) * binsPerBand
            var bandSum: Float = 0
            for k in startBin..<min(endBin, halfSize) {
                bandSum += sqrtMagnitudes[k]
            }
            bands[b] = bandSum
            totalEnergy += bandSum
        }
        
        // Normalize
        if totalEnergy > 0 {
            for b in 0..<bandCount {
                bands[b] /= totalEnergy
            }
        }
        
        return bands
    }
    
    /// Evaluate VAD using sustained speech + spectral matching + adaptive threshold.
    /// Returns true only when confident the user is speaking.
    private func evaluateVAD(rms: Float, spectralBands: [Float], isAISpeaking: Bool = false) -> Bool {
        // 1. Update RMS history for adaptive baseline
        rmsHistory.append(rms)
        if rmsHistory.count > rmsHistorySize {
            rmsHistory.removeFirst()
        }
        
        // 2. Compute adaptive threshold based on recent noise floor
        let sortedHistory = rmsHistory.sorted()
        let medianIdx = sortedHistory.count / 2
        let noiseFloor = sortedHistory[medianIdx]
        let adaptiveFloor = noiseFloor * 2.0
        
        // Calculate adaptive multiplier: if recent RMS is low (quiet environment),
        // use a generous threshold. If recent RMS is high (phone close to mouth),
        // use a higher threshold that requires louder speech.
        let recentMax = sortedHistory.last ?? rms
        let dynamicRange = max(recentMax - noiseFloor, 0.001)
        // When the mic is held close, the closest peaks are ~10x the noise floor.
        // Use a multiplier of 1.0 for quiet, up to ~3.0 for close-proximity.
        adaptiveThresholdMultiplier = min(2.0, max(1.0, dynamicRange / 0.03))
        let effectiveThreshold = max(adaptiveFloor, vadThreshold * adaptiveThresholdMultiplier)

        // When AI is speaking (TTS playing through speaker), the mic picks up
        // the playback audio which can falsely trigger VAD. Apply a higher
        // threshold so only genuinely loud user speech passes through.
        let aiPlaybackThresholdMultiplier: Float = isAISpeaking ? 4.0 : 1.0
        let finalThreshold = effectiveThreshold * aiPlaybackThresholdMultiplier
        
        // 3. Check if current frame exceeds threshold (basic energy test)
        let aboveEnergyThreshold = rms > finalThreshold
        
        // 4. Spectral matching against user voice profile (if warm)
        var spectralMatch = true
        if let profile = userSpectralProfile, spectralProfileFrameCount >= spectralProfileWarmupFrames {
            // Compute cosine similarity between current spectral bands and user profile
            var dotProduct: Float = 0
            var magCurrent: Float = 0
            var magProfile: Float = 0
            for i in 0..<min(spectralBands.count, profile.count) {
                dotProduct += spectralBands[i] * profile[i]
                magCurrent += spectralBands[i] * spectralBands[i]
                magProfile += profile[i] * profile[i]
            }
            let similarity = dotProduct / (sqrt(magCurrent) * sqrt(magProfile) + 0.001)
            // Cosine distance: lower = more similar. Accept if distance < threshold.
            spectralMatch = (1.0 - similarity) < spectralMatchThreshold
        }
        
        // 5. Update user spectral profile on high-confidence speech frames
        //    (frames that are loud and sustained — likely user)
        if aboveEnergyThreshold && spectralMatch {
            // Update running average of spectral profile
            if var profile = userSpectralProfile {
                let alpha: Float = 0.1  // Slow adapt
                for i in 0..<min(spectralBands.count, profile.count) {
                    profile[i] = profile[i] * (1 - alpha) + spectralBands[i] * alpha
                }
                userSpectralProfile = profile
            } else {
                userSpectralProfile = spectralBands
            }
            spectralProfileFrameCount += 1
        }
        
        // 6. Sustained speech detection (debounce)
        if aboveEnergyThreshold && spectralMatch {
            speechFrameCount += 1
            silenceFrameCount = 0
        } else {
            silenceFrameCount += 1
            if silenceFrameCount >= silenceReleaseFrames {
                speechFrameCount = 0
            }
        }
        
        // 7. Final decision: require sustained speech across multiple frames
        return speechFrameCount >= speechDebounceFrames
    }
    
    
    // MARK: - Filler Text Detection
    
    /// Set of filler/noise words that should be silently dropped
    private static let fillerWords: Set<String> = [
        "\u{55ef}", "\u{5560}", "\u{554a}", "\u{54e6}", "\u{5614}", "\u{5416}", "\u{54c8}", "\u{563f}",
        "um", "uh", "ah", "er", "hmm",
    ]
    
    /// Check if text is just filler/thinking noise (e.g., "嗯", "um", "啊")
    private func isFillerText(_ text: String) -> Bool {
        let stripped = text.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "。.!！?？,，…"))
            .lowercased()
        return Self.fillerWords.contains(stripped)
    }
    

    // MARK: - Audio Playback (Streaming)
    
    private func setupAudioPlayback() {
        guard let playerNode = playbackPlayerNode else {
            logger("setupAudioPlayback: playerNode not pre-attached")
            return
        }
        guard let mixNode = playbackMixerNode else {
            logger("setupAudioPlayback: playbackMixerNode not available")
            return
        }
        
        // Only reconnect and update format if sample rate changed
        if playbackFormat?.sampleRate != audioSampleRate {
            // Reconnect the player node to the playback mixer with an explicit format
            // matching the server's sample rate (e.g. 24000 Hz). This tells the engine
            // the player node's output format, so scheduleBuffer buffers must match it.
            // The engine will automatically perform sample rate conversion between the
            // player node (24000 Hz) and the mixer/hardware output (~48000 Hz).
            let playerFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: audioSampleRate,
                channels: 1,
                interleaved: false
            )!
            audioEngine.connect(playerNode, to: mixNode, format: playerFormat)
            playbackFormat = playerFormat
            logger("Audio graph reconnected: player node format -> \(Int(audioSampleRate)) Hz")
        }
        
        // NOTE: Do NOT call playerNode.stop() here — on iOS 16.x, calling stop()
        // on a player node that has never been started with play() causes EXC_BAD_ACCESS.
        // The player's previous playback was already stopped in the audio_start handler
        
        logger("Audio playback format set (sample rate: \(Int(audioSampleRate)) Hz)")
    }

    /// Starts (or restarts) the player node for streaming playback.
    ///
    /// On iOS 16.x, calling stop() on an AVAudioPlayerNode that has never received
    /// play() causes EXC_BAD_ACCESS. We avoid this by tracking playbackEverStarted.
    ///
    /// For the 2nd+ TTS turn, the node was already stopped in the audio_start handler
    /// (before setupAudioPlayback), so here we only call play() — no more stop().
    /// This avoids the iOS 16.x crash that can occur when stop()→play() on a running
    /// engine.
    private func startPlaybackNode() {
        guard let playerNode = playbackPlayerNode else { return }
        
        playerNode.play()
        playbackEverStarted = true
        
        // Mark playback state valid, then flush pending buffers.
        // Must happen inside scheduleQueue.sync to avoid races with handleAudioData
        // which also accesses pendingBuffers and checks playbackStateValid under the lock.
        scheduleQueue.sync { [weak self] in
            guard let self = self else { return }
            self.playbackStateLock.lock()
            self.playbackStateValid = true
            self.playbackStateLock.unlock()
            
            let pending = self.pendingBuffers
            self.pendingBuffers.removeAll()
            for (buffer, _) in pending {
                playerNode.scheduleBuffer(buffer)
            }
        }
        
        logger("Audio playback started (sample rate: \(Int(audioSampleRate)) Hz)")
    }
    
    /// Internal stop: used for transient interruptions (barge-in).
    /// Stops the node and marks state invalid, but keeps playbackFormat alive
    /// so handleAudioData can still check state and drop rather than crash.
    private func stopPlaybackInternal() {
        guard let playerNode = playbackPlayerNode else { return }
        
        // Serialize with handleAudioData
        scheduleQueue.sync {
            pendingBuffers.removeAll()
        }
        
        playbackStateLock.lock()
        playbackStateValid = false
        playbackStateLock.unlock()
        
        playerNode.stop()
        logger("Audio playback stopped (interrupted)")
    }
    
    /// Full cleanup: stops node, clears format, mark state invalid.
    /// Called only from endCall().
    private func stopAudioPlayback() {
        guard let playerNode = playbackPlayerNode else { return }
        
        // Serialize with handleAudioData
        scheduleQueue.sync {
            pendingBuffers.removeAll()
        }
        
        playbackStateLock.lock()
        playbackStateValid = false
        playbackFormat = nil
        playbackStateLock.unlock()
        
        playerNode.stop()
        logger("Audio playback stopped (full cleanup)")
    }
    
    // MARK: - Helpers
    
    private func logger(_ message: String) {
        print("[RealtimeCall] \(message)")
    }
}
