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
import uuid
import json
import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
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

# System prompt for the AI companion
SYSTEM_PROMPT = os.environ.get(
    "VOICEMATE_SYSTEM_PROMPT",
    "你是一个温暖的陪聊伙伴。用自然的口语回复，像是在跟好朋友聊天。"
    "回复要简短自然，适合语音播放。控制在100字以内。不要用Markdown格式。"
    "用中文回复。"
)

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
    conversation_id: Optional[str] = None  # For future multi-turn context


class ChatResponse(BaseModel):
    reply_text: str
    audio_url: str
    conversation_id: str
    duration_ms: int


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

    async def chat(self, text: str, conversation_id: Optional[str] = None) -> str:
        """Send a message to DeepSeek and get reply text."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]

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
            # Fallback response
            return "嗯，我听到你了。不过我现在有点卡顿，能再说一遍吗？"


# ── TTS Engine (edge-tts) ───────────────────────────────────────────────────

class TTSEngine:
    def __init__(self):
        self.voice = TTS_VOICE
        self.rate = TTS_RATE
        self.volume = TTS_VOLUME

    async def synthesize(self, text: str) -> tuple[str, int]:
        """Convert text to speech, return (audio_path, duration_ms)."""
        import edge_tts

        audio_id = str(uuid.uuid4())[:8]
        output_path = str(AUDIO_DIR / f"{audio_id}.mp3")

        communicate = edge_tts.Communicate(
            text,
            self.voice,
            rate=self.rate,
            volume=self.volume,
        )

        start = time.time()
        await communicate.save(output_path)
        elapsed = time.time() - start

        # Rough estimate: edge-tts generates ~50 chars/sec for Chinese
        duration_ms = max(int((len(text) / 5) * 1000), 1000)

        logger.info(f"TTS generated in {elapsed:.2f}s -> {output_path} ({duration_ms}ms)")
        return output_path, duration_ms


# ── Initialize Services ─────────────────────────────────────────────────────

deepseek = DeepSeekClient()
tts = TTSEngine()

# In-memory conversation store (simple for MVP, will persist later)
conversations: dict[str, list[dict]] = {}


# ── API Endpoints ───────────────────────────────────────────────────────────

@app.get("/v1/health")
async def health():
    return {"status": "ok", "service": "voicemate", "version": "1.0.0"}


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Main chat endpoint: receive text → AI reply → TTS → return audio."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    conv_id = request.conversation_id or str(uuid.uuid4())

    logger.info(f"Chat request [{conv_id}]: {request.text[:80]}")

    # 1. Get AI reply
    reply = await deepseek.chat(request.text, conv_id)

    # 2. Generate TTS audio
    audio_path, duration_ms = await tts.synthesize(reply)

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
    )


@app.get("/v1/audio/{audio_id}")
async def get_audio(audio_id: str):
    """Serve a generated audio file."""
    # Support both with and without extension
    audio_path = AUDIO_DIR / audio_id
    if not audio_path.exists():
        audio_path = AUDIO_DIR / f"{audio_id}.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")

    return FileResponse(
        str(audio_path),
        media_type="audio/mpeg",
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


# ── WebSocket for Real-Time Voice (Future) ──────────────────────────────────

# Placeholder for Phase 2: real-time voice conversation
# @app.websocket("/v1/ws/voice")
# async def voice_websocket(websocket: WebSocket):
#     await websocket.accept()
#     # Stream audio in, stream audio out
#     # Uses streaming ASR + streaming LLM + streaming TTS
#     ...


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
