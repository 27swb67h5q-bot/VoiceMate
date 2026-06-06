import SwiftUI

struct ContentView: View {
    @StateObject private var service = VoiceMateService()
    @StateObject private var audio = AudioService()
    @State private var messages: [ChatMessage] = [
        ChatMessage(role: .assistant, text: "我在。你可以打字，也可以按住麦克风说一句。")
    ]
    @State private var inputText = ""
    @State private var conversationId: String?
    @State private var showSettings = false
    @State private var showRealtimeCall = false
    @State private var showClone = false
    @State private var companionState = "在听"
    @FocusState private var inputFocused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                companionStatusBar
                messageList
                composer
            }
            .background(Color(red: 0.93, green: 0.93, blue: 0.94))
            .navigationTitle("VoiceMate")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button {
                        showRealtimeCall = true
                        inputFocused = false
                    } label: {
                        Image(systemName: "phone.fill")
                    }
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button {
                        showSettings = true
                        inputFocused = false
                    } label: {
                        Image(systemName: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $showSettings) {
                SettingsView(service: service, showClone: $showClone)
            }
            .sheet(isPresented: $showClone) {
                VoiceCloneView()
            }
            .fullScreenCover(isPresented: $showRealtimeCall) {
                RealtimeCallView(
                    serverHost: service.serverHost,
                    serverPort: service.serverPort,
                    voice: service.selectedVoice,
                    persona: service.selectedPersona,
                    speed: service.speechSpeed,
                    onTranscript: appendTranscript,
                    onTurnCompleted: appendTurn
                )
            }
            .onChange(of: audio.transcribedText) { text in
                inputText = text
            }
            .task {
                if let text = await service.fetchProactiveText() {
                    messages.append(ChatMessage(role: .assistant, text: text))
                }
            }
        }
    }

    private var companionStatusBar: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(service.isBusy ? Color.green : Color.secondary.opacity(0.45))
                .frame(width: 8, height: 8)
            Text(service.isBusy ? "正在想怎么回应你" : companionState)
                .font(.footnote)
                .foregroundStyle(.secondary)
            Spacer()
            Button {
                showRealtimeCall = true
                inputFocused = false
            } label: {
                Label("通话", systemImage: "phone.fill")
                    .font(.footnote.weight(.medium))
            }
            .buttonStyle(.plain)
            .foregroundStyle(.green)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Color(red: 0.97, green: 0.97, blue: 0.98))
    }

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 10) {
                    ForEach(messages) { message in
                        MessageBubble(
                            message: message,
                            play: { play(message) }
                        )
                        .id(message.id)
                    }

                    if service.isBusy {
                        HStack {
                            Text("正在回复...")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                            Spacer()
                        }
                        .padding(.horizontal, 16)
                        .id("busy")
                    }
                }
                .padding(.vertical, 12)
            }
            .onChange(of: messages.count) { _ in
                scrollToBottom(proxy)
            }
            .onChange(of: service.isBusy) { _ in
                scrollToBottom(proxy)
            }
        }
    }

    private var composer: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: 8) {
                Button {
                    toggleVoiceInput()
                } label: {
                    Image(systemName: audio.isRecording ? "stop.circle.fill" : "mic.circle")
                        .font(.system(size: 28))
                        .foregroundStyle(audio.isRecording ? .red : .primary)
                }
                .frame(width: 36, height: 36)

                TextField("输入消息", text: $inputText)
                    .lineLimit(1)
                    .textFieldStyle(.plain)
                    .focused($inputFocused)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                    .background(Color.white, in: RoundedRectangle(cornerRadius: 6))
                    .submitLabel(.send)
                    .onSubmit { sendText() }

                Button {
                    sendText()
                } label: {
                    Text("发送")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(canSend ? .white : .secondary)
                        .padding(.horizontal, 12)
                        .frame(height: 36)
                        .background(canSend ? Color.green : Color.gray.opacity(0.18), in: RoundedRectangle(cornerRadius: 6))
                }
                .disabled(!canSend)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(Color(red: 0.96, green: 0.96, blue: 0.97))

            if audio.isRecording {
                HStack(spacing: 8) {
                    ProgressView(value: audio.level)
                        .progressViewStyle(.linear)
                    Text("正在听")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 14)
                .padding(.bottom, 8)
                .background(Color(red: 0.96, green: 0.96, blue: 0.97))
            }
        }
    }

    private var canSend: Bool {
        !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !service.isBusy
    }

    private func toggleVoiceInput() {
        inputFocused = false
        if audio.isRecording {
            audio.stop()
        } else {
            audio.start()
        }
    }

    private func sendText() {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !service.isBusy else { return }
        inputText = ""
        audio.stop()

        messages.append(ChatMessage(role: .user, text: text))

        Task {
            do {
                let response = try await service.sendMessage(text, conversationId: conversationId)
                conversationId = response.conversationId
                companionState = companionLabel(response.emotion)
                let assistant = ChatMessage(role: .assistant, text: response.replyText, audioURL: response.audioUrl)
                messages.append(assistant)
                try? await service.playAudio(path: response.audioUrl)
            } catch {
                service.errorMessage = error.localizedDescription
                messages.append(ChatMessage(role: .assistant, text: "我这边连接不太顺：\(error.localizedDescription)"))
            }
        }
    }

    private func play(_ message: ChatMessage) {
        guard let audioURL = message.audioURL else { return }
        Task { try? await service.playAudio(path: audioURL) }
    }

    private func appendTranscript(_ transcript: [(isUser: Bool, text: String)]) {
        transcript.forEach { appendTurn(isUser: $0.isUser, text: $0.text) }
    }

    private func appendTurn(isUser: Bool, text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if !isUser {
            companionState = "刚刚回应过你"
        }
        messages.append(ChatMessage(role: isUser ? .user : .assistant, text: trimmed))
    }

    private func companionLabel(_ emotion: String?) -> String {
        switch emotion {
        case "comforting":
            return "听起来你有点累，我会放轻一点"
        case "cheerful":
            return "被你的开心带起来了"
        case "curious":
            return "在认真接你的问题"
        case "focused":
            return "进入解决问题模式"
        default:
            return "在听"
        }
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
            withAnimation(.easeOut(duration: 0.2)) {
                if service.isBusy {
                    proxy.scrollTo("busy", anchor: .bottom)
                } else if let last = messages.last {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
    }
}

struct MessageBubble: View {
    let message: ChatMessage
    let play: () -> Void

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            if message.isUser { Spacer(minLength: 44) }

            VStack(alignment: message.isUser ? .trailing : .leading, spacing: 4) {
                Text(message.text)
                    .font(.body)
                    .foregroundStyle(.primary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                    .background(message.isUser ? Color(red: 0.58, green: 0.90, blue: 0.42) : Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 6))

                if message.audioURL != nil {
                    Button(action: play) {
                        Label("语音", systemImage: "speaker.wave.2.fill")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(maxWidth: UIScreen.main.bounds.width * 0.74, alignment: message.isUser ? .trailing : .leading)

            if !message.isUser { Spacer(minLength: 44) }
        }
        .padding(.horizontal, 12)
    }
}

struct SettingsView: View {
    @ObservedObject var service: VoiceMateService
    @Binding var showClone: Bool
    @Environment(\.dismiss) private var dismiss

    private let voices = [
        "zh_female_wanqudashu_moon_bigtts": "火山女声",
        "zh_female_qingxinnvsheng_mars_bigtts": "清新女声",
        "zh_female_tianmeixiaoyuan_moon_bigtts": "甜美女声",
        "zh_female_gaolengyujie_moon_bigtts": "冷静女声",
    ]

    private let personas = [
        "love": "温柔陪伴",
        "friend": "朋友",
        "assistant": "助理",
        "mentor": "导师",
        "playful": "活泼",
    ]

    var body: some View {
        NavigationStack {
            Form {
                Section("局域网后端") {
                    TextField("服务器 IP", text: $service.serverHost)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.decimalPad)
                    TextField("端口", text: $service.serverPort)
                        .keyboardType(.numberPad)
                }

                Section("声音") {
                    Picker("音色", selection: $service.selectedVoice) {
                        ForEach(voices.keys.sorted(), id: \.self) { key in
                            Text(voices[key] ?? key).tag(key)
                        }
                    }
                    Slider(value: $service.speechSpeed, in: 0.75...1.25, step: 0.05) {
                        Text("语速")
                    }
                    Button("声音复刻") {
                        dismiss()
                        showClone = true
                    }
                }

                Section("人格") {
                    Picker("人格", selection: $service.selectedPersona) {
                        ForEach(personas.keys.sorted(), id: \.self) { key in
                            Text(personas[key] ?? key).tag(key)
                        }
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
