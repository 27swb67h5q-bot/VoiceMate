import SwiftUI
import AVFoundation

/// Voice cloning setup view
/// Records voice samples and uploads to backend for Fish Audio voice cloning
struct VoiceCloneView: View {
    @AppStorage("cloned_voice_id") private var storedCloneVoiceId = ""
    @StateObject private var voiceService = VoiceMateService()
    @State private var recordings: [VoiceSample] = []
    @State private var isRecording = false
    @State private var currentPhrase = ""
    @State private var audioRecorder: AVAudioRecorder?
    @State private var isUploading = false
    @State private var uploadError: String?
    @State private var cloneStatus: String = ""
    
    let phrases = [
        "你好，今天天气真不错",
        "我很高兴认识你",
        "今天过得怎么样？",
        "能再陪我聊一会儿吗",
        "好的，我明白了",
    ]
    
    var effectiveCloneVoiceId: String {
        // The voice_id stored from server is "fish_<id>" format — use it directly as the voice parameter
        storedCloneVoiceId
    }
    
    var body: some View {
        List {
            Section {
                if storedCloneVoiceId.isEmpty && cloneStatus.isEmpty {
                    VStack(spacing: 16) {
                        Image(systemName: "waveform.circle")
                            .font(.system(size: 48))
                            .foregroundColor(.purple)
                        Text("录制声音样本")
                            .font(.headline)
                        Text("按顺序录制以下句子，每句录一次即可。\n完成后后端会自动生成你的专属声音。")
                            .font(.subheadline)
                            .foregroundColor(.gray)
                            .multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 20)
                    .listRowBackground(Color.clear)
                } else if !storedCloneVoiceId.isEmpty {
                    HStack {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundColor(.green)
                        Text("✓ 已生成专属声音")
                            .font(.subheadline)
                            .foregroundColor(.green)
                    }
                } else if cloneStatus == "processing" {
                    HStack {
                        ProgressView()
                            .scaleEffect(0.8)
                        Text("正在生成克隆声音（约1-2分钟）...")
                            .font(.subheadline)
                            .foregroundColor(.orange)
                    }
                }
            }
            
            Section("录制句子") {
                ForEach(Array(phrases.enumerated()), id: \.offset) { index, phrase in
                    HStack {
                        // Status indicator
                        if index < recordings.count {
                            Image(systemName: "checkmark.circle.fill")
                                .foregroundColor(.green)
                        } else if index == recordings.count && isRecording {
                            Image(systemName: "circle.fill")
                                .foregroundColor(.red)
                                .font(.system(size: 10))
                        } else {
                            Image(systemName: "circle")
                                .foregroundColor(.gray)
                        }
                        
                        Text("\(index + 1). \(phrase)")
                            .font(.body)
                            .padding(.leading, 4)
                        
                        Spacer()
                        
                        // Record button for current phrase
                        if index == recordings.count {
                            Button(action: toggleRecording) {
                                Image(systemName: isRecording ? "stop.circle.fill" : "mic.circle.fill")
                                    .font(.title2)
                                    .foregroundColor(isRecording ? .red : .purple)
                            }
                            .buttonStyle(.borderless)
                            .disabled(isUploading)
                        } else if index < recordings.count {
                            Button(action: { playSample(index) }) {
                                Image(systemName: "play.circle")
                                    .font(.title2)
                                    .foregroundColor(.purple)
                            }
                            .buttonStyle(.borderless)
                            .disabled(isUploading)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
            
            if recordings.count == phrases.count && storedCloneVoiceId.isEmpty && cloneStatus != "processing" {
                Section {
                    Button(action: generateClone) {
                        HStack {
                            Spacer()
                            if isUploading {
                                ProgressView()
                                    .scaleEffect(0.8)
                                    .tint(.white)
                            } else {
                                Image(systemName: "sparkles")
                            }
                            Text(isUploading ? "上传中..." : "生成专属声音")
                            Spacer()
                        }
                        .foregroundColor(.white)
                        .padding(.vertical, 8)
                    }
                    .listRowBackground(Color.purple)
                    .disabled(isUploading)
                }
            }
            
            if let error = uploadError {
                Section {
                    HStack {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundColor(.red)
                        Text(error)
                            .font(.caption)
                            .foregroundColor(.red)
                    }
                }
            }
            
            if !storedCloneVoiceId.isEmpty {
                Section {
                    Button("使用克隆声音聊天", action: selectCloneVoice)
                        .foregroundColor(.purple)
                    Button("重新录制", role: .destructive) {
                        resetRecording()
                    }
                }
            }
        }
        .navigationTitle("声音克隆")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            // Refresh status of existing clone
            if !storedCloneVoiceId.isEmpty {
                refreshCloneStatus()
            }
        }
    }
    
    private func selectCloneVoice() {
        // Set the cloned voice as the active TTS voice in settings
        // Uses @AppStorage("selected_voice") so ContentView picks it up automatically
        UserDefaults.standard.set(storedCloneVoiceId, forKey: "selected_voice")
    }
    
    private func refreshCloneStatus() {
        guard !storedCloneVoiceId.isEmpty else { return }
        Task {
            do {
                let status = try await voiceService.checkCloneStatus(voiceId: storedCloneVoiceId.wrappedValue)
                await MainActor.run {
                    cloneStatus = status.status
                }
            } catch {
                // Silently fail — the voice is still usable
            }
        }
    }
    
    private func toggleRecording() {
        if isRecording {
            stopRecording()
        } else {
            startRecording()
        }
    }
    
    private func startRecording() {
        let audioSession = AVAudioSession.sharedInstance()
        guard let _ = try? audioSession.setCategory(.playAndRecord, mode: .default) else { return }
        
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("voice_sample_\(recordings.count).m4a")
        
        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 44100,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ]
        
        guard let recorder = try? AVAudioRecorder(url: url, settings: settings) else { return }
        recorder.record()
        audioRecorder = recorder
        currentPhrase = phrases[recordings.count]
        isRecording = true
    }
    
    private func stopRecording() {
        audioRecorder?.stop()
        audioRecorder = nil
        isRecording = false
        
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("voice_sample_\(recordings.count).m4a")
        
        if FileManager.default.fileExists(atPath: url.path) {
            recordings.append(VoiceSample(phrase: currentPhrase, fileURL: url))
        }
    }
    
    private func playSample(_ index: Int) {
        guard index < recordings.count else { return }
        let url = recordings[index].fileURL
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
        let player = try? AVAudioPlayer(contentsOf: url)
        player?.play()
    }
    
    private func generateClone() {
        guard recordings.count == phrases.count else { return }
        
        uploadError = nil
        isUploading = true
        cloneStatus = "uploading"
        
        Task {
            do {
                let fileURLs = recordings.map { $0.fileURL }
                let response = try await voiceService.createCloneVoice(fileURLs: fileURLs)
                
                await MainActor.run {
                    isUploading = false
                    storedCloneVoiceId = response.voiceId  // e.g. "fish_abc123"
                    cloneStatus = response.status
                    
                    // Start polling for completion
                    pollCloneStatus()
                }
            } catch {
                await MainActor.run {
                    isUploading = false
                    cloneStatus = ""
                    uploadError = "上传失败: \(error.localizedDescription)"
                }
            }
        }
    }
    
    private func pollCloneStatus() {
        guard !storedCloneVoiceId.isEmpty else { return }
        
        Task {
            var retries = 0
            let maxRetries = 30  // Poll for up to ~60 seconds
            
            while retries < maxRetries {
                do {
                    let status = try await voiceService.checkCloneStatus(voiceId: storedCloneVoiceId.wrappedValue)
                    await MainActor.run {
                        cloneStatus = status.status
                    }
                    
                    if status.status == "completed" {
                        await MainActor.run {
                            // Automatically select the cloned voice
                            UserDefaults.standard.set(storedCloneVoiceId, forKey: "selected_voice")
                        }
                        return
                    }
                } catch {
                    // Transient error — keep polling
                }
                
                retries += 1
                try? await Task.sleep(nanoseconds: 2_000_000_000)  // 2 seconds
            }
        }
    }
    
    private func resetRecording() {
        storedCloneVoiceId = ""
        recordings = []
        cloneStatus = ""
        uploadError = nil
        // Clean up temp files
        for i in 0..<phrases.count {
            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent("voice_sample_\(i).m4a")
            try? FileManager.default.removeItem(at: url)
        }
    }
}

struct VoiceSample {
    let phrase: String
    let fileURL: URL
}

#Preview {
    NavigationStack {
        VoiceCloneView()
    }
}
