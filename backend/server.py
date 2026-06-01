from datetime import datetime, timedelta
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
import shutil
from collections import deque
from pathlib import Path
from typing import Optional
import aiohttp


try:
    import torch  # noqa: F401
except ImportError:
    torch = None

try:
    import soundfile as sf  # noqa: F401
except ImportError:
    sf = None

try:
    import numpy as np  # noqa: F401
except ImportError:
    np = None
import struct
import io
import math

# ChatTTS compatibility: PyTorch 2.12+ requires weights_only=False for tokenizer
try:
    import ChatTTS.core as _chattts_core  # noqa: F401
except ImportError:
    _chattts_core = None
# The tokenizer path is already patched to use weights_only=False

# Load .env file if present
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

try:
    from livekit.api import AccessToken, VideoGrants
except ImportError:
    AccessToken = None
    VideoGrants = None
# ── Config ──────────────────────────────────────────────────────────────────

HOST = os.environ.get("VOICEMATE_HOST", "0.0.0.0")
PORT = int(os.environ.get("VOICEMATE_PORT", "8000"))
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
AUDIO_DIR = Path(os.environ.get("VOICEMATE_AUDIO_DIR", str(BASE_DIR / "audio_cache")))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

TTS_VOICE = os.environ.get("VOICEMATE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")  # edge-tts Chinese female
VOICEMATE_TTS_PROVIDER = os.environ.get("VOICEMATE_TTS_PROVIDER", "auto").strip().lower()

# Fish Audio for voice cloning
FISH_AUDIO_API_KEY = os.environ.get("FISH_AUDIO_API_KEY", "")
FISH_AUDIO_BASE_URL = "https://api.fish.audio/v1"
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_TTS_MODEL = os.environ.get("MIMO_TTS_MODEL", "mimo-v2.5-tts")
MIMO_TTS_VOICE = os.environ.get("MIMO_TTS_VOICE", "\u51b0\u7cd6")
MIMO_TTS_CONTEXT = os.environ.get("MIMO_TTS_CONTEXT", "")
MIMO_VOICE_SAMPLE = os.environ.get("MIMO_VOICE_SAMPLE", "")
TTS_RATE = os.environ.get("VOICEMATE_TTS_RATE", "+0%")
TTS_VOLUME = os.environ.get("VOICEMATE_TTS_VOLUME", "+0%")
CLONE_DIR = Path(os.environ.get("VOICEMATE_CLONE_DIR", str(PROJECT_ROOT / "cloned_voices")))
CLONE_DIR.mkdir(parents=True, exist_ok=True)

# Filler/filler words to skip LLM calls (user thinking noises like "嗯", "um", "uh")
FILLER_WORDS = {
    "嗯", "呃", "啊", "哦", "噢", "吖", "哈", "嘿",
    "um", "uh", "ah", "er", "hmm",
}


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

class CloneVoiceInfo(BaseModel):
    """Stored info about a cloned voice."""
    voice_id: str
    name: str = "我的声音"
    status: str = "completed"  # pending, processing, completed, failed
    created_at: str = ""



class HistoryManager:
    """Persists conversation history to disk for context memory."""
    def __init__(self, history_dir=None):
        history_dir = history_dir or os.environ.get("VOICEMATE_HISTORY_DIR") or str(BASE_DIR / "history")
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
        self.api_key = DEEPSEEK_API_KEY or os.environ.get("OPENAI_API_KEY", "") or "missing-key"
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
        # Inject current time for temporal context
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M %A")
        # Map English weekday to Chinese
        weekday_map = {"Monday": "星期一", "Tuesday": "星期二", "Wednesday": "星期三",
                       "Thursday": "星期四", "Friday": "星期五", "Saturday": "星期六", "Sunday": "星期日"}
        cn_weekday = weekday_map.get(current_time.split()[-1], current_time.split()[-1])
        current_time_cn = current_time.rsplit(" ", 1)[0] + " " + cn_weekday
        messages.append({"role": "user", "content": f"现在是北京时间 {current_time_cn}"})
        messages.append({"role": "assistant", "content": f"知道了，现在是 {current_time_cn}！"})
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
    "affectionate": ["想你", "抱抱", "亲亲", "宝贝", "想你了", "撒娇", "人家", "喜欢你", "陪我", "贴贴"],
    "cheerful": ["哈哈", "开心", "太好", "真棒", "耶", "棒", "好开心", "真好", "嘻嘻", "厉害", "不错"],
    "sad": ["唉", "难过", "伤心", "不开心", "委屈", "哭", "难受", "呜呜", "失落", "累了", "疼"],
    "angry": ["哼", "气", "生气", "烦", "讨厌", "气死", "真是的", "不理你", "过分"],
    "embarrassed": ["害羞", "不好意思", "脸红", "好害羞", "讨厌啦", "别这样", "羞"],
}

def detect_emotion(text: str) -> str:
    for emotion, keywords in EMOTION_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return emotion
    return "gentle"


EMOTION_TTS_PROFILES = {
    "cheerful": {"voice": "zh-CN-XiaoyiNeural", "rate": "+14%", "pitch": "+42Hz", "prefix": "嘿嘿，"},
    "affectionate": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "-3%", "pitch": "+24Hz", "prefix": "嗯，"},
    "sad": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "-18%", "pitch": "-28Hz", "prefix": "唉，"},
    "angry": {"voice": "zh-CN-XiaoyiNeural", "rate": "+6%", "pitch": "-20Hz", "prefix": "哼，"},
    "embarrassed": {"voice": "zh-CN-XiaoyiNeural", "rate": "-5%", "pitch": "+36Hz", "prefix": "啊，"},
    "gentle": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "-4%", "pitch": "+8Hz", "prefix": ""},
}


def prepare_tts_text(text: str, emotion: str = "gentle") -> str:
    """Make synthesized speech less flat with light emotion cues and pauses."""
    clean = naturalize_text(strip_markdown(text or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return clean

    profile = EMOTION_TTS_PROFILES.get(emotion, EMOTION_TTS_PROFILES["gentle"])
    prefix = profile.get("prefix", "")
    if prefix and not clean.startswith(prefix) and len(clean) > 8:
        clean = prefix + clean

    clean = re.sub(r"([。！？!?])", r"\1 ", clean)
    clean = re.sub(r"([，、；;])", r"\1 ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"([，。！？、；])\s+", r"\1", clean)

    if len(clean) > 34 and "，" not in clean[:34]:
        pos = min(max(len(clean) // 2, 12), 26)
        clean = clean[:pos] + "，" + clean[pos:]
    return clean



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

    async def synthesize(
        self,
        text: str,
        emotion: str = "gentle",
        speed_ratio: Optional[float] = None,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
        volume: Optional[str] = None,
    ) -> tuple[str, int]:
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
        
        # Convert to 16-bit PCM for iOS AVAudioPlayer compatibility
        import subprocess
        pcm_path = str(AUDIO_DIR / f"{audio_id}_pcm.wav")
        subprocess.run([
            "ffmpeg", "-y", "-i", output_path,
            "-acodec", "pcm_s16le", "-ar", "24000", "-ac", "1",
            pcm_path
        ], capture_output=True)
        os.replace(pcm_path, output_path)

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


class FishAudioTTS:
    """Fish Audio API integration for TTS with cloned voices.
    
    Uses Fish Audio REST API directly (no official Python SDK).
    Supports voice cloning from uploaded samples and TTS with cloned voices.
    Docs: https://fish.audio/api/
    """
    BASE_URL = FISH_AUDIO_BASE_URL
    
    def __init__(self):
        self.api_key = FISH_AUDIO_API_KEY
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Bearer {self.api_key}",
            })
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    @property
    def is_available(self) -> bool:
        return bool(self.api_key)
    
    async def create_voice(self, audio_files: list[tuple[str, bytes]], 
                           name: str = "voicemate_clone") -> str:
        """Upload audio samples to Fish Audio and create a cloned voice.
        
        Returns the voice_id on success.
        Raises HTTPException on failure.
        """
        if not self.is_available:
            raise HTTPException(status_code=400, detail="Fish Audio API key not configured")
        
        session = await self._get_session()
        
        # Fish Audio model training API: POST /v1/voices
        # Accepts up to 10 audio files, each 5-30 seconds
        form = aiohttp.FormData()
        for filename, data in audio_files:
            form.add_field("files", data, filename=filename, content_type="audio/m4a")
        form.add_field("name", name)
        form.add_field("title", name)
        form.add_field("description", "Voice clone for VoiceMate app")
        
        url = f"{self.BASE_URL}/voices"
        async with session.post(url, data=form) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error(f"Fish Audio create_voice failed ({resp.status}): {body}")
                raise HTTPException(status_code=502, detail=f"Fish Audio API error: {body}")
            result = await resp.json()
            voice_id = result.get("voice_id", "")
            if not voice_id:
                raise HTTPException(status_code=502, detail="Fish Audio: no voice_id in response")
            logger.info(f"Fish Audio voice created: {voice_id}")
            return voice_id
    
    async def get_voice_status(self, voice_id: str) -> dict:
        """Check the training/status of a cloned voice."""
        session = await self._get_session()
        url = f"{self.BASE_URL}/voices/{voice_id}"
        async with session.get(url) as resp:
            if resp.status != 200:
                return {"status": "unknown"}
            return await resp.json()
    
    async def synthesize(self, text: str, voice_id: str,
                         speed_ratio: float = 1.0) -> tuple[str, int]:
        """Synthesize speech using a cloned voice.
        
        Returns (audio_file_path, duration_ms).
        """
        session = await self._get_session()
        
        url = f"{self.BASE_URL}/tts"
        payload = {
            "text": text,
            "voice_id": voice_id,
            "speed": speed_ratio,
            "format": "mp3",
        }
        
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error(f"Fish Audio TTS failed ({resp.status}): {body}")
                raise RuntimeError(f"Fish Audio TTS failed: {body}")
            
            audio_data = await resp.read()
            
            audio_id = str(uuid.uuid4())[:8]
            output_path = str(AUDIO_DIR / f"fish_{audio_id}.mp3")
            with open(output_path, "wb") as f:
                f.write(audio_data)
            
            # Estimate duration from audio data
            duration_ms = max(int(len(audio_data) / 2400 * 1000), 1000)
            logger.info(f"Fish Audio TTS (voice {voice_id[:12]}...) -> {output_path} ({duration_ms}ms)")
            return output_path, duration_ms


class MiMoTTS:
    """Xiaomi MiMo V2.5 TTS through the OpenAI-compatible API."""

    PRESET_VOICES = {
        "\u51b0\u7cd6",  # Bingtang
        "\u8309\u8389",  # Moli
        "\u82cf\u6253",  # Suda
        "\u767d\u6866",  # Baihua
        "Mia",
        "Chloe",
        "Milo",
        "Dean",
    }

    EMOTION_CONTEXT = {
        "cheerful": "\u7528\u8f7b\u5feb\u4e0a\u626c\u7684\u8bed\u6c14\uff0c\u8bed\u901f\u7a0d\u5feb\uff0c\u5e26\u660e\u4eae\u7684\u5f00\u5fc3\u611f\u3002",
        "affectionate": "\u7528\u6e29\u67d4\u4eb2\u8fd1\u7684\u8bed\u6c14\uff0c\u8bed\u901f\u7a0d\u6162\uff0c\u5e26\u4e00\u70b9\u8f7b\u58f0\u548c\u4f9d\u604b\u611f\u3002",
        "sad": "\u7528\u4f4e\u843d\u3001\u8f7b\u58f0\u7684\u8bed\u6c14\uff0c\u505c\u987f\u7a0d\u591a\uff0c\u4e0d\u8981\u5938\u5f20\u54ed\u8154\u3002",
        "angry": "\u7528\u514b\u5236\u7684\u4e0d\u6ee1\u8bed\u6c14\uff0c\u91cd\u97f3\u66f4\u660e\u786e\uff0c\u4f46\u4e0d\u8981\u558a\u53eb\u3002",
        "embarrassed": "\u7528\u5bb3\u7f9e\u3001\u5c0f\u58f0\u3001\u7565\u5e26\u72b9\u8c6b\u7684\u8bed\u6c14\uff0c\u8bed\u5c3e\u653e\u8f7b\u3002",
        "gentle": "\u7528\u81ea\u7136\u3001\u6e29\u67d4\u3001\u50cf\u771f\u4eba\u804a\u5929\u7684\u8bed\u6c14\uff0c\u6709\u8f7b\u5fae\u505c\u987f\u3002",
    }

    def __init__(self):
        self.api_key = MIMO_API_KEY
        self.base_url = MIMO_BASE_URL
        self.model = MIMO_TTS_MODEL
        self.voice = MIMO_TTS_VOICE
        self.context = MIMO_TTS_CONTEXT
        self.voice_sample = MIMO_VOICE_SAMPLE

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _parse_voice(self, voice: Optional[str]) -> Optional[str]:
        if not voice:
            return None
        for prefix in ("mimo:", "mimo_"):
            if voice.startswith(prefix):
                return voice[len(prefix):]
        if voice in self.PRESET_VOICES:
            return voice
        return None

    def _voice_sample_data_url(self) -> str:
        if not self.voice_sample:
            raise RuntimeError("MIMO_VOICE_SAMPLE is required for MiMo voiceclone model")
        path = Path(self.voice_sample)
        if not path.exists():
            raise RuntimeError(f"MiMo voice sample not found: {path}")
        suffix = path.suffix.lower()
        mime_type = {".mp3": "audio/mpeg", ".wav": "audio/wav"}.get(suffix)
        if not mime_type:
            raise RuntimeError("MiMo voice sample must be mp3 or wav")
        data = path.read_bytes()
        if len(data) > 10 * 1024 * 1024:
            raise RuntimeError("MiMo voice sample must be <= 10 MB")
        import base64
        return f"data:{mime_type};base64,{base64.b64encode(data).decode('utf-8')}"

    def _build_context(self, emotion: str) -> str:
        parts = []
        if self.context:
            parts.append(self.context.strip())
        parts.append(self.EMOTION_CONTEXT.get(emotion, self.EMOTION_CONTEXT["gentle"]))
        return "\n".join(p for p in parts if p)

    async def synthesize(
        self,
        text: str,
        emotion: str = "gentle",
        speed_ratio: Optional[float] = None,
        voice: Optional[str] = None,
    ) -> tuple[str, int]:
        if not self.is_available:
            raise RuntimeError("MIMO_API_KEY is not configured")

        from openai import AsyncOpenAI
        import base64

        clean_text = prepare_tts_text(text, emotion)
        context = self._build_context(emotion)
        if speed_ratio is not None:
            if speed_ratio < 0.9:
                context += "\n\u8bed\u901f\u653e\u6162\uff0c\u7559\u51fa\u66f4\u81ea\u7136\u7684\u505c\u987f\u3002"
            elif speed_ratio > 1.1:
                context += "\n\u8bed\u901f\u7a0d\u5feb\uff0c\u8bed\u6c14\u66f4\u8f7b\u5feb\u8fde\u8d2f\u3002"

        messages = []
        if context:
            messages.append({"role": "user", "content": context})
        messages.append({"role": "assistant", "content": clean_text})

        audio: dict[str, str] = {"format": "wav"}
        selected_voice = self._parse_voice(voice) or self.voice
        if self.model == "mimo-v2.5-tts":
            audio["voice"] = selected_voice
        elif self.model == "mimo-v2.5-tts-voiceclone":
            audio["voice"] = self._voice_sample_data_url()

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        completion = await client.chat.completions.create(
            model=self.model,
            messages=messages,
            audio=audio,
        )
        message = completion.choices[0].message
        audio_data = getattr(getattr(message, "audio", None), "data", None)
        if not audio_data:
            raise RuntimeError("MiMo TTS returned no audio data")

        raw_audio = base64.b64decode(audio_data)
        audio_id = str(uuid.uuid4())[:8]
        output_path = str(AUDIO_DIR / f"mimo_{audio_id}.wav")
        with open(output_path, "wb") as f:
            f.write(raw_audio)

        duration_ms = max(int((len(clean_text) / 5) * 1000), 1000)
        logger.info(f"MiMo TTS [{self.model}/{selected_voice}/{emotion}] -> {output_path} ({duration_ms}ms)")
        return output_path, duration_ms


class TTSEngine:


    def __init__(self):
        self.voice = TTS_VOICE
        self.rate = TTS_RATE
        self.volume = TTS_VOLUME
        self.pitch = "+0Hz"
        self.chattts = ChatTTSEngine()
        self.volc = VolcengineTTS()
        self.fish = FishAudioTTS()
        self.mimo = MiMoTTS()

    async def _synthesize_edge_tts(
        self,
        text: str,
        emotion: str = "gentle",
        speed_ratio: Optional[float] = None,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
        volume: Optional[str] = None,
    ) -> tuple[str, int]:
        """Synthesize speech using edge_tts with the given voice and parameters."""
        import edge_tts

        audio_id = str(uuid.uuid4())[:8]
        raw_path = str(AUDIO_DIR / f"raw_{audio_id}.mp3")
        output_path = str(AUDIO_DIR / f"{audio_id}.mp3")

        # Use local variables only — never mutate instance state
        text = prepare_tts_text(text, emotion)
        profile = EMOTION_TTS_PROFILES.get(emotion, EMOTION_TTS_PROFILES["gentle"])
        effective_voice = voice or self.voice
        # Only override voice by emotion if caller didn't specify a voice
        if voice is None:
            effective_voice = profile.get("voice", effective_voice)
        effective_rate = rate or profile.get("rate", self.rate)
        effective_pitch = pitch or profile.get("pitch", self.pitch)
        effective_volume = volume or self.volume

        # speed_ratio override: if App provides speed, use it over emotion rate
        if speed_ratio is not None:
            # Map 0.5~2.0 → edge_tts rate format: +0%, +50%, -20%, etc.
            effective_rate = f"{int((speed_ratio - 1) * 100):+d}%"

        communicate = edge_tts.Communicate(
            text,
            effective_voice,
            rate=effective_rate,
            volume=effective_volume,
            pitch=effective_pitch,
        )

        start = time.time()
        try:
            await communicate.save(raw_path)
        except Exception as e:
            logger.warning(f"edge_tts failed with voice {effective_voice}: {e}")
            logger.info("Falling back to zh-CN-XiaoxiaoNeural (default)")
            communicate = edge_tts.Communicate(
                text,
                "zh-CN-XiaoxiaoNeural",
                rate=effective_rate,
                volume=effective_volume,
                pitch=effective_pitch,
            )
            await communicate.save(raw_path)
        elapsed = time.time() - start

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
            # No ambient file, just copy. Use Python copy for Windows compatibility.
            shutil.copyfile(raw_path, output_path)

        logger.info(f"TTS generated [{emotion}] in {elapsed:.2f}s -> {output_path} ({duration_ms}ms)")
        return output_path, duration_ms


    async def synthesize(
        self,
        text: str,
        emotion: str = "gentle",
        speed_ratio: Optional[float] = None,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
        volume: Optional[str] = None,
    ) -> tuple[str, int]:
        """Convert text to speech with emotion, return (audio_path, duration_ms).
        
        Priority:
          - voice starts with "fish_" → Fish Audio cloned voice
          - voice is explicitly specified → edge_tts directly
          - If no voice specified → ChatTTS (local GPU) → Volcengine (cloud API) → edge_tts (default voice)

        """
        # Cloned voice via Fish Audio
        if voice is not None and voice.startswith("fish_") and self.fish.is_available:
            fish_voice_id = voice[5:]  # strip "fish_" prefix
            try:
                return await self.fish.synthesize(text, fish_voice_id, speed_ratio or 1.0)
            except Exception as e:
                logger.warning(f"Fish Audio TTS failed (voice={fish_voice_id[:12]}...), falling back: {e}")

        wants_mimo = (
            VOICEMATE_TTS_PROVIDER == "mimo"
            or (voice is not None and (
                voice.startswith("mimo:")
                or voice.startswith("mimo_")
                or voice in self.mimo.PRESET_VOICES
            ))
        )
        if wants_mimo:
            try:
                return await self.mimo.synthesize(text, emotion=emotion, speed_ratio=speed_ratio, voice=voice)
            except Exception as e:
                logger.warning(f"MiMo TTS failed, falling back: {e}")
        
        # If user explicitly chose a voice (non-clone), use edge_tts directly
        if voice is not None:
            return await self._synthesize_edge_tts(text, emotion, speed_ratio, voice, rate, pitch, volume)

        # 1) ChatTTS (local GPU, most natural)
        try:
            return await self.chattts.synthesize(text, emotion=emotion, speed_ratio=speed_ratio)
        except Exception as e:
            logger.warning(f"ChatTTS failed, falling back: {e}")

        # 2) MiMo if configured and auto mode is enabled
        if VOICEMATE_TTS_PROVIDER == "auto" and self.mimo.is_available:
            try:
                return await self.mimo.synthesize(text, emotion=emotion, speed_ratio=speed_ratio)
            except Exception as e:
                logger.warning(f"MiMo TTS failed, falling back: {e}")

        # 3) Volcengine if configured
        if self.volc.appid and self.volc.token:
            try:
                path = await self.volc.synthesize(text, emotion=emotion, speed_ratio=speed_ratio)
                duration_ms = max(int((len(text) / 5) * 1000), 1000)
                logger.info(f"Volcengine TTS generated [{emotion}] -> {path} ({duration_ms}ms)")
                return path, duration_ms
            except Exception as e:
                logger.warning(f"Volcengine TTS failed, falling back to edge-tts: {e}")

        # 4) edge_tts fallback
        return await self._synthesize_edge_tts(text, emotion, speed_ratio, voice, rate, pitch, volume)



# ── Initialize Services ─────────────────────────────────────────────────────

deepseek = DeepSeekClient()
tts = TTSEngine()
history = HistoryManager()
# Load cloned voices database
# _load_clone_db() — moved to after function definition


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
    audio_path, duration_ms = await tts.synthesize(reply, emotion=emotion, speed_ratio=request.speed, voice=request.voice)

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
    audio_path, duration_ms = await tts.synthesize(reply, voice=None)

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



class WSConnectionState:
    """Tracks per-connection state to prevent concurrent request handling."""
    def __init__(self):
        self.lock = asyncio.Lock()
        self._processing = False

    @property
    def is_processing(self) -> bool:
        return self._processing

    @is_processing.setter
    def is_processing(self, value: bool):
        self._processing = value


_ws_connections: dict[WebSocket, WSConnectionState] = {}


# ── WebSocket for Streaming Chat ────────────────────────────────────────────

@app.websocket("/v1/ws/chat")
async def ws_chat(websocket: WebSocket):
    state = WSConnectionState()
    _ws_connections[websocket] = state
    await websocket.accept()
    logger.info(f"WS client connected: {websocket.client}")

    async def heartbeat(interval: float = 25.0):
        """Send ping every  seconds while the connection is alive."""
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        try:
            data = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"WS timeout: no message from {websocket.client} within 30s")
            await websocket.send_json({"type": "error", "message": "Request timed out: no message received within 30 seconds"})
            await websocket.close(code=1008)
            return

        if state.is_processing:
            logger.warning(f"WS concurrency violation: {websocket.client} sent multiple requests")
            await websocket.send_json({"type": "error", "message": "Only one request at a time per connection"})
            return

        async with state.lock:
            state.is_processing = True

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
            audio_path, duration_ms = await tts.synthesize(clean_reply, voice=voice_name)
            audio_url = f"/v1/audio/{os.path.basename(audio_path)}"

            await websocket.send_json({
                "type": "done",
                "audio_url": audio_url,
                "conversation_id": conv_id,
                "duration_ms": duration_ms,
                "full_text": clean_reply,
            })
    except WebSocketDisconnect:
        logger.info(f"WS client disconnected: {websocket.client}")
    except Exception as e:
        logger.exception(f"WS error [{type(e).__name__}]: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        heartbeat_task.cancel()
        _ws_connections.pop(websocket, None)
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"WS connection cleaned up: {websocket.client}")


# ── Placeholder for Phase 2: real-time voice conversation


# ── Voice Clone API (Fish Audio) ──────────────────────────────────────────────

VOICE_CLONE_DB = {}  # In-memory: {voice_id: {"name": str, "status": str, "created_at": str}}


@app.post("/v1/clone/upload")
async def clone_upload(files: list[UploadFile] = File(...)):
    """Upload voice samples and create a Fish Audio cloned voice.
    
    Accepts 3-10 audio recordings (5-30 seconds each) of the same speaker.
    Returns a voice_id that can be used as the `voice` parameter in chat.
    """
    if not tts.fish.is_available:
        raise HTTPException(status_code=400, detail="Fish Audio API key not configured. Set FISH_AUDIO_API_KEY in .env")
    
    if len(files) < 3:
        raise HTTPException(status_code=400, detail="至少需要上传 3 段录音样本")
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="最多上传 10 段录音样本")
    
    # Read all audio files
    audio_files: list[tuple[str, bytes]] = []
    for f in files:
        data = await f.read()
        if len(data) == 0:
            continue
        if len(data) > 10 * 1024 * 1024:  # 10MB max per file
            raise HTTPException(status_code=400, detail=f"文件 {f.filename} 太大，每个文件最大 10MB")
        audio_files.append((f.filename or f"sample_{len(audio_files)}.m4a", data))
    
    if len(audio_files) < 3:
        raise HTTPException(status_code=400, detail="需要至少 3 个非空音频文件")
    
    # Create voice via Fish Audio
    voice_id = await tts.fish.create_voice(audio_files)
    
    # Save metadata locally
    voice_info = {
        "voice_id": voice_id,
        "name": "我的声音",
        "status": "processing",  # Fish Audio may need time to train
        "created_at": datetime.utcnow().isoformat(),
    }
    
    # Store in memory + persistent JSON
    VOICE_CLONE_DB[voice_id] = voice_info
    _save_clone_db()
    
    return {
        "voice_id": f"fish_{voice_id}",
        "status": "processing",
        "message": "声音样本已上传，正在生成克隆声音（可能需要几分钟）",
    }


@app.get("/v1/clone/voices")
async def list_cloned_voices():
    """List all cloned voices."""
    return {"voices": list(VOICE_CLONE_DB.values())}


@app.get("/v1/clone/status/{voice_id}")
async def clone_status(voice_id: str):
    """Check the status of a voice clone training job."""
    # Strip fish_ prefix if provided
    raw_id = voice_id[5:] if voice_id.startswith("fish_") else voice_id
    
    info = VOICE_CLONE_DB.get(raw_id)
    if not info:
        raise HTTPException(status_code=404, detail="Voice clone not found")
    
    # Optionally refresh status from Fish Audio
    if info["status"] == "processing":
        try:
            remote = await tts.fish.get_voice_status(raw_id)
            remote_status = remote.get("status", "completed")
            if remote_status == "completed":
                info["status"] = "completed"
                _save_clone_db()
        except Exception:
            pass  # Keep local status
    
    return {
        "voice_id": f"fish_{raw_id}",
        "status": info["status"],
        "name": info["name"],
        "created_at": info["created_at"],
    }


@app.delete("/v1/clone/voices/{voice_id}")
async def delete_cloned_voice(voice_id: str):
    """Delete a cloned voice."""
    raw_id = voice_id[5:] if voice_id.startswith("fish_") else voice_id
    if raw_id in VOICE_CLONE_DB:
        del VOICE_CLONE_DB[raw_id]
        _save_clone_db()
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Voice clone not found")


def _save_clone_db():
    """Persist clone DB to disk."""
    db_path = CLONE_DIR / "clone_db.json"
    try:
        db_path.write_text(json.dumps(VOICE_CLONE_DB, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error(f"Failed to save clone DB: {e}")


def _load_clone_db():
    """Load clone DB from disk on startup."""
    db_path = CLONE_DIR / "clone_db.json"
    if db_path.exists():
        try:
            data = json.loads(db_path.read_text())
            VOICE_CLONE_DB.update(data)
            logger.info(f"Loaded {len(data)} cloned voices from disk")
        except Exception as e:
            logger.error(f"Failed to load clone DB: {e}")


# Initialize clone DB
_load_clone_db()


# ── Real-Time Voice Conversation (Full-Duplex) ──────────────────────────────


# WebRTC VAD configuration
try:
    import webrtcvad
except ImportError:
    webrtcvad = None
VAD_FRAME_MS = 30           # webrtcvad requires 10/20/30ms frames; 30ms = 480 bytes at 16kHz 16-bit
VAD_NOISE_FLOOR_DECAY = 0.97     # leaky integrator decay for tracking noise floor: 0.97 = even slower adaptation (〜33 frames to rise)
VAD_NOISE_FLOOR_INIT = 80.0      # initial noise floor estimate (RMS) — higher to avoid initial false triggers
VAD_SPEECH_RATIO = 2.2           # frame is speech if RMS >= noise_floor * VAD_SPEECH_RATIO — lower = more sensitive to quiet speech
VAD_FLOOR_MIN = 30.0             # minimum noise floor to prevent division issues in very quiet environments
SILENCE_DURATION_MS = 1200  # ms of silence before considering utterance complete (slightly longer to ensure true silence)
MIN_UTTERANCE_MS = 800     # minimum utterance length to process (ms) — ignore very short noise bursts
SAMPLE_RATE = 16000        # iOS sends 16kHz PCM16 mono
BYTES_PER_SAMPLE = 2

# Conservative defaults reduce false triggers from room noise and background speech.
VAD_NOISE_FLOOR_DECAY = float(os.environ.get("VOICEMATE_VAD_NOISE_FLOOR_DECAY", "0.98"))
VAD_NOISE_FLOOR_INIT = float(os.environ.get("VOICEMATE_VAD_NOISE_FLOOR_INIT", "120.0"))
VAD_SPEECH_RATIO = float(os.environ.get("VOICEMATE_VAD_SPEECH_RATIO", "2.8"))
VAD_FLOOR_MIN = float(os.environ.get("VOICEMATE_VAD_FLOOR_MIN", "45.0"))
SILENCE_DURATION_MS = int(os.environ.get("VOICEMATE_VAD_SILENCE_MS", "850"))
MIN_UTTERANCE_MS = int(os.environ.get("VOICEMATE_VAD_MIN_UTTERANCE_MS", "700"))
VAD_PRE_ROLL_MS = int(os.environ.get("VOICEMATE_VAD_PRE_ROLL_MS", "300"))
VAD_CONFIRM_FRAMES = int(os.environ.get("VOICEMATE_VAD_CONFIRM_FRAMES", "6"))
VAD_SHORT_SILENCE_MS = int(os.environ.get("VOICEMATE_VAD_SHORT_SILENCE_MS", "520"))
VAD_LONG_SILENCE_MS = int(os.environ.get("VOICEMATE_VAD_LONG_SILENCE_MS", "1050"))
VAD_SHORT_UTTERANCE_MS = int(os.environ.get("VOICEMATE_VAD_SHORT_UTTERANCE_MS", "900"))
VAD_LONG_UTTERANCE_MS = int(os.environ.get("VOICEMATE_VAD_LONG_UTTERANCE_MS", "2600"))
BARGE_IN_MIN_UTTERANCE_MS = int(os.environ.get("VOICEMATE_BARGE_IN_MIN_UTTERANCE_MS", "420"))
SEMANTIC_TURN_ENABLED = os.environ.get("VOICEMATE_SEMANTIC_TURN_ENABLED", "1").lower() in {"1", "true", "yes"}
SEMANTIC_TURN_DELAY_MS = int(os.environ.get("VOICEMATE_SEMANTIC_TURN_DELAY_MS", "900"))
SEMANTIC_TURN_MIN_CHARS = int(os.environ.get("VOICEMATE_SEMANTIC_TURN_MIN_CHARS", "3"))


SEMANTIC_CONTINUE_WORDS = (
    "如果", "假如", "要是", "因为", "但是", "然后", "所以", "比如", "就是",
    "关于", "还有", "那个", "这个", "我想", "我想问", "我想问你", "我在想",
    "你觉得", "你说", "能不能", "可不可以", "是不是", "等一下", "先别",
    "if", "because", "but", "and then", "so", "for example", "i want to ask",
    "do you think",
)
SEMANTIC_FINAL_PUNCT = "。！？!?~～"


def _semantic_compact_text(text: str) -> str:
    return re.sub(r"[\s，,。！？!?、；;：:~～…]+", "", (text or "").strip().lower())


def is_semantically_incomplete(text: str) -> bool:
    if not SEMANTIC_TURN_ENABLED:
        return False
    stripped = (text or "").strip()
    compact = _semantic_compact_text(stripped)
    if len(compact) < SEMANTIC_TURN_MIN_CHARS:
        return False
    if stripped[-1:] in SEMANTIC_FINAL_PUNCT:
        return False
    if any(compact.endswith(_semantic_compact_text(word)) for word in SEMANTIC_CONTINUE_WORDS):
        return True
    if any(compact.startswith(_semantic_compact_text(word)) for word in ("如果", "假如", "要是", "if")):
        return True
    if re.search(r"(我想|我想问|我在想|你觉得|如果|假如|因为|但是|然后|比如)$", compact):
        return True
    return False


class SemanticTurnGate:
    """Delay obviously unfinished ASR text before calling the LLM."""

    def __init__(self):
        self.pending_task: Optional[asyncio.Task] = None

    def cancel(self):
        if self.pending_task and not self.pending_task.done():
            self.pending_task.cancel()
        self.pending_task = None

    async def submit(self, text: str, callback):
        self.cancel()
        if not is_semantically_incomplete(text):
            await callback(text)
            return

        async def _delayed():
            try:
                await asyncio.sleep(SEMANTIC_TURN_DELAY_MS / 1000)
                await callback(text)
            except asyncio.CancelledError:
                pass

        self.pending_task = asyncio.create_task(_delayed())


class SpeechTurnDetector:
    """Detect a complete user turn from PCM16 frames."""

    def __init__(self, vad):
        self.vad = vad
        self.frame_size = VAD_FRAME_MS * SAMPLE_RATE // 1000 * BYTES_PER_SAMPLE
        self.pre_roll_max = max(1, VAD_PRE_ROLL_MS // VAD_FRAME_MS)
        self.pending = bytearray()
        self.pre_roll = deque(maxlen=self.pre_roll_max)
        self.noise_floor = VAD_NOISE_FLOOR_INIT
        self.reset_turn(clear_preroll=True)

    def reset_turn(self, *, clear_preroll: bool = False):
        self.active = False
        self.speech_confirm_frames = 0
        self.silence_ms = 0.0
        self.speech_ms = 0.0
        self.buffer = bytearray()
        if clear_preroll:
            self.pre_roll.clear()

    def reset_after_ai_turn(self):
        self.reset_turn(clear_preroll=True)
        self.noise_floor = VAD_NOISE_FLOOR_INIT

    def accept(self, pcm_chunk: bytes, *, ai_speaking: bool = False) -> list[dict]:
        turns = []
        self.pending.extend(pcm_chunk)
        while len(self.pending) >= self.frame_size:
            frame = bytes(self.pending[:self.frame_size])
            self.pending = self.pending[self.frame_size:]
            turn = self._push_frame(frame, ai_speaking=ai_speaking)
            if turn:
                turns.append(turn)
        return turns

    def _frame_rms(self, frame: bytes) -> float:
        samples = struct.unpack_from(f"<{len(frame) // BYTES_PER_SAMPLE}h", frame)
        return math.sqrt(sum(s * s for s in samples) / len(samples))

    def _is_speech_frame(self, frame: bytes, *, ai_speaking: bool) -> tuple[bool, float]:
        rms = self._frame_rms(frame)
        ratio = VAD_SPEECH_RATIO * (1.55 if ai_speaking else 1.0)
        if rms < self.noise_floor * ratio:
            return False, rms
        return self.vad.is_speech(frame, SAMPLE_RATE), rms

    def _end_silence_ms(self) -> int:
        if self.speech_ms <= VAD_SHORT_UTTERANCE_MS:
            return VAD_SHORT_SILENCE_MS
        if self.speech_ms >= VAD_LONG_UTTERANCE_MS:
            return VAD_LONG_SILENCE_MS
        return SILENCE_DURATION_MS

    def _push_frame(self, frame: bytes, *, ai_speaking: bool) -> Optional[dict]:
        is_speech, rms = self._is_speech_frame(frame, ai_speaking=ai_speaking)

        if not self.active:
            self.pre_roll.append(frame)
            if not is_speech and rms < self.noise_floor:
                self.noise_floor = self.noise_floor * VAD_NOISE_FLOOR_DECAY + rms * (1 - VAD_NOISE_FLOOR_DECAY)
                self.noise_floor = max(self.noise_floor, VAD_FLOOR_MIN)

        if is_speech:
            if not self.active:
                confirm_frames_needed = VAD_CONFIRM_FRAMES * (2 if ai_speaking else 1)
                self.speech_confirm_frames += 1
                if self.speech_confirm_frames >= confirm_frames_needed:
                    self.active = True
                    self.buffer = bytearray(b"".join(self.pre_roll))
                    self.speech_ms = self.speech_confirm_frames * VAD_FRAME_MS
                    self.silence_ms = 0.0
                return None

            self.buffer.extend(frame)
            self.speech_ms += VAD_FRAME_MS
            self.silence_ms = 0.0
            return None

        if not self.active:
            self.speech_confirm_frames = 0
            return None

        self.buffer.extend(frame)
        self.silence_ms += VAD_FRAME_MS
        if self.silence_ms < self._end_silence_ms():
            return None

        turn = {
            "audio": bytes(self.buffer),
            "speech_ms": self.speech_ms,
            "silence_ms": self.silence_ms,
            "total_ms": len(self.buffer) / (SAMPLE_RATE * BYTES_PER_SAMPLE) * 1000,
            "noise_floor": self.noise_floor,
        }
        self.reset_turn(clear_preroll=True)
        return turn


async def _stream_tts_to_websocket(websocket, text: str, voice=None, speed_ratio=None):
    """Generate TTS audio and stream it as PCM chunks via WebSocket.
    Streams chunks immediately as they're decoded for low latency.
    """
    import edge_tts
    
    emotion = detect_emotion(text)
    profile = EMOTION_TTS_PROFILES.get(emotion, EMOTION_TTS_PROFILES["gentle"])
    text = prepare_tts_text(text, emotion)
    effective_voice = voice or profile.get("voice", TTS_VOICE)
    
    rate_str = profile.get("rate", "+0%")
    if speed_ratio is not None:
        rate_str = f"{int((speed_ratio - 1) * 100):+d}%"
    pitch_str = profile.get("pitch", "+0Hz")
    
    communicate = edge_tts.Communicate(text, effective_voice, rate=rate_str, pitch=pitch_str)
    
    mp3_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_data += chunk["data"]
    
    if not mp3_data:
        logger.warning("TTS produced no audio data")
        return
    
    # Decode MP3 to PCM 24kHz 16-bit mono via ffmpeg
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", "24000", "-ac", "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pcm_data, stderr = await proc.communicate(input=mp3_data)
        if proc.returncode != 0:
            logger.error(f"ffmpeg decode failed: {stderr.decode(errors='replace')[:200]}")
            return
    except FileNotFoundError:
        logger.error("ffmpeg not found, cannot stream TTS audio")
        return
    
    # Signal audio start
    await websocket.send_json({"type": "audio_start", "sample_rate": 24000, "channels": 1})
    
    # Stream PCM chunks (20ms = 960 bytes at 24000Hz 16-bit mono)
    chunk_size = 960
    offset = 0
    while offset < len(pcm_data):
        end = min(offset + chunk_size, len(pcm_data))
        await websocket.send_bytes(pcm_data[offset:end])
        offset = end
        await asyncio.sleep(0.004)
    
    await websocket.send_json({"type": "audio_end"})



# ── Full-Duplex Voice WebSocket ──────────────────────────────────────────

@app.websocket("/v1/ws/voice")
async def ws_voice_realtime(websocket: WebSocket):
    """Full-duplex real-time voice conversation WebSocket.
    
    Protocol:
      Client -> Server:
        Binary: raw PCM16 16kHz mono audio chunks (~50ms each)
        JSON:   {"type": "config", "voice": "...", "persona": "...", "speed": 1.0}
                {"type": "ping"}
                {"type": "barge_in"}  -- user started speaking during AI reply
      
      Server -> Client:
        JSON:   {"type": "pong"}
                {"type": "asr_partial", "text": "..."}  -- partial ASR result
                {"type": "asr_final", "text": "..."}    -- final ASR, user utterance done
                {"type": "token", "content": "..."}     -- LLM stream token
                {"type": "audio_start", "sample_rate": 24000, "channels": 1}
        Binary: PCM16 24kHz mono audio chunks (50ms each)
        JSON:   {"type": "audio_end"}
                {"type": "turn_done", "conversation_id": "..."}
                {"type": "error", "message": "..."}
                {"type": "interrupted"}  -- AI speech interrupted by barge-in
    
    Flow:
      1. Client streams PCM16 16kHz chunks as binary frames
      2. Server performs VAD (energy-based silence detection)
      3. When user utterance complete, send asr_final, call LLM + TTS
      4. Server streams TTS audio chunks back as binary frames
      5. If client sends barge_in during playback, server stops TTS immediately
      6. Client microphone is ALWAYS open; barge-in is detected on client side
    """
    await websocket.accept()
    if webrtcvad is None:
        await websocket.send_json({
            "type": "error",
            "message": "webrtcvad dependency is not installed. Install backend/requirements.txt.",
        })
        await websocket.close(code=1011)
        return
    
    conv_id = str(uuid.uuid4())
    persona = DEFAULT_PERSONA
    voice = None
    speed = None
    
    vad = webrtcvad.Vad(mode=int(os.environ.get("VOICEMATE_WEBRTC_VAD_MODE", "3")))
    turn_detector = SpeechTurnDetector(vad)
    semantic_gate = SemanticTurnGate()
    
    # AI speaking state
    ai_speaking = False
    ai_speak_task = None
    
    # Barge-in guard for explicit client-side barge_in messages.
    barge_in_speech_frames = 0
    barge_in_silence_frames = 0
    barge_in_cooldown = 0
    
    # ASR service placeholder (uses external API; for now we simulate with a simple approach)
    # In production, replace with Deepgram / Azure / Aliyun real-time ASR
    
    logger.info(f"Full-duplex voice call started: {conv_id}, persona={persona}, voice={voice}")
    
    async def handle_barge_in():
        """Handle user interruption during AI speech.
        Uses a confirmation window to avoid false barge-in from AI echo or noise.
        """
        nonlocal ai_speaking, ai_speak_task
        if ai_speaking:
            ai_speaking = False
            if ai_speak_task and not ai_speak_task.done():
                ai_speak_task.cancel()
                try:
                    await ai_speak_task
                except asyncio.CancelledError:
                    pass
            ai_speak_task = None
            try:
                await websocket.send_json({"type": "interrupted"})
            except Exception:
                pass
            logger.info(f"AI speech interrupted by barge-in [{conv_id}]")

    async def handle_user_text(text: str):
        logger.info(f"User text [{conv_id}]: {text[:80]}")
        await ai_speak(text)
    
    async def handle_barge_in_attempt():
        """Check if barge-in should proceed, using confirmation window.
        Returns True if barge-in actually happened, False if not yet confirmed.
        """
        nonlocal ai_speaking, barge_in_speech_frames, barge_in_silence_frames, barge_in_cooldown
        if not ai_speaking:
            return False
        # During cooldown after rejected barge-in, don't even start counting
        if barge_in_cooldown > 0:
            return False
        barge_in_speech_frames += 1
        barge_in_silence_frames = 0
        if barge_in_speech_frames >= BARGE_IN_CONFIRM_FRAMES:
            # Speech sustained long enough — real barge-in
            barge_in_speech_frames = 0
            barge_in_cooldown = 0
            await handle_barge_in()
            return True
        return False
    
    async def process_utterance(turn: dict):
        """Process a complete user utterance: ASR -> LLM -> TTS stream."""
        nonlocal ai_speaking, ai_speak_task, barge_in_speech_frames, barge_in_silence_frames, barge_in_cooldown
        
        speech_ms = float(turn.get("speech_ms", 0.0))
        if speech_ms < MIN_UTTERANCE_MS:
            logger.info(f"Utterance too short: speech={speech_ms:.0f}ms < {MIN_UTTERANCE_MS}ms minimum [{conv_id}]")
            return  # too short, ignore
        
        # Send asr_final (client-side ASR will provide the text separately)
        # For now, we just signal that we detected an utterance
        logger.info(
            "Sending asr_final to iOS [%s] speech=%.0fms silence=%.0fms total=%.0fms",
            conv_id,
            speech_ms,
            float(turn.get("silence_ms", 0.0)),
            float(turn.get("total_ms", 0.0)),
        )
        await websocket.send_json({
            "type": "asr_final",
            "text": "__vad_detected__",
            "conversation_id": conv_id,
        })
        
        # Note: In full production, send audio to server-side ASR (Deepgram/Azure).
        # For now we rely on client-side ASR sending a "text" message.
        # The VAD here is used to trigger the flow; actual transcribed text comes from client.
        # See the "text" command handler below.
    
    def _is_filler_text(text: str) -> bool:
        """Check if text is just filler/thinking noise (e.g., "嗯", "um", "啊")."""
        stripped = text.strip().rstrip(".。!！?？,，…").lower()
        return stripped in FILLER_WORDS

    def _is_filler_text(text: str) -> bool:
        compact = re.sub(r"[\s，。！？、,.!?~～…]+", "", (text or "").strip().lower())
        filler = set(FILLER_WORDS) | {"嗯", "啊", "哦", "额", "呃", "哎", "喂", "um", "uh", "ah", "er", "hmm"}
        if compact in filler or len(compact) < int(os.environ.get("VOICEMATE_ASR_MIN_TEXT_CHARS", "2")):
            return True
        if os.environ.get("VOICEMATE_ASR_REQUIRE_WAKE_WORD", "0").lower() in {"1", "true", "yes"}:
            wake_words = [
                w.strip()
                for w in os.environ.get("VOICEMATE_ASR_WAKE_WORDS", "小妤,妤妤,VoiceMate").split(",")
                if w.strip()
            ]
            return not any(re.sub(r"[\s，。！？、,.!?~～…]+", "", w.lower()) in compact for w in wake_words)
        return False

    async def ai_speak(ai_text: str):
        """Run LLM stream + TTS stream for AI response."""
        nonlocal ai_speaking, ai_speak_task, barge_in_speech_frames, barge_in_silence_frames, barge_in_cooldown

        # Skip filler/thinking noises (e.g., "嗯", "um", "啊") - do not call LLM
        if _is_filler_text(ai_text):
            logger.info(f"Ignoring filler text: {ai_text!r} [{conv_id}]")
            try:
                await websocket.send_json({"type": "turn_skipped", "conversation_id": conv_id, "reason": "filler"})
            except Exception:
                pass
            return

        # Guard: if already speaking, cancel previous task first
        if ai_speaking:
            await handle_barge_in()
            await asyncio.sleep(0.1)
        
        async def _speak_task():
            nonlocal ai_speaking, barge_in_speech_frames, barge_in_silence_frames, barge_in_cooldown
            # Reset VAD state and barge-in confirmation state when AI starts a new turn
            barge_in_speech_frames = 0
            barge_in_silence_frames = 0
            barge_in_cooldown = 0
            try:
                conv_history = history.load(conv_id)
                full_reply = ""
                
                # Stream LLM tokens
                logger.info(f"Starting DeepSeek stream for [{conv_id}]")
                async for token in deepseek.stream_chat(ai_text, conv_history, persona=persona):
                    if not ai_speaking:
                        logger.info(f"DeepSeek stream interrupted [{conv_id}]")
                        return  # interrupted
                    full_reply += token
                    await websocket.send_json({"type": "token", "content": token})
                
                if not ai_speaking:
                    return
                
                logger.info(f"DeepSeek reply complete ({len(full_reply)} chars) [{conv_id}]")
                
                if not full_reply:
                    full_reply = "我听到了呢～"
                
                clean_reply = strip_markdown(full_reply)
                clean_reply = naturalize_text(clean_reply)
                
                history.append(conv_id, ai_text, clean_reply)
                
                if not ai_speaking:
                    return
                
                # Stream TTS audio
                logger.info(f"Starting TTS stream for [{conv_id}]")
                await _stream_tts_to_websocket(websocket, clean_reply, voice=voice, speed_ratio=speed)
                logger.info(f"TTS stream complete [{conv_id}]")
                
                if ai_speaking:
                    await websocket.send_json({
                        "type": "turn_done",
                        "conversation_id": conv_id,
                    })
                    logger.info(f"Turn done sent [{conv_id}]")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"AI speak task error: {e}")
                try:
                    await websocket.send_json({"type": "error", "message": "AI回复失败"})
                except Exception:
                    pass
            finally:
                ai_speaking = False
                ai_speak_task = None
                turn_detector.reset_after_ai_turn()
        
        ai_speaking = True
        ai_speak_task = asyncio.create_task(_speak_task())
    
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=120.0)
            except asyncio.TimeoutError:
                logger.info(f"Voice call timeout: {conv_id}")
                try:
                    await websocket.send_json({"type": "timeout"})
                except Exception:
                    pass
                break
            
            # ── Extract raw data from ASGI Message dict ──
            # FastAPI/Starlette receive() returns {'type': 'websocket.receive', 'bytes': ..., 'text': ...}
            if isinstance(message, dict):
                msg_bytes = message.get("bytes", None)
                msg_text = message.get("text", None)
            elif isinstance(message, bytes):
                msg_bytes = message
                msg_text = None
            elif isinstance(message, str):
                msg_bytes = None
                msg_text = message
            else:
                msg_bytes = None
                msg_text = None
            
            # ── Binary: raw PCM16 audio from iOS mic ──
            if msg_bytes is not None:
                for turn in turn_detector.accept(msg_bytes, ai_speaking=ai_speaking):
                    logger.info(
                        "VAD turn complete [%s] speech=%.0fms silence=%.0fms total=%.0fms noise=%.1f",
                        conv_id,
                        turn["speech_ms"],
                        turn["silence_ms"],
                        turn["total_ms"],
                        turn["noise_floor"],
                    )
                    if ai_speaking:
                        if turn["speech_ms"] >= BARGE_IN_MIN_UTTERANCE_MS:
                            logger.info(
                                "VAD barge-in confirmed [%s] speech=%.0fms",
                                conv_id,
                                turn["speech_ms"],
                            )
                            await handle_barge_in()
                        else:
                            logger.info(
                                "VAD barge-in rejected [%s] speech=%.0fms",
                                conv_id,
                                turn["speech_ms"],
                            )
                            continue

                    await process_utterance(turn)

                continue
            
            # ── Text: JSON messages ──
            if msg_text is None:
                continue
            try:
                data = json.loads(msg_text)
            except (json.JSONDecodeError, TypeError):
                continue
            
            command = data.get("type", "")
            
            if command == "ping":
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    pass
                continue
            
            if command == "config":
                # Update conversation parameters
                if "voice" in data:
                    voice = data["voice"]
                if "persona" in data:
                    persona = data["persona"]
                if "speed" in data:
                    speed = data["speed"]
                if "conversation_id" in data and data["conversation_id"]:
                    conv_id = data["conversation_id"]
                logger.info(f"Config updated [{conv_id}]: persona={persona}, voice={voice}")
                continue
            
            if command == "barge_in":
                # Client detected user speech during AI playback.
                # Server-side guard: only barge-in if AI is actually speaking
                # (the client may send barge_in from echo/noise)
                if ai_speaking:
                    logger.info(f"Client barge_in received while AI speaking, using confirmation window [{conv_id}]")
                    await handle_barge_in_attempt()
                else:
                    logger.info(f"Client barge_in received but AI not speaking, ignoring [{conv_id}]")
                continue
            
            if command == "text":
                # Client-side ASR transcribed text (may arrive alongside VAD)
                text = data.get("text", "").strip()
                if not text:
                    logger.warning(f"Empty text received in text command [{conv_id}]")
                    continue
                
                # Skip filler/thinking noises at the text handler level too
                if _is_filler_text(text):
                    logger.info(f"Ignoring filler text at text handler: {text!r} [{conv_id}]")
                    try:
                        await websocket.send_json({"type": "turn_skipped", "conversation_id": conv_id, "reason": "filler"})
                    except Exception:
                        pass
                    continue
                
                if "voice" in data:
                    voice = data["voice"]
                if "persona" in data:
                    persona = data["persona"]
                if "speed" in data:
                    speed = data["speed"]
                if "conversation_id" in data and data["conversation_id"]:
                    conv_id = data["conversation_id"]
                
                await semantic_gate.submit(text, handle_user_text)
                continue
    
    except WebSocketDisconnect:
        logger.info(f"Voice call disconnected: {conv_id}")
    except Exception as e:
        logger.error(f"Voice call error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        # Cleanup
        semantic_gate.cancel()
        if ai_speak_task and not ai_speak_task.done():
            ai_speak_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass



# ── LiveKit Config ──────────────────────────────────────────────────────────────

LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")
LIVEKIT_HOST = os.environ.get("LIVEKIT_HOST", "192.168.10.233")
LIVEKIT_PORT = int(os.environ.get("LIVEKIT_PORT", "7880"))

ROOM_NAME = "voicemate"


class LiveKitTokenRequest(BaseModel):
    voice: Optional[str] = None
    persona: Optional[str] = None
    speed: Optional[float] = None
    room: Optional[str] = None


# ── LiveKit Token Endpoint ──────────────────────────────────────────────────────

@app.post("/v1/livekit/token")
async def create_livekit_token(request: Optional[LiveKitTokenRequest] = None):
    """
    Generate a LiveKit access token for joining the VoiceMate room.
    """
    try:
        if AccessToken is None or VideoGrants is None:
            raise RuntimeError("livekit-api dependency is not installed. Install backend/requirements.txt.")
        logger.info(f"Generating LiveKit token: key={LIVEKIT_API_KEY[:10]}... secret={LIVEKIT_API_SECRET[:10]}...")
        identity = f"voicemate-{uuid.uuid4().hex[:12]}"
        room_name = request.room if request and request.room else f"{ROOM_NAME}-{uuid.uuid4().hex[:8]}"

        token = AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        token.identity = identity
        token.ttl = timedelta(hours=2)
        token.with_grants(VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        ))
        jwt = token.to_jwt()
        logger.info(f"LiveKit token generated successfully")

        return {
            "token": jwt,
            "room": room_name,
            "url": f"ws://{LIVEKIT_HOST}:{LIVEKIT_PORT}",
            "voice": request.voice if request else None,
            "persona": request.persona if request else None,
            "speed": request.speed if request else None,
        }
    except Exception as e:
        logger.exception(f"LiveKit token generation failed: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))

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
        logger.warning("DEEPSEEK_API_KEY not set. /v1/health and /v1/livekit/token can still run, but chat replies will use the error fallback.")

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
