import Foundation
import AVFoundation
import Speech

/// Handles voice recording and speech-to-text on the iOS device
/// Uses SFSpeechRecognizer for on-device transcription
class AudioService: NSObject, ObservableObject {
    // MARK: - State
    @Published var isRecording = false
    @Published var transcribedText = ""
    @Published var isTranscribing = false
    @Published var recordingLevel: Float = 0.0  // For UI visualization
    
    private var audioRecorder: AVAudioRecorder?
    private let speechRecognizer: SFSpeechRecognizer?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var audioEngine = AVAudioEngine()
    
    override init() {
        // Use Chinese speech recognition for best results
        self.speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN"))
        super.init()
        
        requestPermissions()
    }
    
    private func requestPermissions() {
        // Request microphone permission
        AVAudioSession.sharedInstance().requestRecordPermission { _ in }
        
        // Request speech recognition permission
        SFSpeechRecognizer.requestAuthorization { _ in }
    }
    
    // MARK: - Recording
    
    /// Start recording with speech-to-text
    func startRecording() {
        guard let speechRecognizer = speechRecognizer, speechRecognizer.isAvailable else {
            print("Speech recognizer not available")
            return
        }
        
        // Configure audio session for recording
        let audioSession = AVAudioSession.sharedInstance()
        do {
            try audioSession.setCategory(.playAndRecord, mode: .default)
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)
        } catch {
            print("Failed to set up audio session: \(error)")
            return
        }
        
        // Cancel any existing task
        recognitionTask?.cancel()
        recognitionTask = nil
        
        // Setup recognition request
        recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
        guard let recognitionRequest = recognitionRequest else { return }
        recognitionRequest.shouldReportPartialResults = true
        
        // Start recognition
        let inputNode = audioEngine.inputNode
        recognitionTask = speechRecognizer.recognitionTask(with: recognitionRequest) { [weak self] result, error in
            DispatchQueue.main.async {
                if let result = result {
                    self?.transcribedText = result.bestTranscription.formattedString
                }
                if error != nil || (result?.isFinal ?? false) {
                    self?.isTranscribing = false
                }
            }
        }
        
        // Configure audio engine
        let recordingFormat = inputNode.outputFormat(forBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { [weak self] buffer, _ in
            self?.recognitionRequest?.append(buffer)
            
            // Update recording level
            let level = buffer.averagePowerLevel()
            DispatchQueue.main.async {
                self?.recordingLevel = level
            }
        }
        
        audioEngine.prepare()
        do {
            try audioEngine.start()
            isRecording = true
            isTranscribing = true
            transcribedText = ""
        } catch {
            print("Failed to start audio engine: \(error)")
        }
    }
    
    /// Stop recording and return the transcribed text
    func stopRecording() async -> String {
        audioEngine.stop()
        audioEngine.inputNode.removeTap(onBus: 0)
        recognitionRequest?.endAudio()
        
        // Wait briefly for final recognition results
        try? await Task.sleep(nanoseconds: 500_000_000)  // 0.5s
        
        let result = transcribedText
        isRecording = false
        isTranscribing = false
        recordingLevel = 0.0
        
        // Cleanup
        recognitionRequest = nil
        recognitionTask = nil
        
        return result
    }
    
    /// Check if speech recognition is available
    var isSpeechAvailable: Bool {
        speechRecognizer?.isAvailable ?? false
    }
}

// MARK: - AVAudioPCMBuffer Extension

extension AVAudioPCMBuffer {
    func averagePowerLevel() -> Float {
        guard let channelData = floatChannelData else { return 0 }
        let channelDataValue = channelData.pointee
        let channelDataArray = Array(UnsafeBufferPointer(start: channelDataValue, count: Int(frameLength)))
        
        let sum = channelDataArray.reduce(0) { $0 + abs($1) }
        let average = sum / Float(frameLength)
        
        // Normalize to 0-1 range
        return min(max(average * 5, 0), 1)
    }
}
