import SwiftUI

struct ContentView: View {
    @StateObject private var voiceService = VoiceMateService()
    @StateObject private var audioService = AudioService()
    
    @AppStorage("selected_voice") private var selectedVoice = "zh-CN-XiaoxiaoNeural"
    private let messagesKey = "chat_messages"
    private let convIdKey = "conversation_id"
    
    @State private var messages: [ChatMessage] = []
    @State private var conversationId: String?
    @State private var showSettings = false
    @State private var showConnectionError = false
    @State private var inputMode: InputMode = .voice
    @State private var textInput: String = ""
    @State private var streamingMessageId: UUID? = nil
    @State private var showEmotion: String? = nil
    @State private var emotionMessageId: UUID? = nil
    
    // Long-press recording state
    @State private var isPressingForRecording = false
    
    // Selection mode
    @State private var isSelecting = false
    @State private var selectedIds = Set<UUID>()

    
    // Keyboard handling
    @FocusState private var isTextFieldFocused: Bool
    
    // Proactive timer
    @State private var proactiveTimer: Timer? = nil
    @AppStorage("proactive_enabled") private var proactiveEnabled = true
    
    // Auto-play tracking: prevents replaying the same audio message
    @State private var lastAutoPlayedMessageId: UUID?
    
    // Real-time call
    @State private var showRealtimeCall = false
    
    enum InputMode {
        case text, voice
    }
    
    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // Messages area
                messagesList
                
                // Bottom bar (input or selection mode)
                if isSelecting {
                    selectionBottomBar
                } else {
                    inputBar
                }
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("妤妤")
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
            .fullScreenCover(isPresented: $showRealtimeCall) {
                RealtimeCallView(
                    serverHost: voiceService.serverHost,
                    serverPort: voiceService.serverPort,
                    voice: voiceService.selectedVoice,
                    persona: voiceService.selectedPersona,
                    speed: voiceService.speechSpeed,
                    onTurnCompleted: { [self] isUser, text in
                        self.appendMessage(isUser: isUser, text: text)
                    }
                )
            }
            .alert("连接失败", isPresented: $showConnectionError) {
                Button("设置", action: { showSettings = true })
                Button("重试") { checkConnection() }
            } message: {
                Text("无法连接到 VoiceMate 服务器，请检查网络和服务器地址。")
            }
            .onAppear {
                loadMessages()
                checkConnection()
                startProactiveTimer()
            }
            .onDisappear {
                proactiveTimer?.invalidate()
                proactiveTimer = nil
            }
            .onChange(of: proactiveEnabled) { _ in
                startProactiveTimer()
            }
        }
        .preferredColorScheme(.dark)
        // Recording overlay (WeChat style)
        .overlay(recordingOverlay)
        // Tap background to dismiss keyboard
        .onTapGesture { UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil) }
        // Emotion animation overlay
        .overlay(emotionOverlay)
    }
    
    // MARK: - Selection Bottom Bar
    
    private var selectionBottomBar: some View {
        VStack(spacing: 0) {
            Divider().background(Color.gray.opacity(0.3))
            
            HStack {
                Button("取消") {
                    exitSelectMode()
                }
                .foregroundColor(.purple)
                
                Spacer()
                
                Button("全选") {
                    selectAll()
                }
                .foregroundColor(.purple)
                
                Spacer()
                
                Button("删除 (\(selectedIds.count))", role: .destructive) {
                    deleteSelected()
                }
                .disabled(selectedIds.isEmpty)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
        }
        .background(Color(.systemGray6).opacity(0.95))
    }
    
    // MARK: - Messages List
    
    private var messagesList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Color.clear
                    .frame(height: 0)
                    .id("keyboard_scroll_anchor")
                
                LazyVStack(spacing: 4) {
                    if messages.isEmpty {
                        emptyState
                    } else {
                        ForEach(messages) { message in
                            HStack(spacing: 8) {
                                if isSelecting {
                                    Image(systemName: selectedIds.contains(message.id) ? "checkmark.circle.fill" : "circle")
                                        .foregroundColor(.purple)
                                        .font(.title3)
                                        .onTapGesture {
                                            toggleSelection(message.id)
                                        }
                                }
                                
                                if message.id == streamingMessageId {
                                    MessageBubble(message: message, voiceService: voiceService, streamingText: voiceService.streamingText, onDelete: { deleteMessage(message) }, isSelecting: isSelecting, onMultiSelect: { enterSelectMode() }, onSelect: { toggleSelection(message.id) })
                                        .id(message.id)
                                } else {
                                    MessageBubble(message: message, voiceService: voiceService, streamingText: nil, onDelete: { deleteMessage(message) }, isSelecting: isSelecting, onMultiSelect: { enterSelectMode() }, onSelect: { toggleSelection(message.id) })
                                        .id(message.id)
                                }
                            }
                            .padding(.leading, isSelecting ? 0 : 0)
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            }
            .scrollDismissesKeyboard(.immediately)
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
                if inputMode == .text {
                    // Text input field
                    textFieldArea
                } else {
                    // Voice recording button ("按住 说话")
                    // Tap → switch to text mode, Long press → record voice
                    voiceButtonArea
                }
                
                // Real-time call button
                Button(action: { showRealtimeCall = true }) {
                    Image(systemName: "phone.fill")
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
            // Tap to switch back to voice recording mode
            Button(action: {
                withAnimation(.easeInOut(duration: 0.2)) {
                    inputMode = .voice
                    isTextFieldFocused = false
                    UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                }
            }) {
                Image(systemName: "mic.fill")
                    .font(.system(size: 16))
                    .foregroundColor(.purple)
                    .frame(width: 32, height: 32)
                    .background(Color(.systemGray5))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            
            TextField("输入消息...", text: $textInput)
                .font(.body)
                .onLongPressGesture(minimumDuration: 0.3) {
                    let impact = UIImpactFeedbackGenerator(style: .light)
                    impact.impactOccurred()
                    withAnimation(.easeInOut(duration: 0.2)) {
                        inputMode = .voice
                        isTextFieldFocused = false
                        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                    }
                }
                .focused($isTextFieldFocused)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color(.systemGray5))
                .clipShape(RoundedRectangle(cornerRadius: 20))
                .submitLabel(.send)
                .onSubmit(sendTextMessage)
            
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
            .opacity(voiceService.isProcessing ? 0.5 : 1.0)
            .allowsHitTesting(!voiceService.isProcessing)
            .onTapGesture {
                // Tap → switch to text input mode (show keyboard)
                withAnimation(.easeInOut(duration: 0.2)) {
                    inputMode = .text
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                        isTextFieldFocused = true
                    }
                }
            }
            .simultaneousGesture(
                LongPressGesture(minimumDuration: 0.15)
                    .onEnded { _ in
                        if !audioService.isRecording && !voiceService.isProcessing {
                            audioService.startRecording()
                            isPressingForRecording = true
                            let impact = UIImpactFeedbackGenerator(style: .medium)
                            impact.impactOccurred()
                        }
                    }
                    .sequenced(before: DragGesture(minimumDistance: 0)
                        .onEnded { _ in
                            if audioService.isRecording && isPressingForRecording {
                                Task {
                                    await sendRecording()
                                    isPressingForRecording = false
                                }
                            }
                        }
                    )
            )
    }
    
    // MARK: - Recording Overlay (WeChat style)
    
    @ViewBuilder
    private var recordingOverlay: some View {
        if audioService.isRecording {
            Color.black.opacity(0.5)
                .ignoresSafeArea()
                .overlay(
                    VStack(spacing: 20) {
                        Image(systemName: "mic.fill")
                            .font(.system(size: 80))
                            .foregroundColor(.white)
                        
                        Text("松开 发送")
                            .font(.headline)
                            .foregroundColor(.white)
                    }
                )
                .transition(.opacity)
                .animation(.easeInOut(duration: 0.2), value: audioService.isRecording)
        }
    }
    
    // MARK: - Emotion Animation
    
    @ViewBuilder
    private var emotionOverlay: some View {
        if let emotion = showEmotion {
            Color.clear
                .overlay(
                    VStack {
                        Spacer()
                        HStack {
                            Spacer()
                            switch emotion {
                            case "affectionate": Text("❤️").font(.system(size: 64)).transition(.scale)
                            case "cheerful": Text("✨").font(.system(size: 64)).transition(.scale)
                            case "sad": Text("🥺").font(.system(size: 64)).transition(.scale)
                            case "angry": Text("😤").font(.system(size: 64)).transition(.scale)
                            case "embarrassed": Text("☺️").font(.system(size: 64)).transition(.scale)
                            default: EmptyView()
                            }
                            Spacer()
                        }
                        .padding(.bottom, 160)
                    }
                )
                .onAppear {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                        withAnimation { showEmotion = nil }
                    }
                }
        }
    }
    
    // MARK: - Chat History Persistence
    
    private func saveMessages() {
        if let encoded = try? JSONEncoder().encode(messages) {
            UserDefaults.standard.set(encoded, forKey: messagesKey)
        }
        UserDefaults.standard.set(conversationId, forKey: convIdKey)
    }
    
    private func loadMessages() {
        if let data = UserDefaults.standard.data(forKey: messagesKey),
           let decoded = try? JSONDecoder().decode([ChatMessage].self, from: data) {
            messages = decoded
        }
        conversationId = UserDefaults.standard.string(forKey: convIdKey)
    }
    
    // MARK: - Send Actions
    
    private func sendTextMessage() {
        let text = textInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        textInput = ""
        
        let user = ChatMessage(id: UUID(), isUser: true, text: text, audioURL: nil, timestamp: Date())
        messages.append(user)
        saveMessages()
        
        let aiId = UUID()
        streamingMessageId = aiId
        messages.append(ChatMessage(id: aiId, isUser: false, text: "", audioURL: nil, timestamp: Date()))
        
        Task {
            do {
                let r = try await voiceService.sendMessageStream(text: text, conversationId: conversationId)
                await MainActor.run {
                    conversationId = r.conversationId
                    streamingMessageId = nil
                    if let idx = messages.firstIndex(where: { $0.id == aiId }) {
                        messages[idx].text = r.replyText
                        messages[idx].audioURL = r.audioUrl
                        messages[idx].duration = TimeInterval(r.durationMs) / 1000.0
                    }
                    saveMessages()
                    if let emotion = r.emotion { showEmotion = emotion }
                }
                if let url = voiceService.audioURL(for: r.audioUrl) {
                    // Mark as playing and auto-play
                    await MainActor.run {
                        if let idx = messages.firstIndex(where: { $0.id == aiId }) {
                            messages[idx].isPlaying = true
                            lastAutoPlayedMessageId = aiId
                        }
                    }
                    try? await voiceService.playAudio(from: url, remotePath: r.audioUrl)
                    await MainActor.run {
                        if let idx = messages.firstIndex(where: { $0.id == aiId }) {
                            messages[idx].isPlaying = false
                        }
                    }
                }
            } catch {
                // WebSocket failed - fall back to REST API
                await MainActor.run { streamingMessageId = nil }
                if let idx = messages.firstIndex(where: { $0.id == aiId }) {
                    messages.remove(at: idx)
                }
                // Retry with REST API
                if let fallback = try? await voiceService.sendMessage(text: text, conversationId: conversationId) {
                    await MainActor.run {
                        conversationId = fallback.conversationId
                        let msg = ChatMessage(id: UUID(), isUser: false, text: fallback.replyText, audioURL: fallback.audioUrl, timestamp: Date(), duration: TimeInterval(fallback.durationMs) / 1000.0)
                        messages.append(msg)
                        saveMessages()
                        lastAutoPlayedMessageId = msg.id
                    }
                    if let url = voiceService.audioURL(for: fallback.audioUrl) {
                        // For fallback, find the message by iterating
                        let fbAudioUrl = fallback.audioUrl
                        await MainActor.run {
                            if let idx = messages.firstIndex(where: { $0.audioURL == fbAudioUrl }) {
                                messages[idx].isPlaying = true
                            }
                        }
                        try? await voiceService.playAudio(from: url, remotePath: fallback.audioUrl)
                        await MainActor.run {
                            if let idx = messages.firstIndex(where: { $0.audioURL == fbAudioUrl }) {
                                messages[idx].isPlaying = false
                            }
                        }
                    }
                }
            }
        }
    }
    
    private func sendRecording() async {
        let text = await audioService.stopRecording()
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        
        let user = ChatMessage(id: UUID(), isUser: true, text: text, audioURL: nil, timestamp: Date())
        await MainActor.run { messages.append(user); saveMessages() }
        
        let aiId = UUID()
        await MainActor.run { streamingMessageId = aiId }
        await MainActor.run { messages.append(ChatMessage(id: aiId, isUser: false, text: "", audioURL: nil, timestamp: Date())) }
        
        do {
            let r = try await voiceService.sendMessageStream(text: text, conversationId: conversationId)
            await MainActor.run {
                conversationId = r.conversationId
                streamingMessageId = nil
                if let idx = messages.firstIndex(where: { $0.id == aiId }) {
                    messages[idx].text = r.replyText
                    messages[idx].audioURL = r.audioUrl
                    messages[idx].duration = TimeInterval(r.durationMs) / 1000.0
                }
                saveMessages()
                if let emotion = r.emotion { showEmotion = emotion }
            }
            if let url = voiceService.audioURL(for: r.audioUrl) {
                // Auto-play with isPlaying state tracking
                await MainActor.run {
                    if let idx = messages.firstIndex(where: { $0.id == aiId }) {
                        messages[idx].isPlaying = true
                        lastAutoPlayedMessageId = aiId
                    }
                }
                try? await voiceService.playAudio(from: url, remotePath: r.audioUrl)
                await MainActor.run {
                    if let idx = messages.firstIndex(where: { $0.id == aiId }) {
                        messages[idx].isPlaying = false
                    }
                }
            }
        } catch {
            await MainActor.run { streamingMessageId = nil }
            if let idx = messages.firstIndex(where: { $0.id == aiId }) {
                _ = await MainActor.run { messages.remove(at: idx) }
            }
            if let fb = try? await voiceService.sendMessage(text: text, conversationId: conversationId) {
                await MainActor.run {
                    conversationId = fb.conversationId
                    let m = ChatMessage(id: UUID(), isUser: false, text: fb.replyText, audioURL: fb.audioUrl, timestamp: Date(), duration: TimeInterval(fb.durationMs) / 1000.0)
                    messages.append(m)
                    saveMessages()
                    if let emotion = fb.emotion { showEmotion = emotion }
                }
                if let url = voiceService.audioURL(for: fb.audioUrl) {
                    await MainActor.run {
                        if let idx = messages.firstIndex(where: { $0.audioURL == fb.audioUrl }) {
                            messages[idx].isPlaying = true
                        }
                    }
                    try? await voiceService.playAudio(from: url, remotePath: fb.audioUrl)
                    await MainActor.run {
                        if let idx = messages.firstIndex(where: { $0.audioURL == fb.audioUrl }) {
                            messages[idx].isPlaying = false
                        }
                    }
                }
            }
        }
    }
    
    // MARK: - Proactive Timer
    
    /// Start the proactive timer that periodically checks for AI-initiated messages
    private func startProactiveTimer() {
        proactiveTimer?.invalidate()
        let interval: TimeInterval = proactiveEnabled ? 180 : 0 // 3 minutes
        guard interval > 0 else { return }
        proactiveTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { _ in
            Task { await self.fireProactive() }
        }
    }
    
    /// Fire a proactive check to the backend
    private func fireProactive() async {
        guard proactiveEnabled else { return }
        guard let url = URL(string: "http://\(voiceService.serverHost):\(voiceService.serverPort)/v1/proactive?persona=\(voiceService.selectedPersona)") else { return }
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else { return }
            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let text = json["text"] as? String, !text.isEmpty {
                let emotion = json["emotion"] as? String
                await MainActor.run {
                    let msg = ChatMessage(id: UUID(), isUser: false, text: text, audioURL: nil, timestamp: Date())
                    messages.append(msg)
                    saveMessages()
                    if let e = emotion { showEmotion = e }
                }
            }
        } catch {
            // Silently ignore proactive failures — not critical
        }
    }
    
    // MARK: - Connection Check
    
    
    private func deleteMessage(_ message: ChatMessage) {
        // Delete cached audio file
        if let audioPath = message.audioURL {
            let localURL = AudioCache.localURL(for: audioPath)
            try? FileManager.default.removeItem(at: localURL)
        }
        // Clear streaming state if deleting the currently-streaming message
        if message.id == streamingMessageId {
            streamingMessageId = nil
            voiceService.streamingText = ""
        }
        // Remove from array
        withAnimation {
            messages.removeAll { $0.id == message.id }
        }
        saveMessages()
    }

    // MARK: - Selection Mode
    
    private func enterSelectMode() {
        isSelecting = true
        selectedIds = []
    }
    
    private func exitSelectMode() {
        isSelecting = false
        selectedIds = []
    }
    
    private func toggleSelection(_ id: UUID) {
        if selectedIds.contains(id) {
            selectedIds.remove(id)
        } else {
            selectedIds.insert(id)
        }
    }
    
    private func selectAll() {
        selectedIds = Set(messages.map { $0.id })
    }
    
    private func deleteSelected() {
        for message in messages {
            if selectedIds.contains(message.id) {
                // Delete cached audio file
                if let audioPath = message.audioURL {
                    let localURL = AudioCache.localURL(for: audioPath)
                    try? FileManager.default.removeItem(at: localURL)
                }
                // Clear streaming state if deleting the currently-streaming message
                if message.id == streamingMessageId {
                    streamingMessageId = nil
                    voiceService.streamingText = ""
                }
            }
        }
        withAnimation {
            messages.removeAll { selectedIds.contains($0.id) }
        }
        saveMessages()
        exitSelectMode()
    }
    
    private func checkConnection() {
        Task {
            let healthy = await voiceService.checkHealth()
            if !healthy && !messages.isEmpty {
                await MainActor.run { showConnectionError = true }
            }
        }
    }

    /// Append a single ChatMessage from the real-time call and persist
    private func appendMessage(isUser: Bool, text: String) {
        let msg = ChatMessage(
            id: UUID(),
            isUser: isUser,
            text: text,
            audioURL: nil,
            timestamp: Date()
        )
        messages.append(msg)
        saveMessages()
    }
}

// MARK: - Message Bubble

struct MessageBubble: View {
    let message: ChatMessage
    @State private var isPlaying = false
    let voiceService: VoiceMateService
    let streamingText: String?
    let onDelete: (() -> Void)?
    let isSelecting: Bool
    let onMultiSelect: (() -> Void)?
    let onSelect: (() -> Void)?
    
    var body: some View {
        HStack(alignment: .bottom, spacing: 6) {
            if message.isUser { Spacer(minLength: 60) }
            
            VStack(alignment: message.isUser ? .trailing : .leading, spacing: 4) {
                if message.isUser {
                    // User message -> text bubble (unchanged)
                    userTextBubble
                } else if streamingText != nil {
                    // AI message in streaming state -> show typing text
                    aiStreamingBubble
                } else if message.audioURL != nil {
                    // AI message with audio, not streaming -> voice waveform bubble
                    aiVoiceBubble
                } else {
                    // AI message without audio (proactive, etc.) -> text bubble
                    aiTextBubble
                }
            }
            .contentShape(Rectangle())
            .onTapGesture {
                if isSelecting {
                    onSelect?()
                }
            }
            
            if !message.isUser { Spacer(minLength: 60) }
        }
        .padding(.vertical, 2)
        .contextMenu {
            if !isSelecting {
                Button(role: .destructive) {
                    onDelete?()
                } label: {
                    Label("\u{5220}\u{9664}", systemImage: "trash")
                }
                Button {
                    onMultiSelect?()
                } label: {
                    Label("\u{591A}\u{9009}", systemImage: "checkmark.circle")
                }
            }
        }
    }
    
    // MARK: - Bubble Styles
    
    /// User text message bubble
    private var userTextBubble: some View {
        Text(message.text.isEmpty ? "..." : message.text)
            .font(.body)
            .foregroundColor(.white)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 18)
                    .fill(Color.purple)
            )
    }
    
    /// AI streaming text bubble (typing effect)
    private var aiStreamingBubble: some View {
        let displayText = streamingText ?? message.text
        return Text(displayText.isEmpty ? "..." : displayText)
            .font(.body)
            .foregroundColor(.white)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 18)
                    .fill(Color(.systemGray3))
            )
    }
    
    /// AI text bubble (no audio, e.g. proactive messages)
    private var aiTextBubble: some View {
        Text(message.text.isEmpty ? "..." : message.text)
            .font(.body)
            .foregroundColor(.white)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 18)
                    .fill(Color(.systemGray3))
            )
    }
    
    /// AI voice waveform bubble (WeChat style)
    private var aiVoiceBubble: some View {
        Button(action: { playAudio(message.audioURL ?? "") }) {
            HStack(spacing: 8) {
                // Waveform icon
                Image(systemName: "waveform")
                    .font(.system(size: 16, weight: .medium))
                    .foregroundColor(.white)
                
                // Duration in seconds (e.g. " 3″")
                Text(" \(Int(message.duration))″")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundColor(.white.opacity(0.8))
            }
            .padding(.leading, 14)
            .padding(.trailing, 16)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 18)
                    .fill(Color(.systemGray3))
            )
        }
        .buttonStyle(.plain)
        .disabled(isSelecting)
    }
    
    private func playAudio(_ path: String) {
        guard !path.isEmpty else { return }
        if isPlaying {
            // Stop playback
            voiceService.stopAudio()
            isPlaying = false
        } else {
            // Start playback
            isPlaying = true
            if let url = voiceService.audioURL(for: path) {
                Task {
                    do {
                        try await voiceService.playAudio(from: url, remotePath: path)
                        try await Task.sleep(nanoseconds: UInt64(message.duration * 1_000_000_000))
                    } catch { }
                    await MainActor.run { isPlaying = false }
                }
            } else {
                isPlaying = false
            }
        }
}
}


// MARK: - Settings View

struct SettingsView: View {
    @ObservedObject var service: VoiceMateService
    @Environment(\.dismiss) var dismiss
    @AppStorage("proactive_enabled") private var proactiveEnabled = true
    @AppStorage("selected_voice") private var selectedVoice = "zh-CN-XiaoxiaoNeural"
    
    @AppStorage("cloned_voice_id") private var clonedVoiceId = ""
    
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
                        // Standard edge-tts voices
                        Section(header: Text("标准语音")) {
                            Text("小晓 (温柔女声)").tag("zh-CN-XiaoxiaoNeural")
                            Text("小伊 (活泼女声)").tag("zh-CN-XiaoyiNeural")
                            Text("小萱 (软萌女声)").tag("zh-CN-XiaoxuanNeural")
                        }
                        // Cloned voice if available
                        if !clonedVoiceId.isEmpty {
                            Section(header: Text("克隆声音")) {
                                Text("我的声音").tag(clonedVoiceId)
                            }
                        }
                    }
                    Text("选择 AI 回复时使用的语音")
                        .font(.caption)
                        .foregroundColor(.gray)
                }
                Section("主动推送") {
                    Toggle("启用主动推送", isOn: $proactiveEnabled)
                    Text("开启后，AI 会每隔 3 分钟主动发起对话")
                        .font(.caption)
                        .foregroundColor(.gray)
                }
                
                Section("语速") {
                    VStack {
                        Slider(value: $service.speechSpeed, in: 0.5...2.0, step: 0.1)
                        HStack {
                            Text("慢").foregroundColor(.gray)
                            Spacer()
                            Text("\(service.speechSpeed, specifier: "%.1f")x")
                            Spacer()
                            Text("快").foregroundColor(.gray)
                        }
                        .font(.caption)
                    }
                }
                
                Section("人设") {
                    Picker("性格", selection: $service.selectedPersona) {
                        Text("🧡 恋爱脑").tag("love")
                        Text("💛 温暖陪聊").tag("warm")
                        Text("💜 知心姐姐").tag("sister")
                        Text("🔥 毒舌傲娇").tag("tsundere")
                        Text("🌸 元气少女").tag("genki")
                    }
                    Text("切换后新对话会使用新人设")
                        .font(.caption)
                        .foregroundColor(.gray)
                }
                
                Section("声音克隆") {
                    NavigationLink("录制声音样本", destination: VoiceCloneView())
                        .foregroundColor(.purple)
                    Text("录制几段你的声音，AI 就能用你的声音回复")
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
