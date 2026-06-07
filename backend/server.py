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

from metrics_store import MetricsStore
from orchestrator import CompanionOrchestrator
from volcengine_rtc import VolcengineRTCConfig, VolcengineRTCService

load_dotenv()

ROOT = Path(__file__).resolve().parent
AUDIO_DIR = ROOT / "audio_cache"
HISTORY_DIR = ROOT / "history"
MEMORY_DIR = ROOT / "memory"
CLONE_DB_PATH = ROOT / "voice_clones.json"
for folder in (AUDIO_DIR, HISTORY_DIR, MEMORY_DIR):
    folder.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("VOICEMATE_HOST", "0.0.0.0")
PORT = int(os.environ.get("VOICEMATE_PORT", "8000"))
PUBLIC_HOST = os.environ.get("VOICEMATE_PUBLIC_HOST", "192.168.10.233")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
CHAT_MAX_TOKENS = int(os.environ.get("VOICEMATE_CHAT_MAX_TOKENS", "220"))

VOLCENGINE_TTS_DEFAULT_VOICE = "zh_female_qingxinnvsheng_mars_bigtts"
DEFAULT_VOICE = os.environ.get("VOICEMATE_TTS_VOICE", VOLCENGINE_TTS_DEFAULT_VOICE)
TTS_VOICE = DEFAULT_VOICE
DEFAULT_PERSONA = os.environ.get("VOICEMATE_DEFAULT_PERSONA", "love")

VOLCENGINE_TTS_API_KEY = os.environ.get("VOLCENGINE_TTS_API_KEY", "")
VOLCENGINE_TTS_APP_ID = os.environ.get("VOLCENGINE_TTS_APP_ID", os.environ.get("VOLC_APPID", ""))
VOLCENGINE_TTS_ACCESS_KEY = os.environ.get("VOLCENGINE_TTS_ACCESS_KEY", os.environ.get("VOLC_TOKEN", ""))
VOLCENGINE_TTS_RESOURCE_ID = os.environ.get("VOLCENGINE_TTS_RESOURCE_ID", "volc.service_type.10029")
VOLCENGINE_TTS_WS_URL = os.environ.get("VOLCENGINE_TTS_WS_URL", "wss://openspeech.bytedance.com/api/v3/tts/bidirection")
VOLCENGINE_TTS_VOICE_TYPE = os.environ.get("VOLCENGINE_TTS_VOICE_TYPE", VOLCENGINE_TTS_DEFAULT_VOICE)
VOLCENGINE_TTS_MODEL = os.environ.get("VOLCENGINE_TTS_MODEL", "seed-tts-2.0-expressive")
VOLCENGINE_TTS_FEMALE_VOICES = {
    "zh_female_qingxinnvsheng_mars_bigtts",
    "zh_female_tianmeixiaoyuan_moon_bigtts",
    "zh_female_gaolengyujie_moon_bigtts",
}

LIVEKIT_HOST = os.environ.get("LIVEKIT_HOST", PUBLIC_HOST)
LIVEKIT_PORT = int(os.environ.get("LIVEKIT_PORT", "7880"))
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", f"ws://{LIVEKIT_HOST}:{LIVEKIT_PORT}")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")
LIVEKIT_AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "VoiceMate")
VOLCENGINE_RTC_APP_ID = os.environ.get("VOLCENGINE_RTC_APP_ID", "")
VOLCENGINE_RTC_APP_KEY = os.environ.get("VOLCENGINE_RTC_APP_KEY", "")
VOLCENGINE_RTC_TOKEN_URL = os.environ.get("VOLCENGINE_RTC_TOKEN_URL", "")
VOICEMATE_RTC_PROVIDER = os.environ.get("VOICEMATE_RTC_PROVIDER", "livekit").strip().lower()

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


@app.on_event("startup")
async def startup_prewarm() -> None:
    if os.environ.get("VOICEMATE_STARTUP_PREWARM", "1").lower() in {"0", "false", "no"}:
        return
    try:
        result = await prewarm()
        logger.info("Startup prewarm complete: %s", result)
    except Exception as exc:
        logger.warning("Startup prewarm failed: %s", exc)

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


def is_semantically_incomplete(text: str) -> bool:
    compact = clean_text(text)
    if not compact:
        return True
    return compact.endswith(("，", ",", "、", "但是", "然后", "因为"))


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
    emotion_label: str = "在听你说"
    emotion_intensity: int = 1
    need: str = "conversation"


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


class RTCSessionRequest(BaseModel):
    room: Optional[str] = None
    identity: Optional[str] = None
    voice: Optional[str] = None
    persona: Optional[str] = None
    speed: Optional[float] = 1.0


class VolcengineVoiceChatStartRequest(BaseModel):
    room_id: str
    task_id: Optional[str] = None
    voice: Optional[str] = None
    persona: Optional[str] = None
    speed: Optional[float] = 1.0


class VolcengineVoiceChatStopRequest(BaseModel):
    room_id: str
    task_id: str


class VolcengineCustomLLMRequest(BaseModel):
    text: Optional[str] = None
    query: Optional[str] = None
    content: Optional[str] = None
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None
    persona: Optional[str] = None
    messages: Optional[list[dict[str, Any]]] = None
    history: Optional[list[dict[str, Any]]] = None
    extra: Optional[dict[str, Any]] = None


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
companion_orchestrator = CompanionOrchestrator(MEMORY_DIR)
metrics_store = MetricsStore(MEMORY_DIR)
volcengine_rtc = VolcengineRTCService(VolcengineRTCConfig.from_env())


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def detect_emotion(text: str) -> str:
    if any(word in text for word in ("焦虑", "慌", "害怕", "担心", "心烦", "紧张", "压力", "睡不着")):
        return "anxious"
    if any(word in text for word in ("孤独", "没人陪", "一个人", "没人懂", "空落落")):
        return "lonely"
    if any(word in text for word in ("生气", "气死", "火大", "烦死", "讨厌", "不爽")):
        return "angry"
    if any(word in text for word in ("想你", "抱抱", "陪我", "喜欢你", "爱你", "贴贴")):
        return "affectionate"
    if any(word in text for word in ("开心", "高兴", "哈哈", "太好了", "喜欢")):
        return "cheerful"
    if any(word in text for word in ("难受", "累", "崩溃", "委屈", "想哭", "痛苦", "失落")):
        return "comforting"
    if "?" in text or "？" in text:
        return "curious"
    return "gentle"

def tts_rate(speed: Optional[float]) -> str:
    value = speed or 1.0
    percent = int((value - 1.0) * 100)
    return f"{percent:+d}%"


def volc_speech_rate(speed: Optional[float]) -> int:
    value = max(0.5, min(float(speed or 1.0), 2.0))
    return max(-50, min(100, int((value - 1.0) * 100)))


def volc_emotion(emotion: str) -> Optional[str]:
    return {
        "cheerful": "happy",
        "curious": "happy",
        "affectionate": "happy",
        "comforting": "sad",
        "anxious": "sad",
        "lonely": "sad",
        "angry": "angry",
    }.get(emotion)


def normalize_voice(voice: Optional[str]) -> str:
    return normalize_volcengine_voice(voice)


def normalize_volcengine_voice(voice: Optional[str]) -> str:
    if not voice:
        return VOLCENGINE_TTS_VOICE_TYPE
    if voice.startswith("volc:"):
        return voice.removeprefix("volc:")
    if voice == "zh_female_wanqudashu_moon_bigtts":
        return VOLCENGINE_TTS_DEFAULT_VOICE
    if "_" not in voice:
        return VOLCENGINE_TTS_VOICE_TYPE
    if voice.startswith("zh_female_") and voice in VOLCENGINE_TTS_FEMALE_VOICES:
        return voice
    if voice.startswith("zh_female_"):
        return VOLCENGINE_TTS_VOICE_TYPE
    return voice


def audio_url(path: Path) -> str:
    return f"/v1/audio/{path.stem}"


class LLMClient:
    def __init__(self):
        self.api_key = DEEPSEEK_API_KEY

    async def reply(self, text: str, conversation_id: str, persona: str) -> str:
        history = history_store.load(conversation_id)
        if not self.api_key:
            return f"我听到了：{text}。现在后端还没配置大模型 Key，所以我先用本地回复陪你。"

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=self.api_key, base_url=DEEPSEEK_BASE_URL)
            messages = companion_orchestrator.build_messages(
                user_text=text,
                conversation_id=conversation_id,
                persona=persona,
                history=history,
                mode="chat",
            )
            response = await client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=messages,
                temperature=0.8,
                max_tokens=CHAT_MAX_TOKENS,
            )
            content = response.choices[0].message.content or ""
            return clean_text(content) or "我在听，你继续说。"
        except Exception as exc:
            logger.exception("LLM failed")
            return f"我刚才有点卡住了，但我听到你说：{text}"


class VolcengineTTSClient:
    def __init__(self):
        self.api_key = VOLCENGINE_TTS_API_KEY
        self.app_id = VOLCENGINE_TTS_APP_ID
        self.access_key = VOLCENGINE_TTS_ACCESS_KEY
        self.resource_id = VOLCENGINE_TTS_RESOURCE_ID
        self.ws_url = VOLCENGINE_TTS_WS_URL
        self.voice_type = VOLCENGINE_TTS_VOICE_TYPE
        self.model = VOLCENGINE_TTS_MODEL

    @property
    def is_available(self) -> bool:
        return bool((self.api_key or (self.app_id and self.access_key)) and self.resource_id)

    def _headers(self) -> dict[str, str]:
        headers = {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        else:
            headers["X-Api-App-Key"] = self.app_id
            headers["X-Api-Access-Key"] = self.access_key
        return headers

    @staticmethod
    async def _connect(websockets, url: str, headers: dict[str, str]):
        try:
            return await websockets.connect(url, additional_headers=headers, max_size=1000000000)
        except TypeError:
            return await websockets.connect(url, extra_headers=headers, max_size=1000000000)

    async def synthesize_bytes(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = 1.0,
        emotion: str = "gentle",
        audio_format: str = "mp3",
    ) -> bytes:
        if not self.is_available:
            raise RuntimeError("Volcengine TTS credentials are not configured")

        chunks = bytearray()
        async with await self.open_ws() as ws:
            from volcengine_audio import VolcengineTTSFunctions
            chunks.extend(await self.synthesize_bytes_on_ws(ws, text, voice=voice, speed=speed, emotion=emotion, audio_format=audio_format))
            await ws.send(VolcengineTTSFunctions.finish_connection_payload())

        if not chunks:
            raise RuntimeError("Volcengine TTS returned empty audio")
        return bytes(chunks)

    async def open_ws(self):
        import websockets
        from volcengine_audio import VolcengineTTSFunctions

        recv_timeout = float(os.environ.get("VOLCENGINE_TTS_TIMEOUT_SECONDS", "18"))
        ws = await self._connect(websockets, self.ws_url, self._headers())
        await ws.send(VolcengineTTSFunctions.start_connection_payload())
        await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
        return ws

    async def synthesize_bytes_on_ws(
        self,
        ws,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = 1.0,
        emotion: str = "gentle",
        audio_format: str = "mp3",
    ) -> bytes:
        from volcengine_audio import EventReceive, TTSAudioFormat, VolcengineTTSFunctions

        recv_timeout = float(os.environ.get("VOLCENGINE_TTS_TIMEOUT_SECONDS", "18"))
        speaker = normalize_volcengine_voice(voice)
        session_id = str(uuid.uuid4())
        audio_params = {
            "format": TTSAudioFormat(audio_format).value,
            "sample_rate": 24000,
            "speech_rate": volc_speech_rate(speed),
        }
        emotion_name = volc_emotion(emotion)
        if emotion_name:
            audio_params["emotion"] = emotion_name
            audio_params["emotion_scale"] = 4

        req_params = {
            "text": text,
            "speaker": speaker,
            "model": self.model,
            "audio_params": audio_params,
        }

        chunks = bytearray()
        await ws.send(VolcengineTTSFunctions.start_session_payload(session_id, req_params))
        await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
        await ws.send(VolcengineTTSFunctions.task_request_payload(session_id, text, speaker, audio_params))

        while True:
            event, _sid, payload = VolcengineTTSFunctions.extract_response_payload(
                await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            )
            if event == EventReceive.TTSResponse and isinstance(payload, (bytes, bytearray)):
                chunks.extend(payload)
            elif event == EventReceive.SessionFailed:
                raise RuntimeError(f"Volcengine TTS session failed: {payload}")
            elif event in (EventReceive.TTSSentenceEnd, EventReceive.TTSEnded):
                break

        await ws.send(VolcengineTTSFunctions.finish_session_payload(session_id))
        while True:
            event, _sid, payload = VolcengineTTSFunctions.extract_response_payload(
                await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            )
            if event == EventReceive.SessionFinished:
                break
            if event == EventReceive.SessionFailed:
                raise RuntimeError(f"Volcengine TTS finish failed: {payload}")
        return bytes(chunks)


class TTSEngine:
    def __init__(self):
        self._volc = VolcengineTTSClient()

    async def synthesize(
        self,
        text: str,
        voice: Optional[str],
        speed: Optional[float],
        emotion: Optional[str] = None,
        emotion_speed: Optional[float] = None,
    ) -> tuple[Path, int]:
        audio_id = uuid.uuid4().hex[:16]
        output = AUDIO_DIR / f"{audio_id}.mp3"
        text = prepare_tts_text(text)
        if not text:
            text = "嗯。"
        if not self._volc.is_available:
            raise RuntimeError("Volcengine TTS credentials are not configured")
        if self._volc.is_available:
            try:
                output.write_bytes(
                    await self._volc.synthesize_bytes(
                        text,
                        voice=voice,
                        speed=(speed or 1.0) * (emotion_speed or 1.0),
                        emotion=emotion or detect_emotion(text),
                        audio_format="mp3",
                    )
                )
                return output, max(650, int(len(text) / 5.2 * 1000))
            except Exception:
                logger.exception("volcengine tts failed")
                raise

        raise RuntimeError("Volcengine TTS did not produce audio")


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
    return HTMLResponse("<h1>VoiceMate</h1><p>Backend is running.</p>")


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    total_timer = metrics_store.timer("chat_total")
    text = clean_text(request.text)
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    conversation_id = request.conversation_id or uuid.uuid4().hex
    persona = request.persona or DEFAULT_PERSONA
    analysis = companion_orchestrator.analyze(text)
    llm_timer = metrics_store.timer("chat_llm")
    reply = await llm.reply(text, conversation_id, persona)
    llm_timer.stop(persona=persona)
    reply_emotion = detect_emotion(reply)
    tts_emotion = analysis.tts_emotion if analysis.emotion != "neutral" else reply_emotion
    tts_timer = metrics_store.timer("chat_tts")
    audio_path, duration_ms = await tts.synthesize(
        reply,
        request.voice,
        request.speed,
        emotion=tts_emotion,
        emotion_speed=analysis.tts_speed,
    )
    tts_timer.stop(emotion=tts_emotion)
    history_store.append(conversation_id, text, reply)
    recorded = companion_orchestrator.record_turn(
        conversation_id=conversation_id,
        user_text=text,
        assistant_text=reply,
    )
    total_timer.stop(persona=persona, emotion=recorded.emotion)
    return ChatResponse(
        reply_text=reply,
        audio_url=audio_url(audio_path),
        conversation_id=conversation_id,
        duration_ms=duration_ms,
        emotion=recorded.emotion if recorded.emotion != "neutral" else reply_emotion,
        emotion_label=recorded.status_label,
        emotion_intensity=recorded.intensity,
        need=recorded.need,
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


@app.post("/v1/rtc/session")
async def rtc_session(request: Optional[RTCSessionRequest] = None):
    provider = VOICEMATE_RTC_PROVIDER
    if provider == "volcengine":
        try:
            return await volcengine_rtc.create_client_session(
                room_id=request.room if request else None,
                user_id=request.identity if request else None,
                persona=request.persona if request else None,
                voice=normalize_volcengine_voice(request.voice if request else None),
                speed=request.speed if request else 1.0,
            )
        except Exception as exc:
            logger.exception("Volcengine RTC session failed")
            raise HTTPException(status_code=500, detail=f"Volcengine RTC session failed: {exc}") from exc

    token = await livekit_token(
        LiveKitTokenRequest(
            room=request.room if request else None,
            identity=request.identity if request else None,
            voice=request.voice if request else None,
            persona=request.persona if request else None,
            speed=request.speed if request else 1.0,
        )
    )
    return {"provider": "livekit", **token}


@app.post("/v1/volcengine/voice-chat/start")
async def volcengine_voice_chat_start(request: VolcengineVoiceChatStartRequest):
    try:
        return await volcengine_rtc.start_voice_chat(
            room_id=request.room_id,
            task_id=request.task_id,
            persona=request.persona,
            voice=normalize_volcengine_voice(request.voice),
            speed=request.speed,
        )
    except Exception as exc:
        logger.exception("Volcengine StartVoiceChat failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/volcengine/voice-chat/update")
async def volcengine_voice_chat_update(payload: dict[str, Any]):
    try:
        return await volcengine_rtc.update_voice_chat(payload)
    except Exception as exc:
        logger.exception("Volcengine UpdateVoiceChat failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/volcengine/voice-chat/stop")
async def volcengine_voice_chat_stop(request: VolcengineVoiceChatStopRequest):
    try:
        return await volcengine_rtc.stop_voice_chat(room_id=request.room_id, task_id=request.task_id)
    except Exception as exc:
        logger.exception("Volcengine StopVoiceChat failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _extract_custom_llm_text(payload: VolcengineCustomLLMRequest | dict[str, Any]) -> str:
    data = payload.model_dump() if isinstance(payload, VolcengineCustomLLMRequest) else payload
    for key in ("text", "query", "content", "input", "prompt"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return clean_text(value)

    for key in ("messages", "history"):
        messages = data.get(key)
        if not isinstance(messages, list):
            continue
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or message.get("Role") or "").lower()
            content = message.get("content") or message.get("Content") or message.get("text")
            if role in {"user", "human"} and isinstance(content, str) and content.strip():
                return clean_text(content)
            if isinstance(content, list):
                parts = [item.get("text", "") for item in content if isinstance(item, dict)]
                joined = clean_text(" ".join(parts))
                if role in {"user", "human"} and joined:
                    return joined
    return ""


def _custom_llm_conversation_id(payload: VolcengineCustomLLMRequest | dict[str, Any]) -> str:
    data = payload.model_dump() if isinstance(payload, VolcengineCustomLLMRequest) else payload
    for key in ("conversation_id", "ConversationId", "dialog_id", "room_id", "RoomId", "user_id", "UserId"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"[^a-zA-Z0-9_-]", "", value)[:96] or "volcengine"
    return "volcengine"


@app.post("/v1/volcengine/custom-llm")
async def volcengine_custom_llm(payload: dict[str, Any]):
    text = _extract_custom_llm_text(payload)
    if not text:
        return {
            "content": "我在，继续说。",
            "text": "我在，继续说。",
            "reply": "我在，继续说。",
            "finish": True,
        }

    conversation_id = _custom_llm_conversation_id(payload)
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    persona = payload.get("persona") or payload.get("Persona") or extra.get("persona") or DEFAULT_PERSONA
    analysis = companion_orchestrator.analyze(text)
    started = metrics_store.timer("volcengine_custom_llm")
    reply = await llm.reply(text, conversation_id, str(persona))
    recorded = companion_orchestrator.record_turn(
        conversation_id=conversation_id,
        user_text=text,
        assistant_text=reply,
    )
    started.stop(emotion=recorded.emotion)
    logger.info(
        "Volcengine CustomLLM reply conversation=%s emotion=%s chars=%d",
        conversation_id,
        recorded.emotion,
        len(reply),
    )
    return {
        "content": reply,
        "text": reply,
        "reply": reply,
        "message": {"role": "assistant", "content": reply},
        "emotion": recorded.emotion if recorded.emotion != "neutral" else analysis.emotion,
        "emotion_label": recorded.status_label,
        "tts": {
            "emotion": analysis.tts_emotion,
            "speed": analysis.tts_speed,
        },
        "finish": True,
    }


@app.get("/v1/monitor/summary")
async def monitor_summary(window_seconds: int = 3600):
    volc_caps = volcengine_rtc.capabilities()
    return {
        "status": "ok",
        "rtc_provider": VOICEMATE_RTC_PROVIDER,
        "targets": {
            "first_ack_ms": 300,
            "full_turn_ms": 1200 if VOICEMATE_RTC_PROVIDER == "volcengine" else 1800,
        },
        "metrics": metrics_store.summary(window_seconds=window_seconds),
        "voice": VOLCENGINE_TTS_VOICE_TYPE,
        "livekit_url": LIVEKIT_URL,
        "volcengine_rtc_configured": volc_caps["configured"],
        "volcengine_rtc": volc_caps,
    }


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page():
    data = await monitor_summary()
    rows = []
    for name, item in data["metrics"].items():
        rows.append(
            "<tr>"
            f"<td>{name}</td>"
            f"<td>{item['count']}</td>"
            f"<td>{item['latest_ms']}</td>"
            f"<td>{item['p50_ms']}</td>"
            f"<td>{item['p90_ms']}</td>"
            f"<td>{item['avg_ms']}</td>"
            "</tr>"
        )
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width,initial-scale=1">
          <meta http-equiv="refresh" content="5">
          <title>VoiceMate Monitor</title>
          <style>
            body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:24px;background:#f6f7f8;color:#1f2328}}
            h1{{font-size:22px;margin:0 0 12px}}
            .meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:12px 0 18px}}
            .box{{background:white;border:1px solid #e5e7eb;border-radius:8px;padding:12px}}
            table{{width:100%;border-collapse:collapse;background:white;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}}
            th,td{{text-align:left;padding:10px;border-bottom:1px solid #edf0f2;font-size:14px}}
            th{{background:#f0f2f4}}
          </style>
        </head>
        <body>
          <h1>VoiceMate Monitor</h1>
          <div class="meta">
            <div class="box">RTC: <b>{rtc}</b></div>
            <div class="box">Voice: <b>{voice}</b></div>
            <div class="box">LiveKit: <b>{livekit}</b></div>
            <div class="box">Volc RTC configured: <b>{volc_rtc}</b></div>
          </div>
          <table>
            <thead><tr><th>Metric</th><th>Count</th><th>Latest ms</th><th>P50 ms</th><th>P90 ms</th><th>Avg ms</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </body>
        </html>
        """.format(
            rtc=data["rtc_provider"],
            voice=data["voice"],
            livekit=data["livekit_url"],
            volc_rtc=data["volcengine_rtc_configured"],
            rows="\n".join(rows) or "<tr><td colspan='6'>暂无指标，先进行一次聊天或通话。</td></tr>",
        )
    )


@app.get("/v1/rtc/capabilities")
async def rtc_capabilities():
    volc_caps = volcengine_rtc.capabilities()
    return {
        "active_provider": VOICEMATE_RTC_PROVIDER,
        "livekit": {
            "configured": bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET),
            "url": LIVEKIT_URL,
            "role": "active" if VOICEMATE_RTC_PROVIDER == "livekit" else "fallback",
        },
        "volcengine_rtc": {
            **volc_caps,
            "role": "active" if VOICEMATE_RTC_PROVIDER == "volcengine" else "prepared",
            "note": "Switch VOICEMATE_RTC_PROVIDER=volcengine after AppId, token generation and iOS SDK integration are ready.",
        },
    }


@app.post("/v1/prewarm")
async def prewarm():
    results: dict[str, Any] = {}
    started = metrics_store.timer("prewarm_total")
    try:
        tts_timer = metrics_store.timer("prewarm_tts")
        audio = await tts._volc.synthesize_bytes(
            "嗯，我在。",
            voice=VOLCENGINE_TTS_VOICE_TYPE,
            speed=1.0,
            emotion="gentle",
            audio_format="pcm",
        )
        results["tts_pcm_bytes"] = len(audio)
        results["tts_ms"] = tts_timer.stop()
    except Exception as exc:
        results["tts_error"] = str(exc)

    if DEEPSEEK_API_KEY:
        try:
            llm_timer = metrics_store.timer("prewarm_llm")
            _ = await llm.reply("只回复一个字：在", "prewarm", DEFAULT_PERSONA)
            results["llm_ms"] = llm_timer.stop()
        except Exception as exc:
            results["llm_error"] = str(exc)

    results["total_ms"] = started.stop()
    return {"status": "ok", **results}


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
