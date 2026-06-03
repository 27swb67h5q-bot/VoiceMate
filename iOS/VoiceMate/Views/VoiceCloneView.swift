import AVFoundation
import SwiftUI

struct VoiceCloneView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var service = VoiceMateService()
    @State private var recorder: AVAudioRecorder?
    @State private var samples: [URL] = []
    @State private var isRecording = false
    @State private var status = "录 3 段自然说话样本，每段 8 到 15 秒。"
    @State private var isUploading = false
    @AppStorage("cloned_voice_id") private var clonedVoiceId = ""

    private let prompts = [
        "今天过得怎么样？我想听你慢慢说。",
        "如果我们正在打电话，你希望我用什么语气陪你？",
        "随便讲一小段最近发生的事，保持自然就好。",
    ]

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(status)
                        .font(.body)
                    if !clonedVoiceId.isEmpty {
                        Text("当前克隆音色：\(clonedVoiceId)")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }

                Section("样本") {
                    ForEach(prompts.indices, id: \.self) { index in
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(prompts[index])
                                Text(index < samples.count ? "已录制" : "未录制")
                                    .font(.caption)
                                    .foregroundStyle(index < samples.count ? .green : .secondary)
                            }
                            Spacer()
                            if index < samples.count {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(.green)
                            }
                        }
                    }
                }

                Section {
                    Button {
                        isRecording ? stopRecording() : startRecording()
                    } label: {
                        Label(isRecording ? "停止录制" : "录制下一段", systemImage: isRecording ? "stop.circle.fill" : "record.circle")
                    }
                    .disabled(samples.count >= prompts.count && !isRecording)

                    Button {
                        upload()
                    } label: {
                        if isUploading {
                            ProgressView()
                        } else {
                            Label("上传并创建音色", systemImage: "icloud.and.arrow.up")
                        }
                    }
                    .disabled(samples.isEmpty || isUploading)
                }
            }
            .navigationTitle("声音复刻")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("完成") { dismiss() }
                }
            }
        }
    }

    private func startRecording() {
        guard samples.count < prompts.count else { return }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .spokenAudio, options: [.defaultToSpeaker])
            try session.setActive(true)

            let url = FileManager.default.temporaryDirectory.appendingPathComponent("voicemate-sample-\(UUID().uuidString).m4a")
            let settings: [String: Any] = [
                AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                AVSampleRateKey: 44100,
                AVNumberOfChannelsKey: 1,
                AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
            ]
            recorder = try AVAudioRecorder(url: url, settings: settings)
            recorder?.record()
            isRecording = true
            status = prompts[samples.count]
        } catch {
            status = "录制失败：\(error.localizedDescription)"
        }
    }

    private func stopRecording() {
        guard let recorder else { return }
        recorder.stop()
        samples.append(recorder.url)
        self.recorder = nil
        isRecording = false
        status = samples.count >= prompts.count ? "样本已够，可以上传。" : "已保存，继续录下一段。"
    }

    private func upload() {
        isUploading = true
        Task {
            do {
                let result = try await service.createCloneVoice(fileURLs: samples)
                clonedVoiceId = result.voiceId
                UserDefaults.standard.set(result.voiceId, forKey: "selected_voice")
                status = result.message ?? "音色已创建。"
            } catch {
                status = "上传失败：\(error.localizedDescription)"
            }
            isUploading = false
        }
    }
}
