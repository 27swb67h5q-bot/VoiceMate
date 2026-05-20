import SwiftUI
import AVFoundation

/// Voice cloning setup view
/// Records voice samples for AI voice cloning (placeholder - API integration TBD)
struct VoiceCloneView: View {
    @AppStorage("cloned_voice_id") private var clonedVoiceId = ""
    @State private var recordings: [VoiceSample] = []
    @State private var isRecording = false
    @State private var currentPhrase = ""
    @State private var audioRecorder: AVAudioRecorder?
    
    let phrases = [
        "你好，今天天气真不错",
        "我很高兴认识你",
        "今天过得怎么样？",
        "能再陪我聊一会儿吗",
        "好的，我明白了",
    ]
    
    var body: some View {
        List {
            Section {
                if clonedVoiceId.isEmpty {
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
                } else {
                    HStack {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundColor(.green)
                        Text("已生成专属声音 ID: \(clonedVoiceId.prefix(12))...")
                            .font(.caption)
                            .foregroundColor(.gray)
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
                        } else if index < recordings.count {
                            Button(action: { playSample(index) }) {
                                Image(systemName: "play.circle")
                                    .font(.title2)
                                    .foregroundColor(.purple)
                            }
                            .buttonStyle(.borderless)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
            
            if recordings.count == phrases.count && clonedVoiceId.isEmpty {
                Section {
                    Button(action: generateClone) {
                        HStack {
                            Spacer()
                            Image(systemName: "sparkles")
                            Text("生成专属声音")
                            Spacer()
                        }
                        .foregroundColor(.white)
                        .padding(.vertical, 8)
                    }
                    .listRowBackground(Color.purple)
                }
            }
            
            if !clonedVoiceId.isEmpty {
                Section {
                    Button("重新录制", role: .destructive) {
                        resetRecording()
                    }
                }
            }
        }
        .navigationTitle("声音克隆")
        .navigationBarTitleDisplayMode(.inline)
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
        // TODO: Audio playback for reviewing samples
    }
    
    private func generateClone() {
        // TODO: Upload samples to voice cloning API (Fish Audio / ElevenLabs)
        // For now, just set a placeholder ID so UI shows "已完成"
        clonedVoiceId = "pending_clone_\(UUID().uuidString.prefix(8))"
    }
    
    private func resetRecording() {
        clonedVoiceId = ""
        recordings = []
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
