import SwiftUI

struct ContentView: View {
    @StateObject private var voiceService = VoiceMateService()
    @StateObject private var audioService = AudioService()
    
    @State private var messages: [ChatMessage] = []
    @State private var conversationId: String?
    @State private var showSettings = false
    @State private var showConnectionError = false
    @State private var inputMode: InputMode = .voice  // .text or .voice
    @State private var textInput: String = ""
    
    enum InputMode {
        case text, voice
    }
    
    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // Messages area
                messagesList
                
                // Bottom input bar (WeChat style)
                inputBar
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("VoiceMate")
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(.visible, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    HStack(spacing: 12) {
                        // Connection status dot
                        Circle()
                            .fill(voiceService.isProcessing ? Color.yellow :
                                  messages.isEmpty ? Color.gray : Color.green)
                            .frame(width: 8, height: 8)
                        
                        Button(action: { showSettings = true }) {
                            Image(systemName: "gearshape.fill")
                                .foregroundColor(.gray)
                        }
                    }
                }
            }
            .sheet(isPresented: $showSettings) {
                SettingsView(service: voiceService)
            }
            .alert("连接失败", isPresented: $showConnectionError) {
                Button("设置", action: { showSettings = true })
                Button("重试") { checkConnection() }
            } message: {
                Text("无法连接到 VoiceMate 服务器，请检查网络和服务器地址。")
            }
            .onAppear {
                checkConnection()
            }
        }
        .preferredColorScheme(.dark)
    }
    
    // MARK: - Messages List
    
    private var messagesList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 4) {
                    if messages.isEmpty {
                        emptyState
                    } else {
                        ForEach(messages) { message in
                            MessageBubble(message: message, voiceService: voiceService)
                                .id(message.id)
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            }
            .onChange(of: messages.count) { _ in
                if let last = messages.last {
                    withAnimation {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
        }
    }
    
    private var emptyState: some View {
        VStack(spacing: 16) {
            Spacer()
            Image(systemName: "waveform.circle.fill")
                .font(.system(size: 72))
                .foregroundColor(.purple.opacity(0.5))
            Text("开始聊天")
                .font(.title3)
                .fontWeight(.semibold)
                .foregroundColor(.gray)
            Text("在下方输入文字或按住麦克风说话")
                .font(.subheadline)
                .foregroundColor(.gray.opacity(0.6))
            Spacer()
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }
    
    // MARK: - Input Bar (WeChat style)
    
    private var inputBar: some View {
        VStack(spacing: 0) {
            Divider().background(Color.gray.opacity(0.3))
            
            HStack(spacing: 8) {
                // "+" button
                Button(action: {}) {
                    Image(systemName: "plus.circle.fill")
                        .font(.title2)
                        .foregroundColor(.gray)
                }
                
                if inputMode == .text {
                    // Text input field
                    textFieldArea
                } else {
                    // Voice recording button ("按住 说话")
                    voiceButtonArea
                }
                
                // Toggle button (switch between text/voice)
                Button(action: {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        inputMode = inputMode == .text ? .voice : .text
                    }
                }) {
                    Image(systemName: inputMode == .text ? "mic.fill" : "keyboard.fill")
                        .font(.system(size: 16))
                        .foregroundColor(.white)
                        .frame(width: 32, height: 32)
                        .background(Color.purple)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .padding(.bottom, 4)
        }
        .background(Color(.systemGray6).opacity(0.95))
    }
    
    // MARK: - Text Input Area
    
    private var textFieldArea: some View {
        HStack(spacing: 6) {
            TextField("输入消息...", text: $textInput)
                .font(.body)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color(.systemGray5))
                .clipShape(RoundedRectangle(cornerRadius: 20))
            
            if !textInput.trimmingCharacters(in: .whitespaces).isEmpty {
                Button(action: sendTextMessage) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.title2)
                        .foregroundColor(.purple)
                }
                .disabled(voiceService.isProcessing)
            }
        }
    }
    
    // MARK: - Voice Recording Area
    
    private var voiceButtonArea: some View {
        Button(action: {
            if audioService.isRecording {
                Task { await sendRecording() }
            }
        }) {
            Text(audioService.isRecording ? "松开 发送" : "按住 说话")
                .font(.body)
                .fontWeight(.medium)
                .foregroundColor(audioService.isRecording ? .white : .primary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 22)
                        .fill(audioService.isRecording ? Color.red : Color(.systemGray5))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 22)
                        .stroke(audioService.isRecording ? Color.red.opacity(0.5) : Color.clear, lineWidth: 2)
                )
        }
        .simultaneousGesture(
            LongPressGesture(minimumDuration: 0.15)
                .onEnded { _ in
                    if !audioService.isRecording && !voiceService.isProcessing {
                        audioService.startRecording()
                        let impact = UIImpactFeedbackGenerator(style: .medium)
                        impact.impactOccurred()
                    }
                }
        )
        .disabled(voiceService.isProcessing)
    }
    
    // MARK: - Send Actions
    
    private func sendTextMessage() {
        let text = textInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        
        textInput = ""
        
        let userMessage = ChatMessage(
            id: UUID(), isUser: true, text: text,
            audioURL: nil, timestamp: Date()
        )
        messages.append(userMessage)
        
        Task {
            do {
                let response = try await voiceService.sendMessage(text: text, conversationId: conversationId)
                await MainActor.run { conversationId = response.conversationId }
                
                let aiMessage = ChatMessage(
                    id: UUID(), isUser: false,
                    text: response.replyText,
                    audioURL: response.audioUrl,
                    timestamp: Date(),
                    duration: TimeInterval(response.durationMs) / 1000.0
                )
                await MainActor.run { messages.append(aiMessage) }
                
                if let audioURL = voiceService.audioURL(for: response.audioUrl) {
                    try? await voiceService.playAudio(from: audioURL)
                }
            } catch {
                await MainActor.run { showConnectionError = true }
            }
        }
    }
    
    private func sendRecording() async {
        let text = await audioService.stopRecording()
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        
        let userMessage = ChatMessage(
            id: UUID(), isUser: true, text: text,
            audioURL: nil, timestamp: Date()
        )
        await MainActor.run { messages.append(userMessage) }
        
        do {
            let response = try await voiceService.sendMessage(text: text, conversationId: conversationId)
            await MainActor.run { conversationId = response.conversationId }
            
            let aiMessage = ChatMessage(
                id: UUID(), isUser: false,
                text: response.replyText,
                audioURL: response.audioUrl,
                timestamp: Date(),
                duration: TimeInterval(response.durationMs) / 1000.0
            )
            await MainActor.run { messages.append(aiMessage) }
            
            if let audioURL = voiceService.audioURL(for: response.audioUrl) {
                try? await voiceService.playAudio(from: audioURL)
            }
        } catch {
            await MainActor.run { showConnectionError = true }
        }
    }
    
    private func checkConnection() {
        Task {
            let healthy = await voiceService.checkHealth()
            if !healthy && !messages.isEmpty {
                await MainActor.run { showConnectionError = true }
            }
        }
    }
}

// MARK: - Message Bubble

struct MessageBubble: View {
    let message: ChatMessage
    @State private var isPlaying = false
    let voiceService: VoiceMateService
    
    var body: some View {
        HStack(alignment: .bottom, spacing: 6) {
            if message.isUser { Spacer(minLength: 60) }
            
            VStack(alignment: message.isUser ? .trailing : .leading, spacing: 4) {
                // Bubble
                Text(message.text)
                    .font(.body)
                    .foregroundColor(.white)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(
                        RoundedRectangle(cornerRadius: 18)
                            .fill(message.isUser ? Color.purple : Color(.systemGray3))
                    )
                
                // AI audio play button
                if !message.isUser, let audioPath = message.audioURL {
                    Button(action: { playAudio(audioPath) }) {
                        HStack(spacing: 6) {
                            Image(systemName: isPlaying ? "stop.fill" : "play.fill")
                                .font(.caption)
                            Text(isPlaying ? "停止" : "语音 \(Int(message.duration))″")
                                .font(.caption)
                        }
                        .foregroundColor(.purple)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 4)
                        .background(Color.purple.opacity(0.1))
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                    }
                }
            }
            
            if !message.isUser { Spacer(minLength: 60) }
        }
        .padding(.vertical, 2)
    }
    
    private func playAudio(_ path: String) {
        isPlaying = true
        if let url = voiceService.audioURL(for: path) {
            Task {
                try? await voiceService.playAudio(from: url)
                try? await Task.sleep(nanoseconds: UInt64(message.duration * 1_000_000_000))
                await MainActor.run { isPlaying = false }
            }
        }
    }
}

// MARK: - Settings View

struct SettingsView: View {
    @ObservedObject var service: VoiceMateService
    @Environment(\.dismiss) var dismiss
    @AppStorage("selected_voice") private var selectedVoice = "zh-CN-XiaoxiaoNeural"
    
    let voices = [
        ("zh-CN-XiaoxiaoNeural", "小晓 (女声)"),
        ("zh-CN-XiaoyiNeural", "小伊 (女声活泼)"),
        ("zh-CN-YunxiNeural", "云希 (男声)"),
        ("zh-CN-YunjianNeural", "云健 (男声成熟)"),
        ("zh-CN-XiaochenNeural", "小辰 (男声文艺)"),
    ]
    
    var body: some View {
        NavigationStack {
            Form {
                Section("服务器配置") {
                    HStack {
                        Text("地址")
                        TextField("服务器 IP", text: $service.serverHost)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                            .autocorrectionDisabled()
                    }
                    HStack {
                        Text("端口")
                        TextField("端口号", text: $service.serverPort)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                    }
                }
                
                Section("语音设置") {
                    Picker("AI 声音", selection: $selectedVoice) {
                        ForEach(voices, id: \.0) { voice in
                            Text(voice.1).tag(voice.0)
                        }
                    }
                    Text("选择 AI 回复时使用的语音")
                        .font(.caption)
                        .foregroundColor(.gray)
                }
                
                Section {
                    Button("测试连接") {
                        Task {
                            let ok = await service.checkHealth()
                            await MainActor.run {
                                if ok { dismiss() }
                            }
                        }
                    }
                }
                
                Section("关于") {
                    HStack {
                        Text("版本")
                        Spacer()
                        Text("1.0.0").foregroundColor(.gray)
                    }
                }
            }
            .navigationTitle("设置")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("完成") { dismiss() }
                }
            }
        }
        .onChange(of: selectedVoice) { newVoice in
            service.selectedVoice = newVoice
        }
    }
}

#Preview {
    ContentView()
}
