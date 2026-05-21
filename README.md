# VoiceMate 🎙️

AI 语音陪聊机器人。iOS App + Hermes 后端。

按住说话 → AI 回复 → 语音播放，像微信发语音一样自然。

## 架构（全双工实时通话）

### 通话流程

iOS 麦克风**常开**，实时流式传输 PCM16 16kHz 音频到服务端，服务端进行 VAD（静音检测）。
用户说完整句话停顿后，自动触发 AI 回复。AI 回复时用户可随时**打断（Barge-in）**。

```
iPhone (VoiceMate App)                Backend
┌──────────────────────────┐          ┌─────────────────────┐
│ 麦克风常开               │          │ FastAPI WebSocket    │
│  ├─ PCM16 16kHz 流式音频  │ binary→  │  ├─ VAD 静音检测     │
│  ├─ 流式音频接收          │ ←binary  │  ├─ DeepSeek 流式    │
│  │  PCM16 24kHz chunks   │          │  │  生成（流文本）    │
│  ├─ 客户端 VAD            │          │  └─ edge-tts 流式    │
│  │  (打断检测)            │          │     语音合成（流音频）│
│  └─ Barge-in 打断         │ JSON→    │                      │
└──────────────────────────┘          └─────────────────────┘
```

### WebSocket 协议

**Client -> Server:**
| 类型 | 格式 | 说明 |
|------|------|------|
| 音频 | Binary (PCM16 16kHz mono chunks) | 麦克风常开，实时流式发送 |
| 配置 | JSON | 连接及更新参数 |
| 打断 | JSON | 用户插嘴，AI 立即停止 |
| 文本 | JSON | 客户端 ASR 结果（可选） |
| Ping | JSON | 心跳保活 |

**Server -> Client:**
| 类型 | 格式 | 说明 |
|------|------|------|
| 文本 token | JSON | DeepSeek 流式输出 |
| 音频开始 | JSON | TTS 即将开始 |
| 音频数据 | Binary (PCM16 24kHz mono chunks) | AI 语音流式回传 |
| 音频结束 | JSON | TTS 结束 |
| 轮次完成 | JSON | AI 完整回复结束 |
| 打断确认 | JSON | AI 已被打断 |
| ASR 部分 | JSON | 语音识别中间结果 |
| ASR 最终 | JSON | 识别完成 |
| Pong | JSON | 心跳回复 |
| 错误 | JSON | 错误信息 |

## 快速开始

### 1. 启动后端（WSL）

```bash
cd ~/VoiceMate/backend
bash run.sh
```

服务器启动在 `http://0.0.0.0:8000`

检查是否运行：
```bash
curl http://localhost:8000/v1/health
# {"status":"ok","service":"voicemate","version":"1.0.0"}
```

### 2. 编译 iOS App

#### 方式 A：Codemagic（推荐，免费在线编译）

1. 把整个 `VoiceMate/` 目录推送到 GitHub 私有仓库
2. 注册 [codemagic.io](https://codemagic.io/start)（用 GitHub 登录）
3. 添加你的仓库
4. Codemagic 会自动检测 `codemagic.yaml` 配置
5. 点击 "Start build"
6. 等 5-10 分钟，下载生成的 `.ipa` 文件
7. 用 TrollStore 安装

#### 方式 B：本地 Mac 编译（如果你有 Mac）

```bash
cd iOS
brew install xcodegen
xcodegen generate
open VoiceMate.xcodeproj
# 选择你的 iPhone 作为目标，按 Cmd+R 运行
```

### 3. iPhone 上配置

1. 安装 App 后打开
2. 点击右上角 ⚙️ → 设置服务器地址
3. 填你的 WSL IP（在 WSL 里运行 `ip addr show eth0 | grep inet` 查看）
4. 端口默认 `8000`
5. 点"测试连接"，成功即可开始聊天

### 4. 开始聊天

按住底部紫色麦克风按钮 → 说话 → 松手 → AI 自动语音回复

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | (必填) | DeepSeek API key |
| `DEEPSEEK_MODEL` | `deepseek-chat` | AI 模型 |
| `VOICEMATE_HOST` | `0.0.0.0` | 监听地址 |
| `VOICEMATE_PORT` | `8000` | 监听端口 |
| `VOICEMATE_TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | TTS 语音（edge-tts） |
| `VOICEMATE_SYSTEM_PROMPT` | (温暖陪聊) | AI 人设 |

### TTS 可选语音

- `zh-CN-XiaoxiaoNeural` — 晓晓（女声，推荐）
- `zh-CN-YunxiNeural` — 云希（男声）
- `zh-CN-XiaoyiNeural` — 小艺（女声，活泼）
- `zh-CN-YunjianNeural` — 云健（男声）
- `zh-TW-HsiaoChenNeural` — 晓臻（台湾腔女声）
- `en-US-JennyNeural` — Jenny（英语女声）

## 项目结构

```
VoiceMate/
├── backend/
│   ├── server.py       # FastAPI 服务器
│   ├── run.sh          # 启动脚本
│   └── .env.example    # 环境变量模板
├── iOS/
│   ├── project.yml     # XcodeGen 项目配置
│   ├── codemagic.yaml  # Codemagic CI 配置
│   └── VoiceMate/
│       ├── VoiceMateApp.swift     # App 入口
│       ├── Views/
│       │   └── ContentView.swift  # 主界面 (消息列表 + 录音按钮 + 设置)
│       ├── Models/
│       │   └── ChatMessage.swift   # 数据模型
│       ├── Services/
│       │   ├── AudioService.swift  # 录音 + 语音识别 (SFSpeechRecognizer)
│       │   └── VoiceMateService.swift  # 后端 API 通信
│       └── Resources/
│           └── Info.plist
└── .github/workflows/
    └── build-ios.yml   # GitHub Actions (需要 macOS runner)
```

## 未来计划

- [x] 语音消息模式（按住说话 → AI 回复）
- [x] 实时语音通话（WebSocket 流式对话）
- [ ] 对话历史持久化
- [ ] 多语言支持
- [ ] 自定义 AI 人设
- [ ] 连续多轮对话上下文
