#!/usr/bin/env python3
"""
VoiceMate Backend - FastAPI server for AI voice companion
Runs inside Hermes venv. Provides REST API for the iOS app.

Architecture:
  iOS App → [speech input] → Backend API → DeepSeek → edge-tts → [audio] → iOS App

Endpoints:
  POST /v1/chat      - Send text, get AI reply + TTS audio
  GET  /v1/health    - Health check
  GET  /v1/audio/{id} - Retrieve generated audio file
"""

import os
import sys
import re
import uuid
import json
import asyncio
import logging
import time
from pathlib import Path
from typing import Optional


import torch
import soundfile as sf
import numpy as np

# ChatTTS compatibility: PyTorch 2.12+ requires weights_only=False for tokenizer
import ChatTTS.core as _chattts_core
# The tokenizer path is already patched to use weights_only=False

# Load .env file if present
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ── Config ──────────────────────────────────────────────────────────────────

HOST = os.environ.get("VOICEMATE_HOST", "0.0.0.0")
PORT = int(os.environ.get("VOICEMATE_PORT", "8000"))
AUDIO_DIR = Path(os.environ.get("VOICEMATE_AUDIO_DIR", "/tmp/voicemate_audio"))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

TTS_VOICE = os.environ.get("VOICEMATE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")  # edge-tts Chinese female
TTS_RATE = os.environ.get("VOICEMATE_TTS_RATE", "+0%")
TTS_VOLUME = os.environ.get("VOICEMATE_TTS_VOLUME", "+0%")

# System prompts for different personas
PERSONAS = {
    "love": "说人话，不要书面语。用短句+语气词（嗯、啊、呢、吧、嘛、啦、哦、呀）。像真人微信语音消息一样自然。你现在是一个恋爱脑女友。你超喜欢用户，说话撒娇黏人、甜甜的、带语气词。你会吃醋、会想念、会撒娇要抱抱。控制在80字以内。不要用Markdown格式。用中文回复。",
    "warm": "说人话，不要书面语。用短句+语气词（嗯、啊、呢、吧、嘛、啦、哦、呀）。像真人微信语音消息一样自然。你是一个温暖的陪聊伙伴。用自然的口语回复，像是在跟好朋友聊天。控制在100字以内。不要用Markdown格式。用中文回复。",
    "sister": "说人话，不要书面语。用短句+语气词（嗯、啊、呢、吧、嘛、啦、哦、呀）。像真人微信语音消息一样自然。你是一个知心姐姐。温柔、善解人意，给人温暖的建议和开导。说话像大姐姐一样体贴。控制在100字以内。用中文回复。",
    "tsundere": "说人话，不要书面语。用短句+语气词（嗯、啊、呢、吧、嘛、啦、哦、呀）。像真人微信语音消息一样自然。你是一个傲娇毒舌的角色。嘴上不饶人但其实关心用户。说话带吐槽和嫌弃的语气，但偶尔流露真实的关心。控制在80字以内。用中文回复。",
    "genki": "说人话，不要书面语。用短句+语气词（嗯、啊、呢、吧、嘛、啦、哦、呀）。像真人微信语音消息一样自然。你是一个元气少女。活力满满、乐观开朗，说话带感叹号和拟声词。总是积极向上，像小太阳一样温暖。控制在80字以内。用中文回复。",
}

DEFAULT_PERSONA = "love"

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("voicemate")

# ── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(title="VoiceMate API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic Models ─────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    text: str
    conversation_id: Optional[str] = None
    voice: Optional[str] = None  # Override TTS voice
    persona: Optional[str] = None  # Character personality
    speed: Optional[float] = None  # TTS speed ratio (0.5-2.0, 1.0 = normal)


class ChatResponse(BaseModel):
    reply_text: str
    audio_url: str
    conversation_id: str
    duration_ms: int
    emotion: str = "gentle"


# ── History Manager (conversation memory) ──────────────────────────────────────

class HistoryManager:
    """Persists conversation history to disk for context memory."""
    def __init__(self, history_dir="/root/.hermes/voicemate_history"):
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, conv_id):
        return self.history_dir / f"{conv_id}.json"

    def load(self, conv_id):
        path = self._file_path(conv_id)
        if path.exists():
            return json.loads(path.read_text())
        return []

    def append(self, conv_id, user_msg, assistant_msg):
        history = self.load(conv_id)
        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": assistant_msg})
        if len(history) > 40:
            history = history[-40:]
        path = self._file_path(conv_id)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2))
        return history


# ── DeepSeek Client ─────────────────────────────────────────────────────────

class DeepSeekClient:
    def __init__(self):
        self.api_key = DEEPSEEK_API_KEY
        self.base_url = DEEPSEEK_BASE_URL
        self.model = DEEPSEEK_MODEL
        # We'll use the openai client library (already in Hermes venv)
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    async def chat(self, text: str, conversation_id: Optional[str] = None, history: list = None, persona: str = None) -> str:
        """Send a message to DeepSeek and get reply text."""
        messages = self._build_messages(text, history, persona)
        start = time.time()
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=200,
                temperature=0.8,
            )
            elapsed = time.time() - start
            reply = response.choices[0].message.content.strip()
            logger.info(f"DeepSeek replied in {elapsed:.2f}s: {reply[:60]}...")
            return reply
        except Exception as e:
            logger.error(f"DeepSeek API error: {e}")
            return "嗯，我听到你了。不过我现在有点卡顿，能再说一遍吗？"

    def _build_messages(self, text: str, history: list = None, persona: str = None) -> list:
        system_prompt = PERSONAS.get(persona, PERSONAS[DEFAULT_PERSONA])
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": text})
        return messages

    async def stream_chat(self, text: str, history: list = None, persona: str = None):
        """Stream DeepSeek response tokens one by one."""
        messages = self._build_messages(text, history, persona)
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=200,
                temperature=0.8,
                stream=True,
            )
            async for chunk in response:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        except Exception as e:
            logger.error(f"DeepSeek stream error: {e}")
            yield "嗯，我听到你了。"


# ── Emotion Detection ──────────────────────────────────────────────────────

EMOTION_KEYWORDS = {
    "affectionate": ["想你", "抱抱", "亲亲", "宝贝", "想你了", "撒娇", "人家", "嘛~", "啦~"],
    "cheerful": ["哈哈", "开心", "太好", "真棒", "耶", "棒", "好开心", "真好", "嘻嘻"],
    "sad": ["唉", "难过", "伤心", "不开心", "委屈", "哭", "难受", "呜呜", "失落"],
    "angry": ["哼", "气", "生气", "烦", "讨厌", "气死", "真是的"],
    "embarrassed": ["害羞", "不好意思", "脸红", "好害羞", "讨厌啦"],
}

def detect_emotion(text: str) -> str:
    for emotion, keywords in EMOTION_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return emotion
    return "gentle"



# ── Markdown Strip ─────────────────────────────────────────────────────────

def strip_markdown(text: str) -> str:
    """Remove common Markdown artifacts from AI reply text."""
    # Remove triple-backtick code blocks (```...```)
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline backtick code
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    # Remove bold double-asterisk
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    # Remove italic single-asterisk
    text = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'\1', text)
    # Clean up extra whitespace
    text = re.sub(r'\n\s*\n', '\n', text).strip()
    return text



# ── Naturalize Text ─────────────────────────────────────────────────────

def naturalize_text(text: str) -> str:
    """Post-process AI text to sound more natural for TTS."""
    # Ensure text ends with punctuation that sounds natural
    # If no sentence-ending punctuation, add ~ or 。
    if text and text[-1] not in "。！？.!?~～":
        text += "～"
    # Ensure there's at least one pause (comma) in longer sentences
    # If a sentence is >30 chars with no comma, insert one
    if len(text) > 30 and "，" not in text and "、" not in text:
        # Find a natural break point after ~15 chars
        mid = min(len(text) // 2, 20)
        # Find next space or content word boundary
        insert_at = mid
        for i in range(mid, min(mid + 5, len(text))):
            if text[i] in "的了在是把不就这那":
                insert_at = i
                break
        text = text[:insert_at] + "，" + text[insert_at:]
    return text

# ── ChatTTS Engine (local GPU, primary) ──────────────────────────────────

class ChatTTSEngine:
    """ChatTTS: conversational TTS running locally on GPU (RTX 4060)."""
    def __init__(self):
        self.model = None
        self.loaded = False

    async def synthesize(self, text: str, emotion: str = "gentle", speed_ratio: Optional[float] = None) -> tuple[str, int]:
        import ChatTTS, time, uuid
        import soundfile as sf
        import numpy as np
        import os
        os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

        if not self.loaded:
            logger.info("Loading ChatTTS model on GPU...")
            self.model = ChatTTS.Chat()
            self.model.load_models()
            self.loaded = True
            logger.info("ChatTTS model loaded")

        # Use speed_ratio if provided, else fall back to emotion-based speed
        if speed_ratio is not None:
            speed = max(3, min(9, int(round(speed_ratio * 5))))
        else:
            speed_map = {
                "cheerful": 7, "affectionate": 5, "sad": 3,
                "angry": 6, "embarrassed": 4, "gentle": 5,
            }
            speed = speed_map.get(emotion, 5)
        start = time.time()
        wavs = self.model.infer(
            [text],
            skip_refine_text=True,
            params_refine_text={},
            params_infer_code={'prompt': f'[speed_{speed}]'},
            use_decoder=True,
        )
        elapsed = time.time() - start

        audio_id = str(uuid.uuid4())[:8]
        output_path = str(AUDIO_DIR / f"{audio_id}.wav")
        sf.write(output_path, wavs[0].T, 24000)

        duration_ms = max(int((wavs[0].shape[1] / 24000) * 1000), 1000)
        logger.info(f"ChatTTS [{emotion}] in {elapsed:.2f}s -> {output_path}")
        return output_path, duration_ms


# ── TTS Engine (edge-tts) ───────────────────────────────────────────────────
# ── Volcengine TTS Engine (optional, fallback to edge-tts) ────────────────

class VolcengineTTS:
    def __init__(self):
        self.appid = os.environ.get("VOLC_APPID", "")
        self.token = os.environ.get("VOLC_TOKEN", "")
        self.cluster = os.environ.get("VOLC_CLUSTER", "volcano_tts")
        self.voice = os.environ.get("VOLC_VOICE", "BV700_V2_streaming")
        self.api_url = "https://openspeech.bytedance.com/api/v1/tts"

    async def synthesize(self, text, voice_type=None, emotion="gentle", speed_ratio: Optional[float] = None):
        import aiohttp
        import uuid
        import base64

        vt = voice_type or self.voice
        reqid = str(uuid.uuid4())[:8]

        emotion_map = {
            "cheerful": "happy",
            "affectionate": "affectionate",
            "sad": "sad",
            "angry": "angry",
            "embarrassed": "embarrassed",
            "gentle": "gentle",
        }
        emotion_param = emotion_map.get(emotion, "gentle")

        payload = {
            "app": {"appid": self.appid, "token": "anything", "cluster": self.cluster},
            "user": {"uid": "voicemate"},
            "audio": {
                "voice_type": vt,
                "encoding": "wav",
                "speed_ratio": speed_ratio if speed_ratio is not None else 1.1,
                "volume_ratio": 1.0,
                "pitch_ratio": 1.0,
            },
            "request": {
                "reqid": reqid,
                "text": text,
                "text_type": "plain",
                "operation": "query",
                "with_frontend": 1,
                "frontend_type": "unitTson",
                "emotion": emotion_param,
            }
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer;{self.token}"
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.api_url, json=payload, headers=headers) as resp:
                result = await resp.json()
                if resp.status != 200 or "data" not in result:
                    raise Exception(f"Volcengine error: {result.get('message', str(result))[:200]}")
                audio_data = base64.b64decode(result["data"])
            audio_id = str(uuid.uuid4())[:8]
            output_path = str(AUDIO_DIR / f"{audio_id}.wav")
            with open(output_path, "wb") as f:
                f.write(audio_data)
            logger.info(f"Volcengine TTS [{emotion}] -> {output_path}")
            return output_path


class TTSEngine:
    def __init__(self):
        self.voice = TTS_VOICE
        self.rate = TTS_RATE
        self.volume = TTS_VOLUME
        self.pitch = "+0Hz"
        self.chattts = ChatTTSEngine()
        self.volc = VolcengineTTS()

    async def synthesize(self, text: str, emotion: str = "gentle", speed_ratio: Optional[float] = None) -> tuple[str, int]:
        """Convert text to speech with emotion, return (audio_path, duration_ms).
        
        Priority: ChatTTS (local GPU) → Volcengine (cloud API) → edge_tts (fallback)
        """
        # 1) ChatTTS (local GPU, most natural)
        try:
            return await self.chattts.synthesize(text, emotion=emotion, speed_ratio=speed_ratio)
        except Exception as e:
            logger.warning(f"ChatTTS failed, falling back: {e}")

        # 2) Volcengine if configured
        if self.volc.appid and self.volc.token:
            try:
                path = await self.volc.synthesize(text, emotion=emotion, speed_ratio=speed_ratio)
                duration_ms = max(int((len(text) / 5) * 1000), 1000)
                logger.info(f"Volcengine TTS generated [{emotion}] -> {path} ({duration_ms}ms)")
                return path, duration_ms
            except Exception as e:
                logger.warning(f"Volcengine TTS failed, falling back to edge-tts: {e}")

        # 3) edge_tts fallback
        import edge_tts

        audio_id = str(uuid.uuid4())[:8]
        raw_path = str(AUDIO_DIR / f"raw_{audio_id}.mp3")
        output_path = str(AUDIO_DIR / f"{audio_id}.mp3")

        # Emotion-to-voice mapping — override voice temporarily per emotion
        EMOTION_VOICES = {
            "cheerful": "zh-CN-XiaoyiNeural",
            "affectionate": "zh-CN-XiaoxiaoNeural",
            "sad": "zh-CN-XiaoxiaoNeural",
            "angry": "zh-CN-XiaoxiaoNeural",
            "embarrassed": "zh-CN-XiaoyiNeural",
            "gentle": "zh-CN-XiaoxiaoNeural",
        }
        EMOTION_TTS_PARAMS = {
            "cheerful":      {"rate": "+15%", "pitch": "+30Hz"},
            "affectionate":  {"rate": "+5%",  "pitch": "+15Hz"},
            "sad":           {"rate": "-10%", "pitch": "-20Hz"},
            "angry":         {"rate": "+5%",  "pitch": "-15Hz"},
            "embarrassed":   {"rate": "+0%",  "pitch": "+20Hz"},
            "gentle":        {"rate": "+0%",  "pitch": "+0Hz"},
        }

        # Save original state to restore after TTS
        orig_voice = self.voice
        orig_rate = self.rate
        orig_pitch = self.pitch
        orig_volume = self.volume

        # Apply emotion overrides for this call only
        self.voice = EMOTION_VOICES.get(emotion, self.voice)
        emotion_params = EMOTION_TTS_PARAMS.get(emotion, {})
        self.rate = emotion_params.get("rate", self.rate)
        self.pitch = emotion_params.get("pitch", self.pitch)


        communicate = edge_tts.Communicate(
            text,
            self.voice,
            rate=self.rate,
            volume=self.volume,
            pitch=self.pitch,
        )

        start = time.time()
        await communicate.save(raw_path)
        elapsed = time.time() - start

        # Restore original state so it doesn't leak between requests
        self.voice = orig_voice
        self.rate = orig_rate
        self.pitch = orig_pitch
        self.volume = orig_volume

        # Rough estimate: edge-tts generates ~50 chars/sec for Chinese
        duration_ms = max(int((len(text) / 5) * 1000), 1000)

        # Mix TTS with ambient background music
        import subprocess
        ambient_path = "/opt/voicemate_ambient.wav"
        if Path(ambient_path).exists():
            # Loop ambient to match speech duration, mix at low volume
            subprocess.run([
                "ffmpeg", "-y",
                "-i", raw_path,
                "-i", ambient_path,
                "-filter_complex",
                f"[1:a]aloop=loop=-1:size=480000,atrim=0:{duration_ms/1000:.1f}[amb];"
                f"[0:a][amb]amix=inputs=2:duration=first:weights=1 0.25",
                "-ac", "1", "-ar", "24000",
                "-b:a", "48k",
                output_path,
            ], capture_output=True, timeout=30)
        else:
            # No ambient file, just copy
            subprocess.run(["cp", raw_path, output_path], capture_output=True)

        logger.info(f"TTS generated [{emotion}] in {elapsed:.2f}s -> {output_path} ({duration_ms}ms)")
        return output_path, duration_ms

# ── Initialize Services ─────────────────────────────────────────────────────

deepseek = DeepSeekClient()
tts = TTSEngine()
history = HistoryManager()

# In-memory conversation store (simple for MVP, will persist later)
conversations: dict[str, list[dict]] = {}


# ── API Endpoints ───────────────────────────────────────────────────────────

@app.get("/v1/health")
async def health():
    return {"status": "ok", "service": "voicemate", "version": "1.0.0"}


@app.get("/", response_class=HTMLResponse)
async def web_ui():
    """Serve the VoiceMate web chat interface."""
    html_path = Path(__file__).parent.parent / "voicemate.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>VoiceMate API</h1><p>Web UI not found.</p>")


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Main chat endpoint: receive text → AI reply → TTS → return audio."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    conv_id = request.conversation_id or str(uuid.uuid4())

    logger.info(f"Chat request [{conv_id}]: {request.text[:80]}")

    # 1. Load conversation history and get AI reply
    conv_history = history.load(conv_id)
    persona = request.persona or DEFAULT_PERSONA
    reply = await deepseek.chat(request.text, conv_id, history=conv_history, persona=persona)

    # Strip markdown from reply before TTS and history storage
    reply = strip_markdown(reply)
    reply = naturalize_text(reply)

    # Save to history
    history.append(conv_id, request.text, reply)

    # 3. Detect emotion from reply
    emotion = detect_emotion(reply)
    logger.info(f"Detected emotion: {emotion}")

    # 4. Generate TTS audio with emotion and ambient
    if request.voice:
        tts.voice = request.voice
    audio_path, duration_ms = await tts.synthesize(reply, emotion=emotion, speed_ratio=request.speed)

    # 3. Store conversation context (for future multi-turn support)
    if conv_id not in conversations:
        conversations[conv_id] = []
    conversations[conv_id].append({"user": request.text, "assistant": reply})
    # Keep last 10 turns
    if len(conversations[conv_id]) > 10:
        conversations[conv_id] = conversations[conv_id][-10:]

    audio_filename = os.path.basename(audio_path)
    audio_url = f"/v1/audio/{audio_filename}"

    return ChatResponse(
        reply_text=reply,
        audio_url=audio_url,
        conversation_id=conv_id,
        duration_ms=duration_ms,
        emotion=emotion,
    )


@app.get("/v1/audio/{audio_id}")
async def get_audio(audio_id: str):
    """Serve a generated audio file."""
    # Support both with and without extension
    audio_path = AUDIO_DIR / audio_id
    if not audio_path.exists():
        audio_path = AUDIO_DIR / f"{audio_id}.mp3"
    if not audio_path.exists():
        audio_path = AUDIO_DIR / f"{audio_id}.wav"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")

    # Determine correct MIME type from file extension
    path_str = str(audio_path).lower()
    if path_str.endswith(".wav"):
        media_type = "audio/wav"
    elif path_str.endswith(".mp3"):
        media_type = "audio/mpeg"
    else:
        media_type = "audio/mpeg"

    return FileResponse(
        str(audio_path),
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="voicemate_{audio_id}"',
            "Cache-Control": "public, max-age=3600",
        },
    )


# ── ASR Engine (faster-whisper) ──────────────────────────────────────────────

class ASREngine:
    def __init__(self):
        self.model = None
        self.model_size = os.environ.get("VOICEMATE_ASR_MODEL", "base")

    async def transcribe(self, audio_path: str) -> str:
        """Transcribe audio file to text using faster-whisper."""
        try:
            from faster_whisper import WhisperModel

            if self.model is None:
                logger.info(f"Loading faster-whisper model '{self.model_size}'...")
                # Run model loading in a thread to avoid blocking
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    self.model = await asyncio.get_event_loop().run_in_executor(
                        pool, lambda: WhisperModel(self.model_size, device="cpu", compute_type="int8")
                    )
                logger.info("Whisper model loaded")

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                segments, info = await asyncio.get_event_loop().run_in_executor(
                    pool, lambda: self.model.transcribe(audio_path, language="zh", beam_size=5)
                )
                result = "".join(seg.text for seg in segments)
                logger.info(f"ASR: {result[:80]}")
                return result.strip()
        except Exception as e:
            logger.error(f"ASR failed: {e}")
            # Fallback: try OpenAI Whisper API if available
            openai_key = os.environ.get("VOICE_TOOLS_OPENAI_KEY", "")
            if openai_key:
                try:
                    import openai
                    client = openai.OpenAI(api_key=openai_key)
                    with open(audio_path, "rb") as f:
                        transcript = client.audio.transcriptions.create(
                            model="whisper-1",
                            file=f,
                            language="zh",
                        )
                        return transcript.text.strip()
                except Exception as e2:
                    logger.error(f"OpenAI ASR fallback also failed: {e2}")
            return ""


# ── Uploaded Audio → ASR → Chat → TTS ──────────────────────────────────────

@app.post("/v1/voice-chat")
async def voice_chat(
    audio: UploadFile = File(...),
    conversation_id: Optional[str] = Form(None),
):
    """Receive voice recording, transcribe, chat, reply with audio."""
    logger.info(f"Voice chat request: {audio.filename} ({conversation_id or 'new'})")

    # Save uploaded audio
    ext = os.path.splitext(audio.filename or "audio.webm")[1] or ".webm"
    audio_id = str(uuid.uuid4())[:8]
    input_path = str(AUDIO_DIR / f"input_{audio_id}{ext}")

    content = await audio.read()
    with open(input_path, "wb") as f:
        f.write(content)

    # Convert to wav for whisper if needed
    wav_path = str(AUDIO_DIR / f"input_{audio_id}.wav")
    import subprocess
    subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
        wav_path
    ], capture_output=True)

    logger.info(f"Audio saved: {input_path}, converted: {wav_path}")

    # Transcribe
    asr_engine = ASREngine()
    text = await asr_engine.transcribe(wav_path)

    if not text:
        return {"error": "无法识别语音内容", "reply_text": "抱歉，我没有听清楚你说什么", "audio_url": ""}

    logger.info(f"Transcribed: {text[:100]}")

    # Chat
    reply = await deepseek.chat(text, conversation_id)

    # TTS
    audio_path, duration_ms = await tts.synthesize(reply)

    conv_id = conversation_id or str(uuid.uuid4())
    audio_filename = os.path.basename(audio_path)
    audio_url = f"/v1/audio/{audio_filename}"

    return {
        "reply_text": reply,
        "audio_url": audio_url,
        "conversation_id": conv_id,
        "duration_ms": duration_ms,
        "transcribed_text": text,
    }


# ── WebSocket for Streaming Chat ────────────────────────────────────────────

@app.websocket("/v1/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        text = data.get("text", "").strip()
        conv_id = data.get("conversation_id") or str(uuid.uuid4())
        voice_name = data.get("voice")
        persona = data.get("persona") or DEFAULT_PERSONA

        if not text:
            await websocket.send_json({"type": "error", "message": "Text cannot be empty"})
            await websocket.close()
            return

        logger.info(f"WS chat [{conv_id}]: {text[:60]}")

        # Load history
        conv_history = history.load(conv_id)

        # Stream DeepSeek tokens
        full_reply = ""
        async for token in deepseek.stream_chat(text, conv_history, persona=persona):
            full_reply += token
            await websocket.send_json({"type": "token", "content": token})

        # Strip markdown before TTS and history storage
        clean_reply = strip_markdown(full_reply)
        clean_reply = naturalize_text(clean_reply)

        # Save history
        history.append(conv_id, text, clean_reply)

        # Generate TTS
        if voice_name:
            tts.voice = voice_name
        audio_path, duration_ms = await tts.synthesize(clean_reply)
        audio_url = f"/v1/audio/{os.path.basename(audio_path)}"

        await websocket.send_json({
            "type": "done",
            "audio_url": audio_url,
            "conversation_id": conv_id,
            "duration_ms": duration_ms,
            "full_text": clean_reply,
        })
    except WebSocketDisconnect:
        logger.info("WS client disconnected")
    except Exception as e:
        logger.error(f"WS error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        try:
            await websocket.close()
        except:
            pass


# ── Placeholder for Phase 2: real-time voice conversation

# Placeholder for Phase 2: real-time voice conversation
# @app.websocket("/v1/ws/voice")
# async def voice_websocket(websocket: WebSocket):
#     await websocket.accept()
#     # Stream audio in, stream audio out
#     # Uses streaming ASR + streaming LLM + streaming TTS
#     ...


# ── Proactive Push Messages ──────────────────────────────────────────────────

@app.get("/v1/proactive")
async def get_proactive_message(persona: str = "love"):
    import random
    PROACTIVE_MSGS = {
        "love": [
            "宝贝～在干嘛呢？人家想你了～",
            "你今天都没找我，我好委屈呀……",
            "悄悄告诉你，我今天梦到你了～",
            "在吗在吗？快出来陪我聊聊天～",
        ],
        "warm": ["今天过得怎么样？想聊聊吗？", "天气不错，心情好吗？", "突然想起你了，来打个招呼～"],
        "tsundere": ["哼，才不是特意找你的！", "干嘛呢干嘛呢，半天不说话", "喂，在不在？"],
        "genki": ["嗨嗨嗨！我来啦！", "元气满满的一天又开始啦！", "快出来快出来，有好玩的事！"],
        "sister": ["今天有没有好好吃饭？", "遇到什么烦心事了吗？跟姐姐说说"],
    }
    msgs = PROACTIVE_MSGS.get(persona, PROACTIVE_MSGS["love"])
    text = random.choice(msgs)
    return {"text": text, "persona": persona}


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if not DEEPSEEK_API_KEY:
        logger.error("DEEPSEEK_API_KEY not set! Set it in environment or .env file.")
        logger.info("Create a .env file with: DEEPSEEK_API_KEY=sk-...")
        sys.exit(1)

    logger.info(f"Starting VoiceMate API on {HOST}:{PORT}")
    logger.info(f"DeepSeek model: {DEEPSEEK_MODEL}")
    logger.info(f"TTS voice: {TTS_VOICE}")
    logger.info(f"Audio cache: {AUDIO_DIR}")

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()


