# VoiceMate

VoiceMate is a local-network iOS companion app for iOS 16.5 and TrollStore.

Current stack:

- iOS SwiftUI app with a clean chat-first interface.
- FastAPI backend for text chat, voice clone placeholders, audio files, and LiveKit tokens.
- LiveKit realtime voice call pipeline.
- DeepSeek-compatible LLM.
- Volcengine streaming ASR and Volcengine V3 TTS as the only voice providers.
- Codemagic unsigned IPA build for TrollStore.

## Repository Layout

```text
VoiceMate/
  backend/
    server.py
    livekit_agent.py
    requirements.txt
    .env.example
  iOS/
    project.yml
    Package.swift
    VoiceMate/
  codemagic.yaml
  start-voicemate-all.bat
```

## Backend

Create `backend/.env` from `backend/.env.example`, then configure:

```text
DEEPSEEK_API_KEY=
LIVEKIT_HOST=192.168.10.233
LIVEKIT_PORT=7880
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
VOLCENGINE_TTS_API_KEY=
VOLCENGINE_TTS_RESOURCE_ID=volc.service_type.10029
VOLCENGINE_TTS_WS_URL=wss://openspeech.bytedance.com/api/v3/tts/bidirection
VOLCENGINE_TTS_VOICE_TYPE=zh_female_qingxinnvsheng_mars_bigtts
```

On Windows, use:

```bat
start-voicemate-all.bat
```

The app expects the backend API on port `8000` and LiveKit on port `7880`.

## Monitoring

Open the local monitor after the backend starts:

```text
http://127.0.0.1:8000/monitor
```

The monitor tracks chat/realtime ASR, LLM, TTS, ACK and prewarm latency with
P50/P90 summaries. The practical latency target is first audible ACK under
`300ms`; a full cloud ASR -> LLM -> TTS turn is tracked separately because it is
provider/network dependent.

## iOS Build

Codemagic is the supported build path.

The workflow:

1. Generates the Xcode project with XcodeGen.
2. Builds `VoiceMate` unsigned for `iphoneos`.
3. Packages a single artifact: `VoiceMate.ipa`.

Install the IPA with TrollStore.

## Realtime Voice

The project now has one RTC session entrypoint:

```text
iOS -> /v1/rtc/session -> selected RTC provider
```

`VOICEMATE_RTC_PROVIDER=livekit` keeps the LAN fallback path:

```text
iOS microphone -> LiveKit -> backend agent -> Volcengine ASR -> LLM -> Volcengine TTS -> LiveKit -> iOS speaker
```

`VOICEMATE_RTC_PROVIDER=volcengine` is the target low-latency path:

```text
iOS -> Volcengine RTC / real-time conversational AI -> backend CustomLLM -> Volcengine TTS -> iOS
```

The backend exposes:

- `POST /v1/rtc/session`: unified call session bootstrap.
- `GET /v1/rtc/capabilities`: current provider and missing Volcengine fields.
- `POST /v1/volcengine/custom-llm`: CustomLLM callback for persona, emotion and memory.
- `POST /v1/volcengine/voice-chat/start|update|stop`: server-side OpenAPI bridge.

Volcengine RTC requires the service to be enabled in the Volcengine console,
plus RTC AppId, token generation and iOS SDK integration before it can fully
replace LiveKit media transport.
