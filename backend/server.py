"""
VoiceMate backend.

Clean rebuild goals:
- Small FastAPI surface with stable iOS-compatible responses.
- Text chat -> LLM -> TTS audio file.
- Voice clone endpoints keep the app workflow alive even when a cloud clone
  provider is not configured.
- LiveKit token endpoint powers realtime calls; WebRTC handles echo
  cancellation instead of the iOS app streaming raw speaker/mic PCM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

load_dotenv()

ROOT = Path(__file__).resolve().parent
AUDIO_DIR = ROOT / "audio_cache"
HISTORY_DIR = ROOT / "history"
CLONE_DB_PATH = ROOT / "voice_clones.json"
for folder in (AUDIO_DIR, HISTORY_DIR):
    folder.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("VOICEMATE_HOST", "0.0.0.0")
PORT = int(os.environ.get("VOICEMATE_PORT", "8000"))
PUBLIC_HOST = os.environ.get("VOICEMATE_PUBLIC_HOST", "192.168.10.233")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

DEFAULT_VOICE = os.environ.get("VOICEMATE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
TTS_VOICE = DEFAULT_VOICE
DEFAULT_PERSONA = os.environ.get("VOICEMATE_DEFAULT_PERSONA", "love")
VOICEMATE_TTS_PROVIDER = os.environ.get("VOICEMATE_TTS_PROVIDER", "edge").strip().lower()

LIVEKIT_HOST = os.environ.get("LIVEKIT_HOST", PUBLIC_HOST)
LIVEKIT_PORT = int(os.environ.get("LIVEKIT_PORT", "7880"))
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", f"ws://{LIVEKIT_HOST}:{LIVEKIT_PORT}")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")
LIVEKIT_AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "VoiceMate")

logging.basicConfig(
    level=os.environ.get("VOICEMATE_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("voicemate")

app = FastAPI(title="VoiceMate API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PERSONAS: dict[str, str] = {
    "love": "你是亲密、温柔、有边界感的个人陪伴型 AI。回复自然、简短、有情绪，但不要油腻。",
    "friend": "你像一个可靠朋友，说话轻松、真诚，少说教，多共情。",
    "assistant": "你是高效助理，先给结论，再给必要步骤，语言简洁。",
    "mentor": "你像温和导师，帮助用户看清问题，给可执行建议。",
    "playful": "你活泼一点，但不夸张，不使用密集表情。",
}

PROACTIVE: dict[str, list[str]] = {
    "love": ["我在，慢慢说就好。", "今天有什么想跟我讲的吗？"],
    "friend": ["我来了，最近怎么样？", "有事就说，我听着。"],
    "assistant": ["已就绪。你可以直接告诉我任务。"],
    "mentor": ["我们可以先把问题拆小一点。"],
    "playful": ["上线啦，今天从哪件小事开始？"],
}

FILLER_WORDS = {"嗯", "啊", "哦", "呃", "唔", "um", "uh", "hmm"}
EMOTION_TTS_PROFILES: dict[str, dict[str, str]] = {
    "gentle": {"rate": "+0%", "pitch": "+0Hz"},
    "curious": {"rate": "+2%", "pitch": "+5Hz"},
    "cheerful": {"rate": "+4%", "pitch": "+8Hz"},
    "comforting": {"rate": "-2%", "pitch": "-3Hz"},
}


def strip_markdown(text: str) -> str:
    text = re.sub(r"[*_`#>-]+", "", text or "")
    return clean_text(text)


def naturalize_text(text: str, emotion: str = "gentle") -> str:
    return strip_markdown(text)


def strip_spoken_emotes(text: str) -> str:
    text = text or ""
    text = re.sub(
        r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF]",
        "",
        text,
    )
    emote_words = (
        "笑|微笑|偷笑|苦笑|大笑|开心|难过|委屈|害羞|脸红|眨眼|抱抱|叹气|哭|哭笑|"
        "捂脸|思考|点头|摇头|撒娇|认真|温柔|惊讶|尴尬|可怜|调皮|爱心|心动"
    )
    text = re.sub(rf"[\(（\[]\s*(?:{emote_words})\s*[\)）\]]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:哈\s*){2,}", "", text)
    text = re.sub(r"^[,，。.!！?？、\s]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def prepare_tts_text(text: str, emotion: str = "gentle") -> str:
    return strip_spoken_emotes(naturalize_text(text, emotion))


def resolve_edge_voice(voice: Optional[str], emotion: str = "gentle") -> str:
    return normalize_voice(voice)


def is_semantically_incomplete(text: str) -> bool:
    compact = clean_text(text)
    if not compact:
        return True
    return compact.endswith(("，", ",", "、", "但是", "然后", "因为"))


class MiMoTTS:
    @property
    def is_available(self) -> bool:
        return False

    async def synthesize(self, text: str, emotion: str = "gentle", speed_ratio: Optional[float] = 1.0):
        return await tts.synthesize(text, DEFAULT_VOICE, speed_ratio)


class ChatRequest(BaseModel):
    text: str = Field(min_length=1)
    conversation_id: Optional[str] = None
    voice: Optional[str] = None
    persona: Optional[str] = None
    speed: Optional[float] = Field(default=1.0, ge=0.5, le=2.0)


class ChatResponse(BaseModel):
    reply_text: str
    audio_url: str
    conversation_id: str
    duration_ms: int = 0
    emotion: str = "gentle"


class CloneVoiceResponse(BaseModel):
    voice_id: str
    status: str
    message: Optional[str] = None


class CloneVoiceInfo(BaseModel):
    voice_id: str
    name: Optional[str] = None
    status: str = "ready"
    created_at: Optional[str] = None


class LiveKitTokenRequest(BaseModel):
    room: Optional[str] = None
    identity: Optional[str] = None
    voice: Optional[str] = None
    persona: Optional[str] = None
    speed: Optional[float] = 1.0


class ConversationStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, conversation_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", conversation_id) or uuid.uuid4().hex
        return self.root / f"{safe}.json"

    def load(self, conversation_id: str) -> list[dict[str, str]]:
        path = self._path(conversation_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def append(self, conversation_id: str, user_text: str, assistant_text: str) -> None:
        history = self.load(conversation_id)
        history.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )
        self._path(conversation_id).write_text(
            json.dumps(history[-20:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


history_store = ConversationStore(HISTORY_DIR)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def detect_emotion(text: str) -> str:
    if re.search(r"[?？]", text):
        return "curious"
    if any(word in text for word in ("开心", "高兴", "哈哈", "喜欢")):
        return "cheerful"
    if any(word in text for word in ("难过", "烦", "累", "痛苦", "崩")):
        return "comforting"
    return "gentle"


def tts_rate(speed: Optional[float]) -> str:
    value = speed or 1.0
    percent = int((value - 1.0) * 100)
    return f"{percent:+d}%"


def normalize_voice(voice: Optional[str]) -> str:
    if not voice:
        return DEFAULT_VOICE
    if voice.startswith("clone_"):
        return DEFAULT_VOICE
    if voice.startswith("fish_") or voice.startswith("mimo_"):
        return DEFAULT_VOICE
    return voice


def audio_url(path: Path) -> str:
    return f"/v1/audio/{path.stem}"


class LLMClient:
    def __init__(self):
        self.api_key = DEEPSEEK_API_KEY

    async def reply(self, text: str, conversation_id: str, persona: str) -> str:
        prompt = PERSONAS.get(persona, PERSONAS[DEFAULT_PERSONA])
        history = history_store.load(conversation_id)
        if not self.api_key:
            return f"我听到了：{text}。现在后端还没配置大模型 Key，所以我先用本地回复陪你。"

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=self.api_key, base_url=DEEPSEEK_BASE_URL)
            messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]
            messages.extend(history[-12:])
            messages.append({"role": "user", "content": text})
            response = await client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=messages,
                temperature=0.8,
                max_tokens=500,
            )
            content = response.choices[0].message.content or ""
            return clean_text(content) or "我在听，你继续说。"
        except Exception as exc:
            logger.exception("LLM failed")
            return f"我刚才有点卡住了，但我听到你说：{text}"


class TTSEngine:
    async def synthesize(self, text: str, voice: Optional[str], speed: Optional[float]) -> tuple[Path, int]:
        import edge_tts

        audio_id = uuid.uuid4().hex[:16]
        output = AUDIO_DIR / f"{audio_id}.mp3"
        selected_voice = normalize_voice(voice)
        text = prepare_tts_text(text)
        if not text:
            text = "嗯。"
        communicate = edge_tts.Communicate(
            text=text,
            voice=selected_voice,
            rate=tts_rate(speed),
        )
        try:
            await communicate.save(str(output))
        except Exception:
            logger.exception("edge-tts failed, retrying default voice")
            communicate = edge_tts.Communicate(text=text, voice=DEFAULT_VOICE, rate=tts_rate(speed))
            await communicate.save(str(output))
        duration_ms = max(800, int(len(text) / 4.5 * 1000))
        return output, duration_ms


llm = LLMClient()
tts = TTSEngine()


def load_clones() -> dict[str, Any]:
    if not CLONE_DB_PATH.exists():
        return {}
    try:
        return json.loads(CLONE_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_clones(data: dict[str, Any]) -> None:
    CLONE_DB_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/v1/health")
async def health():
    return {"status": "ok", "service": "voicemate", "version": "2.0.0"}


@app.get("/", response_class=HTMLResponse)
async def index():
    html = ROOT.parent / "voicemate.html"
    if html.exists():
        return HTMLResponse(html.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>VoiceMate</h1><p>Backend is running.</p>")


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    text = clean_text(request.text)
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    conversation_id = request.conversation_id or uuid.uuid4().hex
    persona = request.persona or DEFAULT_PERSONA
    reply = await llm.reply(text, conversation_id, persona)
    emotion = detect_emotion(reply)
    audio_path, duration_ms = await tts.synthesize(reply, request.voice, request.speed)
    history_store.append(conversation_id, text, reply)
    return ChatResponse(
        reply_text=reply,
        audio_url=audio_url(audio_path),
        conversation_id=conversation_id,
        duration_ms=duration_ms,
        emotion=emotion,
    )


@app.websocket("/v1/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            request = ChatRequest(
                text=payload.get("text", ""),
                conversation_id=payload.get("conversation_id"),
                voice=payload.get("voice"),
                persona=payload.get("persona"),
                speed=payload.get("speed") or 1.0,
            )
            response = await chat(request)
            await websocket.send_json({"type": "done", **response.model_dump()})
    except WebSocketDisconnect:
        return
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})


@app.get("/v1/audio/{audio_id}")
async def get_audio(audio_id: str):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", audio_id)
    for suffix in (".mp3", ".wav", ".m4a"):
        path = AUDIO_DIR / f"{safe}{suffix}"
        if path.exists():
            media = "audio/mpeg" if suffix == ".mp3" else "audio/wav"
            return FileResponse(path, media_type=media)
    raise HTTPException(status_code=404, detail="Audio not found")


@app.post("/v1/clone/upload", response_model=CloneVoiceResponse)
async def clone_upload(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No audio files uploaded")

    clone_id = f"clone_{uuid.uuid4().hex[:10]}"
    clone_dir = AUDIO_DIR / clone_id
    clone_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for index, file in enumerate(files):
        suffix = Path(file.filename or "").suffix or ".m4a"
        path = clone_dir / f"sample_{index}{suffix}"
        path.write_bytes(await file.read())
        saved.append(str(path))

    clones = load_clones()
    clones[clone_id] = {
        "voice_id": clone_id,
        "name": "我的声音",
        "status": "ready",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "samples": saved,
    }
    save_clones(clones)
    return CloneVoiceResponse(
        voice_id=clone_id,
        status="ready",
        message="样本已保存。未配置云端克隆服务时会先使用默认音色。",
    )


@app.get("/v1/clone/voices")
async def clone_voices():
    clones = load_clones()
    return {"voices": [CloneVoiceInfo(**value).model_dump() for value in clones.values()]}


@app.get("/v1/clone/status/{voice_id}", response_model=CloneVoiceInfo)
async def clone_status(voice_id: str):
    clones = load_clones()
    if voice_id not in clones:
        raise HTTPException(status_code=404, detail="Voice clone not found")
    return CloneVoiceInfo(**clones[voice_id])


@app.delete("/v1/clone/voices/{voice_id}")
async def clone_delete(voice_id: str):
    clones = load_clones()
    if voice_id not in clones:
        raise HTTPException(status_code=404, detail="Voice clone not found")
    clones.pop(voice_id)
    save_clones(clones)
    return {"status": "deleted"}


@app.post("/v1/livekit/token")
async def livekit_token(request: Optional[LiveKitTokenRequest] = None):
    room_name = (request.room if request else None) or f"voicemate-{uuid.uuid4().hex[:8]}"
    identity = (request.identity if request else None) or f"ios-{uuid.uuid4().hex[:8]}"
    metadata = {
        "voice": request.voice if request else None,
        "persona": request.persona if request else None,
        "speed": request.speed if request else 1.0,
    }

    try:
        from livekit import api

        metadata_json = json.dumps(metadata, ensure_ascii=False)
        room_config = api.RoomConfiguration(
            name=room_name,
            metadata=metadata_json,
            agents=[
                api.RoomAgentDispatch(
                    agent_name=LIVEKIT_AGENT_NAME,
                    metadata=metadata_json,
                )
            ],
        )
        token = (
            api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(identity)
            .with_name("VoiceMate")
            .with_metadata(metadata_json)
            .with_room_config(room_config)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )
    except Exception as exc:
        logger.exception("LiveKit token failed")
        raise HTTPException(status_code=500, detail=f"LiveKit token failed: {exc}") from exc

    return {"token": token, "room": room_name, "url": LIVEKIT_URL}


@app.get("/v1/proactive")
async def proactive(persona: str = DEFAULT_PERSONA):
    choices = PROACTIVE.get(persona, PROACTIVE[DEFAULT_PERSONA])
    index = uuid.uuid4().int % len(choices)
    return {"text": choices[index], "persona": persona}


@app.post("/v1/voice-chat")
async def voice_chat(audio: UploadFile = File(...), conversation_id: Optional[str] = None):
    # Kept for compatibility. The rebuilt iOS app uses SFSpeechRecognizer for
    # one-shot voice input and LiveKit for realtime calls.
    raise HTTPException(status_code=410, detail="Use iOS speech input or LiveKit realtime call")


@app.websocket("/v1/ws/voice")
async def ws_voice_compat(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json(
        {
            "type": "error",
            "message": "Realtime voice now uses LiveKit/WebRTC to avoid speaker echo.",
        }
    )
    await websocket.close(code=1000)


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting VoiceMate API on %s:%s", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT)
