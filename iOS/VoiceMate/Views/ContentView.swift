import SwiftUI

struct ContentView: View {
    @StateObject private var voiceService = VoiceMateService()
    @StateObject private var audioService = AudioService()
    
    @State private var messages: [ChatMessage] = []
    @State private var conversationId: String?
    @State private var showSettings = false
    @State private var showConnectionError = false
    
    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // Connection status bar
                connectionBar
                
                // Messages area
                messagesList
                
                // Recording area
                recordingArea
            }
            .background(Color.black)
            .navigationTitle("VoiceMate")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button(action: { showSettings = true }) {
                        Image(systemName: "gear")
                            .foregroundColor(.purple)
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
    
    // MARK: - Connection Bar
    
    private var connectionBar: some View {
        HStack {
            Circle()
                .fill(voiceService.isProcessing ? Color.yellow :
                      messages.isEmpty ? Color.gray : Color.green)
                .frame(width: 8, height: 8)
            Text(voiceService.isProcessing ? "处理中..." :
                 messages.isEmpty ? "点击录音开始聊天" : "已连接")
                .font(.caption)
                .foregroundColor(.gray)
            Spacer()
        }
        .padding(.horizontal)
        .padding(.vertical, 6)
    }
    
    // MARK: - Messages List
    
    private var messagesList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                if messages.isEmpty {
                    emptyState
                } else {
                    LazyVStack(spacing: 12) {
                        ForEach(messages) { message in
                            MessageBubble(message: message, voiceService: voiceService)
                                .id(message.id)
                        }
                    }
                    .padding()
                }
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
        VStack(spacing: 20) {
            Spacer()
            
            Image(systemName: "waveform.circle.fill")
                .font(.system(size: 80))
                .foregroundColor(.purple.opacity(0.6))
            
            Text("按住下方按钮开始聊天")
                .font(.title3)
                .foregroundColor(.gray)
            
            Text("我会用语音回复你")
                .font(.subheadline)
                .foregroundColor(.gray.opacity(0.6))
            
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }
    
    // MARK: - Recording Area
    
    private var recordingArea: some View {
        VStack(spacing: 0) {
            Divider()
                .background(Color.purple.opacity(0.3))
            
            HStack(spacing: 16) {
                // Transcribed text preview
                if audioService.isRecording || !audioService.transcribedText.isEmpty {
                    Text(audioService.transcribedText.isEmpty ? "正在听..." : audioService.transcribedText)
                        .font(.body)
                        .foregroundColor(.white.opacity(0.8))
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                
                Spacer()
                
                // Record button
                recordButton
            }
            .padding(.horizontal)
            .padding(.vertical, 12)
            .background(Color.black)
        }
    }
    
    private var recordButton: some View {
        Button(action: {
            if audioService.isRecording {
                Task { await sendRecording() }
            }
        }) {
            ZStack {
                Circle()
                    .fill(audioService.isRecording ? Color.red : Color.purple)
                    .frame(width: 56, height: 56)
                
                if audioService.isRecording {
                    // Stop icon (square)
                    RoundedRectangle(cornerRadius: 4)
                        .fill(Color.white)
                        .frame(width: 20, height: 20)
                } else {
                    // Mic icon
                    Image(systemName: "mic.fill")
                        .font(.title2)
                        .foregroundColor(.white)
                }
            }
        }
        .simultaneousGesture(
            LongPressGesture(minimumDuration: 0.1)
                .onEnded { _ in
                    if !audioService.isRecording {
                        audioService.startRecording()
                        let impact = UIImpactFeedbackGenerator(style: .medium)
                        impact.impactOccurred()
                    }
                }
        )
        .scaleEffect(audioService.isRecording ? 1.1 : 1.0)
        .animation(.spring(response: 0.3), value: audioService.isRecording)
        .disabled(voiceService.isProcessing)
    }
    
    // MARK: - Actions
    
    private func sendRecording() async {
        let text = await audioService.stopRecording()
        
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return
        }
        
        // Add user message
        let userMessage = ChatMessage(
            id: UUID(),
            isUser: true,
            text: text,
            audioURL: nil,
            timestamp: Date()
        )
        await MainActor.run {
            messages.append(userMessage)
        }
        
        // Send to backend
        do {
            let response = try await voiceService.sendMessage(
                text: text,
                conversationId: conversationId
            )
            
            await MainActor.run {
                conversationId = response.conversationId
            }
            
            // Add AI message
            let aiMessage = ChatMessage(
                id: UUID(),
                isUser: false,
                text: response.replyText,
                audioURL: response.audioUrl,
                timestamp: Date(),
                duration: TimeInterval(response.durationMs) / 1000.0
            )
            await MainActor.run {
                messages.append(aiMessage)
            }
            
            // Auto-play the AI's voice reply
            if let audioURL = voiceService.audioURL(for: response.audioUrl) {
                try? await voiceService.playAudio(from: audioURL)
            }
        } catch {
            await MainActor.run {
                showConnectionError = true
            }
        }
    }
    
    private func checkConnection() {
        Task {
            let healthy = await voiceService.checkHealth()
            if !healthy && !messages.isEmpty {
                await MainActor.run {
                    showConnectionError = true
                }
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
        HStack {
            if message.isUser { Spacer() }
            
            VStack(alignment: message.isUser ? .trailing : .leading, spacing: 4) {
                // Voice indicator / Text display
                HStack(spacing: 8) {
                    if !message.isUser {
                        // AI message - play button
                        Button(action: { playAudio() }) {
                            Image(systemName: isPlaying ? "stop.circle.fill" : "play.circle.fill")
                                .font(.title2)
                                .foregroundColor(.purple)
                        }
                        
                        // Waveform animation when playing
                        if isPlaying {
                            HStack(spacing: 3) {
                                ForEach(0..<4) { i in
                                    RoundedRectangle(cornerRadius: 2)
                                        .fill(Color.purple)
                                        .frame(width: 3, height: 12 + CGFloat.random(in: 4...16))
                                        .animation(
                                            .easeInOut(duration: 0.5).repeatForever().delay(Double(i) * 0.1),
                                            value: isPlaying
                                        )
                                }
                            }
                        }
                    }
                    
                    // Text
                    Text(message.text)
                        .font(.body)
                        .foregroundColor(message.isUser ? .white : .white.opacity(0.9))
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(
                            RoundedRectangle(cornerRadius: 16)
                                .fill(message.isUser ? Color.purple : Color.gray.opacity(0.2))
                        )
                }
                
                // Timestamp
                Text(message.timestamp, style: .time)
                    .font(.caption2)
                    .foregroundColor(.gray.opacity(0.5))
            }
            
            if !message.isUser { Spacer() }
        }
    }
    
    private func playAudio() {
        isPlaying.toggle()
        if isPlaying, let audioPath = message.audioURL, let url = voiceService.audioURL(for: audioPath) {
            Task {
                try? await voiceService.playAudio(from: url)
                // Reset after playing
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
    
    var body: some View {
        NavigationStack {
            Form {
                Section("服务器配置") {
                    HStack {
                        Text("地址")
                        TextField("服务器 IP", text: $service.serverHost)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                    }
                    
                    HStack {
                        Text("端口")
                        TextField("端口号", text: $service.serverPort)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                    }
                }
                
                Section {
                    Button("测试连接") {
                        Task {
                            let ok = await service.checkHealth()
                            await MainActor.run {
                                if ok {
                                    dismiss()
                                }
                            }
                        }
                    }
                }
                
                Section("关于") {
                    HStack {
                        Text("版本")
                        Spacer()
                        Text("1.0.0")
                            .foregroundColor(.gray)
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
    }
}
