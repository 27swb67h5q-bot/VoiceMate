import AVFoundation
import Foundation
import Speech

final class AudioService: NSObject, ObservableObject {
    @Published var isRecording = false
    @Published var transcribedText = ""
    @Published var level: Float = 0
    @Published var errorMessage: String?

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN"))
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?

    override init() {
        super.init()
        requestPermissions()
    }

    func start() {
        guard !isRecording else { return }
        transcribedText = ""
        errorMessage = nil

        do {
            try configureSession()
            try startRecognition()
            isRecording = true
        } catch {
            errorMessage = error.localizedDescription
            stop()
        }
    }

    func stop() {
        guard isRecording || engine.isRunning else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        request?.endAudio()
        task?.cancel()
        task = nil
        request = nil
        isRecording = false
        level = 0
    }

    private func requestPermissions() {
        AVAudioSession.sharedInstance().requestRecordPermission { _ in }
        SFSpeechRecognizer.requestAuthorization { _ in }
    }

    private func configureSession() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .measurement, options: [.duckOthers])
        try session.setActive(true, options: .notifyOthersOnDeactivation)
    }

    private func startRecognition() throws {
        task?.cancel()
        task = nil

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        if #available(iOS 13.0, *) {
            request.requiresOnDeviceRecognition = false
        }
        self.request = request

        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            request.append(buffer)
            self?.updateLevel(buffer)
        }

        engine.prepare()
        try engine.start()

        task = recognizer?.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor in
                guard let self else { return }
                if let result {
                    self.transcribedText = result.bestTranscription.formattedString
                    if result.isFinal {
                        self.stop()
                    }
                }
                if let error {
                    self.errorMessage = error.localizedDescription
                    self.stop()
                }
            }
        }
    }

    private func updateLevel(_ buffer: AVAudioPCMBuffer) {
        guard let data = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        let frames = Int(buffer.frameLength)
        var sum: Float = 0
        for index in stride(from: 0, to: frames, by: 8) {
            sum += abs(data[index])
        }
        let value = min(1, sum / Float(max(frames / 8, 1)) * 12)
        Task { @MainActor in
            self.level = value
        }
    }
}
