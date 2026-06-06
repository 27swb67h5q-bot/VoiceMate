#!/usr/bin/env python3
"""
VoiceMate LiveKit Agent

Connects to a local LiveKit server, receives user voice in real-time,
processes with DeepSeek LLM, and responds with Volcengine voice synthesis.

Architecture:
  User mic → LiveKit Room → (VAD) → Volcengine streaming ASR → DeepSeek LLM → Volcengine TTS → LiveKit Room → User speaker

Supports:
  - Real-time voice via LiveKit Agent pipeline
  - Built-in VAD + endpointing
  - DeepSeek LLM for response generation
  - Volcengine TTS for voice synthesis (reuses server.py approach)
  - Barge-in (user interruption during AI speech)
  - Persona system prompts from existing VoiceMate project

Usage:
  # Install deps (already in venv):
  #   pip install livekit livekit-agents openai webrtcvad volcengine-audio

  # Set env vars:
  #   export LIVEKIT_URL=ws://localhost:7880
  #   export LIVEKIT_API_KEY=your_api_key
  #   export LIVEKIT_API_SECRET=your_api_secret
  #   export DEEPSEEK_API_KEY=sk-...

  # Run:
  #   python livekit_agent.py
"""

import os
import sys
import json
import asyncio
import logging
import uuid
import time
import struct
import math
import re
import array
from datetime import datetime
from pathlib import Path
from typing import Optional, AsyncIterator, AsyncIterable, AsyncGenerator

from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault(
    "HF_ENDPOINT",
    os.environ.get("VOICEMATE_HF_ENDPOINT", "https://huggingface.co"),
)

# ── LiveKit imports ──────────────────────────────────────────────────────────
from livekit import rtc
from livekit.agents.utils import AudioBuffer

from livekit.agents import (
    AgentSession, AutoSubscribe, JobContext, WorkerOptions, cli,
    llm, stt, tts, vad, metrics,
)
from livekit.agents.voice import Agent, RunContext
from livekit.plugins import silero

# ── Add project root for imports ─────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from server import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    TTS_VOICE,
    PERSONAS,
    DEFAULT_PERSONA,
    FILLER_WORDS,
    strip_markdown,
    naturalize_text,
    detect_emotion,
    prepare_tts_text,
    EMOTION_TTS_PROFILES,
    is_semantically_incomplete,
    AUDIO_DIR,
    VolcengineTTSClient,
    companion_orchestrator,
    history_store,
    logger as voicemate_logger,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("voicemate-livekit")
try:
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "agent-runtime.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(file_handler)
except Exception:
    pass

# ── Config ───────────────────────────────────────────────────────────────────

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
LIVEKIT_AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "VoiceMate")
LLM_MAX_TOKENS = int(os.environ.get("VOICEMATE_LLM_MAX_TOKENS", "140"))
REALTIME_TTS_PROVIDER = "volcengine"
ASR_PROVIDER = "volcengine"
VOLCENGINE_ASR_WS_URL = os.environ.get(
    "VOLCENGINE_ASR_WS_URL",
    "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
)
VOLCENGINE_ASR_APP_KEY = os.environ.get("VOLCENGINE_ASR_APP_KEY", os.environ.get("VOLC_APPID", ""))
VOLCENGINE_ASR_ACCESS_KEY = os.environ.get("VOLCENGINE_ASR_ACCESS_KEY", os.environ.get("VOLC_TOKEN", ""))
VOLCENGINE_ASR_RESOURCE_ID = os.environ.get("VOLCENGINE_ASR_RESOURCE_ID", "volc.bigasr.sauc.duration")

# Noise/side-speech rejection. Tune these from .env if the room is very quiet/loud.
ASR_MIN_AUDIO_SECONDS = float(os.environ.get("VOICEMATE_ASR_MIN_AUDIO_SECONDS", "0.75"))
ASR_MIN_RMS = float(os.environ.get("VOICEMATE_ASR_MIN_RMS", "180"))
ASR_MAX_NO_SPEECH_PROB = float(os.environ.get("VOICEMATE_ASR_MAX_NO_SPEECH_PROB", "0.65"))
ASR_MIN_TEXT_CHARS = int(os.environ.get("VOICEMATE_ASR_MIN_TEXT_CHARS", "2"))
ASR_REQUIRE_WAKE_WORD = os.environ.get("VOICEMATE_ASR_REQUIRE_WAKE_WORD", "0").lower() in {"1", "true", "yes"}
ASR_WAKE_WORDS = tuple(
    w.strip()
    for w in os.environ.get("VOICEMATE_ASR_WAKE_WORDS", "小妤,妤妤,VoiceMate").split(",")
    if w.strip()
)
SPEAKER_LOCK_ENABLED = os.environ.get("VOICEMATE_SPEAKER_LOCK", "0").lower() in {"1", "true", "yes", "auto"}
SPEAKER_LOCK_MIN_SECONDS = float(os.environ.get("VOICEMATE_SPEAKER_LOCK_MIN_SECONDS", "1.2"))
SPEAKER_LOCK_MIN_RMS = float(os.environ.get("VOICEMATE_SPEAKER_LOCK_MIN_RMS", "260"))
SPEAKER_LOCK_THRESHOLD = float(os.environ.get("VOICEMATE_SPEAKER_LOCK_THRESHOLD", "0.58"))
SPEAKER_LOCK_LEARN_RATE = float(os.environ.get("VOICEMATE_SPEAKER_LOCK_LEARN_RATE", "0.18"))


def _normalize_asr_text(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~～…]+", "", text or "").lower()


def _is_noise_text(text: str) -> bool:
    normalized = _normalize_asr_text(text)
    if len(normalized) < ASR_MIN_TEXT_CHARS:
        return True
    filler = {_normalize_asr_text(w) for w in FILLER_WORDS}
    filler.update({"嗯", "啊", "哦", "额", "呃", "哎", "喂", "um", "uh", "ah", "er", "hmm"})
    if normalized in filler:
        return True
    if ASR_REQUIRE_WAKE_WORD:
        return not any(_normalize_asr_text(w) in normalized for w in ASR_WAKE_WORDS)
    return False


def _pcm_voice_features(pcm_data: bytes, sample_rate: int = 16000) -> tuple[float, ...]:
    """Small near-field speaker profile; not biometric, just rejects obvious off-mic speech/noise."""
    sample_count = len(pcm_data) // 2
    if sample_count <= 0:
        return (0.0, 0.0, 0.0, 0.0, 0.0)

    samples = struct.unpack_from(f"<{sample_count}h", pcm_data)
    rms = math.sqrt(sum(s * s for s in samples) / sample_count)
    abs_mean = sum(abs(s) for s in samples) / sample_count
    peak = max(abs(s) for s in samples) or 1
    zcr = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0 <= b) or (a >= 0 > b)) / max(sample_count - 1, 1)

    frame = max(int(sample_rate * 0.1), 1)
    frame_rms = []
    for i in range(0, sample_count - frame + 1, frame):
        chunk = samples[i:i + frame]
        frame_rms.append(math.sqrt(sum(s * s for s in chunk) / frame))
    if not frame_rms:
        frame_rms = [rms]

    voiced_ratio = sum(1 for v in frame_rms if v >= max(ASR_MIN_RMS, rms * 0.45)) / len(frame_rms)
    energy_var = math.sqrt(sum((v - rms) ** 2 for v in frame_rms) / len(frame_rms)) / max(rms, 1.0)
    crest = peak / max(abs_mean, 1.0)
    return (rms, zcr, voiced_ratio, energy_var, crest)


def _speaker_similarity(profile: tuple[float, ...], features: tuple[float, ...]) -> float:
    if not profile or not features or profile[0] <= 0 or features[0] <= 0:
        return 0.0
    rms_ratio = min(profile[0], features[0]) / max(profile[0], features[0])
    zcr_score = max(0.0, 1.0 - abs(profile[1] - features[1]) / 0.18)
    voiced_score = max(0.0, 1.0 - abs(profile[2] - features[2]) / 0.45)
    energy_score = max(0.0, 1.0 - abs(profile[3] - features[3]) / 1.2)
    crest_score = max(0.0, 1.0 - abs(profile[4] - features[4]) / 7.0)
    return (
        rms_ratio * 0.35
        + zcr_score * 0.20
        + voiced_score * 0.20
        + energy_score * 0.15
        + crest_score * 0.10
    )


class VolcengineStreamingASRClient:
    def __init__(self):
        self.ws_url = VOLCENGINE_ASR_WS_URL
        self.app_key = VOLCENGINE_ASR_APP_KEY
        self.access_key = VOLCENGINE_ASR_ACCESS_KEY
        self.resource_id = VOLCENGINE_ASR_RESOURCE_ID

    @property
    def is_available(self) -> bool:
        return bool(self.app_key and self.access_key and self.resource_id)

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-App-Key": self.app_key,
            "X-Api-Access-Key": self.access_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    @staticmethod
    async def _connect(websockets, url: str, headers: dict[str, str]):
        try:
            return await websockets.connect(url, additional_headers=headers, max_size=1000000000)
        except TypeError:
            return await websockets.connect(url, extra_headers=headers, max_size=1000000000)

    async def transcribe_pcm(self, pcm_data: bytes, sample_rate: int = 16000) -> tuple[str, float]:
        if not self.is_available:
            raise RuntimeError("Volcengine ASR credentials are not configured")

        import websockets
        from volcengine_audio import VolcengineAsrFunctionsV3, VolcengineAsrRequestV3

        started = time.perf_counter()
        request = VolcengineAsrRequestV3(
            user=VolcengineAsrRequestV3.User(uid="voicemate"),
            audio=VolcengineAsrRequestV3.Audio(format="pcm", codec="raw", rate=16000, bits=16, channel=1),
            request=VolcengineAsrRequestV3.Request(
                model_name="bigmodel",
                enable_punc=True,
                enable_itn=True,
                enable_ddc=True,
                enable_accelerate_text=True,
                accelerate_score=8,
                vad_segment_duration=int(os.environ.get("VOLCENGINE_ASR_VAD_SEGMENT_MS", "1200")),
                end_window_size=int(os.environ.get("VOLCENGINE_ASR_END_WINDOW_MS", "420")),
                force_to_speech_time=int(os.environ.get("VOLCENGINE_ASR_FORCE_SPEECH_MS", "6000")),
            ),
        ).model_dump(exclude_none=True)

        final_text = ""
        latest_text = ""
        sequence = 1
        frame_bytes = int(sample_rate * 0.10) * 2
        timeout = float(os.environ.get("VOLCENGINE_ASR_TIMEOUT_SECONDS", "18"))

        async with await self._connect(websockets, self.ws_url, self._headers()) as ws:
            await ws.send(VolcengineAsrFunctionsV3.generate_asr_full_client_request(sequence, request, compression=True))
            await asyncio.wait_for(ws.recv(), timeout=timeout)
            sequence += 1

            for offset in range(0, len(pcm_data), frame_bytes):
                chunk = pcm_data[offset: offset + frame_bytes]
                await ws.send(VolcengineAsrFunctionsV3.generate_asr_audio_only_request(sequence, chunk, compress=True))
                sequence += 1
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.02)
                    except asyncio.TimeoutError:
                        break
                    parsed = VolcengineAsrFunctionsV3.parse_response(raw)
                    latest_text = self._extract_text(parsed) or latest_text
                    if parsed.get("is_last_package"):
                        final_text = latest_text
                        break
                if final_text:
                    break

            if not final_text:
                await ws.send(VolcengineAsrFunctionsV3.generate_asr_audio_only_request(sequence, b"", compress=False))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    parsed = VolcengineAsrFunctionsV3.parse_response(raw)
                    latest_text = self._extract_text(parsed) or latest_text
                    if parsed.get("is_last_package"):
                        final_text = latest_text
                        break

        return (final_text or latest_text).strip(), (time.perf_counter() - started) * 1000

    @staticmethod
    def _extract_text(payload: dict) -> str:
        result = payload.get("payload_msg") or payload.get("payload") or payload
        if not isinstance(result, dict):
            return ""
        result_obj = result.get("result") or result.get("message", {}).get("result")
        if isinstance(result_obj, dict):
            text = result_obj.get("text")
            if isinstance(text, str):
                return text
            utterances = result_obj.get("utterances")
            if isinstance(utterances, list) and utterances:
                return "".join(u.get("text", "") for u in utterances if isinstance(u, dict))
        if isinstance(result_obj, list) and result_obj:
            return "".join(item.get("text", "") for item in result_obj if isinstance(item, dict))
        return ""


# Volcengine realtime STT

class VolcengineSTT(stt.STT):
    """Speech-to-Text using Volcengine streaming ASR only."""

    def __init__(self):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
            )
        )
        self._speaker_profile: Optional[tuple[float, ...]] = None
        self._volc_asr = VolcengineStreamingASRClient()
        self.last_asr_latency_ms: Optional[float] = None

    def stream(self, *, language=None, conn_options=None) -> stt.RecognizeStream:
        from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

        if not self._volc_asr.is_available:
            raise RuntimeError("Volcengine ASR credentials are not configured")
        return VolcengineRecognizeStream(
            self,
            conn_options=conn_options or DEFAULT_API_CONNECT_OPTIONS,
            sample_rate=16000,
        )

    @staticmethod
    def _frame_to_mono_pcm(frame: rtc.AudioFrame) -> tuple[bytes, int, int]:
        raw = frame.data
        if hasattr(raw, "tobytes"):
            raw = raw.tobytes()
        elif isinstance(raw, memoryview):
            raw = bytes(raw)

        sample_rate = int(getattr(frame, "sample_rate", 0) or 16000)
        channels = max(int(getattr(frame, "num_channels", 0) or 1), 1)
        frame_width = channels * 2
        if not raw or len(raw) < 2:
            return b"", sample_rate, channels
        if len(raw) % frame_width:
            raw = raw[: len(raw) - (len(raw) % frame_width)]

        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()

        if channels == 1:
            mono = samples
        else:
            mono = array.array("h")
            for i in range(0, len(samples), channels):
                mono.append(int(sum(samples[i:i + channels]) / channels))

        if sys.byteorder != "little":
            mono.byteswap()
        return mono.tobytes(), sample_rate, channels

    def _audio_buffer_to_wav(self, buffer: AudioBuffer) -> tuple[bytes, float, float, tuple[float, ...], int, int, int]:
        """Convert LiveKit AudioBuffer to WAV bytes using the frame's real format."""
        import io
        import wave

        # AudioBuffer is list[AudioFrame] | AudioFrame
        if isinstance(buffer, rtc.AudioFrame):
            frames = [buffer]
        else:
            frames = list(buffer)

        pcm_chunks: list[bytes] = []
        target_sample_rate: Optional[int] = None
        max_channels = 1
        for frame in frames:
            mono_pcm, sample_rate, channels = self._frame_to_mono_pcm(frame)
            if not mono_pcm:
                continue
            if target_sample_rate is None:
                target_sample_rate = sample_rate
            elif sample_rate != target_sample_rate:
                try:
                    import audioop
                    mono_pcm, _ = audioop.ratecv(
                        mono_pcm,
                        2,
                        1,
                        sample_rate,
                        target_sample_rate,
                        None,
                    )
                except Exception:
                    logger.warning(
                        "STT received mixed sample rates (%s -> %s) and could not resample",
                        sample_rate,
                        target_sample_rate,
                    )
            max_channels = max(max_channels, channels)
            pcm_chunks.append(mono_pcm)

        sample_rate = target_sample_rate or 16000
        pcm_data = b"".join(pcm_chunks)
        sample_count = len(pcm_data) // 2
        if sample_count:
            samples = struct.unpack_from(f"<{sample_count}h", pcm_data)
            rms = math.sqrt(sum(s * s for s in samples) / sample_count)
        else:
            rms = 0.0
        duration_seconds = sample_count / sample_rate if sample_rate else 0.0

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        return (
            buf.getvalue(),
            duration_seconds,
            rms,
            _pcm_voice_features(pcm_data, sample_rate),
            sample_rate,
            max_channels,
            len(frames),
        )

    def _update_speaker_profile(self, voice_features: tuple[float, ...]) -> None:
        if self._speaker_profile is None:
            self._speaker_profile = voice_features
            logger.info("Speaker lock enrolled from first clear utterance")
            return
        alpha = max(0.0, min(SPEAKER_LOCK_LEARN_RATE, 1.0))
        self._speaker_profile = tuple(
            old * (1.0 - alpha) + new * alpha
            for old, new in zip(self._speaker_profile, voice_features)
        )

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: Optional[str] = None,
        conn_options: object = None,
    ) -> stt.SpeechEvent:
        """Transcribe audio buffer to text."""
        lang = language or "zh"

        try:
            wav_bytes, duration_seconds, rms, voice_features, sample_rate, channels, frame_count = self._audio_buffer_to_wav(buffer)
            logger.info(
                "STT buffer: frames=%d sr=%d channels<=%d duration=%.2fs rms=%.1f provider=volcengine",
                frame_count,
                sample_rate,
                channels,
                duration_seconds,
                rms,
            )
            if duration_seconds < ASR_MIN_AUDIO_SECONDS or rms < ASR_MIN_RMS:
                logger.info(
                    "STT rejected short/quiet audio: %.2fs rms=%.1f",
                    duration_seconds,
                    rms,
                )
                return stt.SpeechEvent(
                    type=stt.SpeechEventType.END_OF_SPEECH,
                    alternatives=[],
                )

            if SPEAKER_LOCK_ENABLED and self._speaker_profile is not None:
                similarity = _speaker_similarity(self._speaker_profile, voice_features)
                if similarity < SPEAKER_LOCK_THRESHOLD:
                    logger.info(
                        "STT rejected off-speaker audio: score=%.2f rms=%.1f",
                        similarity,
                        rms,
                    )
                    return stt.SpeechEvent(
                        type=stt.SpeechEventType.END_OF_SPEECH,
                        alternatives=[],
                    )

            if not self._volc_asr.is_available:
                raise RuntimeError("Volcengine ASR credentials are not configured")

            pcm_data = wav_bytes[44:] if wav_bytes.startswith(b"RIFF") else wav_bytes
            if sample_rate != 16000:
                import audioop
                pcm_data, _ = audioop.ratecv(pcm_data, 2, 1, sample_rate, 16000, None)
            text, latency_ms = await self._volc_asr.transcribe_pcm(pcm_data, sample_rate=16000)
            self.last_asr_latency_ms = latency_ms
            logger.info("Volcengine ASR: latency=%.0fms text=%s", latency_ms, text[:80])

            if not text or _is_noise_text(text):
                logger.info("STT: empty transcription")
                return stt.SpeechEvent(
                    type=stt.SpeechEventType.END_OF_SPEECH,
                    alternatives=[],
                )

            if SPEAKER_LOCK_ENABLED and duration_seconds >= SPEAKER_LOCK_MIN_SECONDS and rms >= SPEAKER_LOCK_MIN_RMS:
                self._update_speaker_profile(voice_features)

            logger.info(f"STT: {text[:80]}")
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[
                    stt.SpeechData(
                        language=lang,
                        text=text,
                        start_time=0.0,
                        end_time=0.0,
                        confidence=0.95,
                    )
                ],
            )

        except Exception as e:
            logger.error(f"STT recognition error: {e}")
            return stt.SpeechEvent(
                type=stt.SpeechEventType.END_OF_SPEECH,
                alternatives=[],
            )


# ── Custom LLM: DeepSeek ────────────────────────────────────────────────────

class VolcengineRecognizeStream(stt.RecognizeStream):
    def __init__(self, volcengine_stt: VolcengineSTT, *, conn_options, sample_rate: int = 16000):
        super().__init__(stt=volcengine_stt, conn_options=conn_options, sample_rate=sample_rate)
        self._owner = volcengine_stt

    async def _run(self) -> None:
        import websockets
        from volcengine_audio import VolcengineAsrFunctionsV3, VolcengineAsrRequestV3

        client = self._owner._volc_asr
        if not client.is_available:
            raise RuntimeError("Volcengine ASR credentials are not configured")

        request = VolcengineAsrRequestV3(
            user=VolcengineAsrRequestV3.User(uid="voicemate"),
            audio=VolcengineAsrRequestV3.Audio(format="pcm", codec="raw", rate=16000, bits=16, channel=1),
            request=VolcengineAsrRequestV3.Request(
                model_name="bigmodel",
                enable_punc=True,
                enable_itn=True,
                enable_ddc=True,
                enable_accelerate_text=True,
                accelerate_score=8,
                vad_segment_duration=int(os.environ.get("VOLCENGINE_ASR_VAD_SEGMENT_MS", "1200")),
                end_window_size=int(os.environ.get("VOLCENGINE_ASR_END_WINDOW_MS", "420")),
                force_to_speech_time=int(os.environ.get("VOLCENGINE_ASR_FORCE_SPEECH_MS", "6000")),
            ),
        ).model_dump(exclude_none=True)

        started = time.perf_counter()
        sequence = 1
        latest_text = ""
        final_sent = False
        start_sent = False
        flushed = False
        latest_text_at = 0.0
        flush_grace_ms = int(os.environ.get("VOLCENGINE_ASR_FLUSH_GRACE_MS", "420"))
        auto_final_ms = int(os.environ.get("VOLCENGINE_ASR_AUTO_FINAL_MS", "900"))
        ws = await client._connect(websockets, client.ws_url, client._headers())
        try:
            await ws.send(VolcengineAsrFunctionsV3.generate_asr_full_client_request(sequence, request, compression=True))
            sequence += 1

            def emit_start_once() -> None:
                nonlocal start_sent
                if start_sent:
                    return
                start_sent = True
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))

            def emit_final_from_latest() -> None:
                nonlocal final_sent
                if final_sent:
                    return
                if latest_text:
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                            alternatives=[
                                stt.SpeechData(
                                    language="zh",
                                    text=latest_text,
                                    confidence=0.85,
                                )
                            ],
                        )
                    )
                final_sent = True
                self._owner.last_asr_latency_ms = (time.perf_counter() - started) * 1000
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))

            async def recv_loop():
                nonlocal latest_text, final_sent, latest_text_at
                try:
                    async for raw in ws:
                        parsed = VolcengineAsrFunctionsV3.parse_response(raw)
                        text = client._extract_text(parsed)
                        if text and text != latest_text:
                            emit_start_once()
                            latest_text = text
                            latest_text_at = time.perf_counter()
                            event_type = (
                                stt.SpeechEventType.FINAL_TRANSCRIPT
                                if parsed.get("is_last_package")
                                else stt.SpeechEventType.INTERIM_TRANSCRIPT
                            )
                            self._event_ch.send_nowait(
                                stt.SpeechEvent(
                                    type=event_type,
                                    alternatives=[
                                        stt.SpeechData(
                                            language="zh",
                                            text=text,
                                            confidence=0.9,
                                        )
                                    ],
                                )
                            )
                            if flushed and not final_sent:
                                emit_final_from_latest()
                                break
                        if parsed.get("is_last_package"):
                            emit_final_from_latest()
                            break
                except websockets.exceptions.ConnectionClosed:
                    emit_final_from_latest()

            async def stable_text_watchdog():
                while not final_sent:
                    await asyncio.sleep(0.1)
                    if (
                        latest_text
                        and latest_text_at > 0
                        and (time.perf_counter() - latest_text_at) * 1000 >= auto_final_ms
                    ):
                        emit_final_from_latest()
                        break

            recv_task = asyncio.create_task(recv_loop())
            watchdog_task = asyncio.create_task(stable_text_watchdog())
            async for item in self._input_ch:
                if final_sent:
                    break
                if isinstance(item, stt.RecognizeStream._FlushSentinel):
                    flushed = True
                    await ws.send(VolcengineAsrFunctionsV3.generate_asr_audio_only_request(sequence, b"", compress=False))
                    sequence += 1
                    if latest_text:
                        await asyncio.sleep(max(0, flush_grace_ms) / 1000)
                        emit_final_from_latest()
                        break
                    continue

                mono_pcm, sample_rate, _channels = VolcengineSTT._frame_to_mono_pcm(item)
                if not mono_pcm:
                    continue
                if sample_rate != 16000:
                    import audioop
                    mono_pcm, _ = audioop.ratecv(mono_pcm, 2, 1, sample_rate, 16000, None)
                await ws.send(VolcengineAsrFunctionsV3.generate_asr_audio_only_request(sequence, mono_pcm, compress=True))
                sequence += 1

            if not final_sent:
                await ws.send(VolcengineAsrFunctionsV3.generate_asr_audio_only_request(sequence, b"", compress=False))
            if final_sent:
                recv_task.cancel()
                watchdog_task.cancel()
                try:
                    await recv_task
                except asyncio.CancelledError:
                    pass
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            else:
                await recv_task
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            emit_final_from_latest()
        finally:
            await ws.close()


class DeepSeekLLM(llm.LLM):
    """LiveKit LLM adapter for DeepSeek API via OpenAI-compatible endpoint."""

    def __init__(self):
        super().__init__()
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
        )
        self._model = DEEPSEEK_MODEL

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "deepseek"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        **kwargs,
    ) -> llm.LLMStream:
        return DeepSeekLLMStream(self, chat_ctx, **kwargs)


class DeepSeekLLMStream(llm.LLMStream):
    """Streaming wrapper for DeepSeek API responses."""

    def __init__(self, deepseek_llm: DeepSeekLLM, chat_ctx: llm.ChatContext, **kwargs):
        from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
        tools = kwargs.get("tools") or []
        conn_options = kwargs.get("conn_options") or DEFAULT_API_CONNECT_OPTIONS
        super().__init__(
            deepseek_llm,
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
        )
        self._llm = deepseek_llm
        self._chat_ctx = chat_ctx

    async def _run(self) -> None:
        messages, _ = self._chat_ctx.to_provider_format(
            "openai", inject_dummy_user_message=False
        )
        last_user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_text = content
                break
        if is_semantically_incomplete(last_user_text):
            logger.info("Semantic turn gate: waiting for continuation: %s", last_user_text[:80])
            self._event_ch.send_nowait(llm.ChatChunk(
                id=str(uuid.uuid4()),
                delta=llm.ChoiceDelta(role="assistant", content="嗯，你继续说，我听着。"),
            ))
            return

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M %A")
        weekday_map = {
            "Monday": "星期一", "Tuesday": "星期二", "Wednesday": "星期三",
            "Thursday": "星期四", "Friday": "星期五", "Saturday": "星期六",
            "Sunday": "星期日",
        }
        cn_weekday = weekday_map.get(current_time.split()[-1], current_time.split()[-1])
        current_time_cn = current_time.rsplit(" ", 1)[0] + " " + cn_weekday

        enriched = []
        for msg in messages:
            enriched.append(msg)
            if msg.get("role") == "system":
                enriched.append({"role": "user", "content": f"现在是北京时间 {current_time_cn}"})
                enriched.append({"role": "assistant", "content": f"知道了，现在是 {current_time_cn}。"})

        if not any(m.get("role") == "system" for m in enriched):
            enriched.insert(0, {"role": "system", "content": PERSONAS.get(DEFAULT_PERSONA, PERSONAS["love"])})

        try:
            response = await self._llm._client.chat.completions.create(
                model=self._llm._model,
                messages=enriched,
                max_tokens=LLM_MAX_TOKENS,
                temperature=0.8,
                stream=True,
            )
            async for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    self._event_ch.send_nowait(llm.ChatChunk(
                        id=chunk.id or str(uuid.uuid4()),
                        delta=llm.ChoiceDelta(role="assistant", content=delta.content),
                    ))
        except Exception as e:
            logger.error("DeepSeek stream error: %s", e)
            self._event_ch.send_nowait(llm.ChatChunk(
                id=str(uuid.uuid4()),
                delta=llm.ChoiceDelta(role="assistant", content="嗯，我听到你了。不过我现在有点卡，能再说一遍吗？"),
            ))


class VolcengineLiveTTS(tts.TTS):
    """LiveKit TTS adapter for Volcengine V3 bidirectional TTS."""

    ACK_TEXTS = {
        "gentle": "嗯，我在。",
        "happy": "嘿，我听着。",
        "sad": "我在，慢慢说。",
        "angry": "我听见了，先别急。",
    }

    def __init__(self):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=24000,
            num_channels=1,
        )
        self._client = VolcengineTTSClient()
        self.emotion_hint = "gentle"
        self.speed_hint = 1.0
        self._ack_cache_dir = AUDIO_DIR / "realtime_ack_pcm"
        self._ack_cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def provider(self) -> str:
        return "volcengine"

    @property
    def is_available(self) -> bool:
        return self._client.is_available

    def synthesize(self, text: str, **kwargs) -> tts.ChunkedStream:
        return VolcengineLiveTTSChunkedStream(self, text)

    def stream(self, **kwargs) -> tts.SynthesizeStream:
        return VolcengineLiveTTSStream(self, conn_options=kwargs.get("conn_options"))

    def _ack_path(self, emotion: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", emotion or "gentle")
        return self._ack_cache_dir / f"{safe}.pcm"

    def cached_ack_pcm(self, emotion: str) -> bytes:
        path = self._ack_path(self._normalize_ack_emotion(emotion))
        try:
            return path.read_bytes() if path.exists() else b""
        except Exception:
            return b""

    def _normalize_ack_emotion(self, emotion: str) -> str:
        if emotion in {"happy", "sad", "angry"}:
            return emotion
        return "gentle"

    async def prewarm_ack_cache(self) -> None:
        if not self.is_available:
            return
        for emotion, text in self.ACK_TEXTS.items():
            path = self._ack_path(emotion)
            if path.exists() and path.stat().st_size > 0:
                continue
            try:
                path.write_bytes(
                    await self._client.synthesize_bytes(
                        text,
                        speed=0.98,
                        emotion=emotion,
                        audio_format="pcm",
                    )
                )
                logger.info("Prewarmed realtime TTS ack: %s", emotion)
            except Exception as e:
                logger.warning("Failed to prewarm realtime TTS ack %s: %s", emotion, e)


class VolcengineLiveTTSChunkedStream(tts.ChunkedStream):
    def __init__(self, volc_tts_obj: VolcengineLiveTTS, text: str):
        from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
        super().__init__(
            tts=volc_tts_obj,
            input_text=text,
            conn_options=DEFAULT_API_CONNECT_OPTIONS,
        )
        self._tts = volc_tts_obj
        self._text = text

    async def _run(self, emitter: tts.AudioEmitter) -> None:
        emotion = detect_emotion(self._text)
        if emotion in {"gentle", "neutral", "curious"}:
            emotion = self._tts.emotion_hint
        text = prepare_tts_text(strip_markdown(self._text), emotion)
        if not text:
            text = "嗯"

        pcm_data = await self._tts._client.synthesize_bytes(
            text,
            speed=self._tts.speed_hint,
            emotion=emotion,
            audio_format="pcm",
        )
        if not pcm_data:
            return

        emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
        )

        frame_size = 1920
        offset = 0
        while offset < len(pcm_data):
            end = min(offset + frame_size, len(pcm_data))
            chunk = pcm_data[offset:end]
            if len(chunk) < frame_size:
                chunk += b"\x00" * (frame_size - len(chunk))
            emitter.push(chunk)
            offset = end

        emitter.flush()


class VolcengineLiveTTSStream(tts.SynthesizeStream):
    def __init__(self, volc_tts_obj: VolcengineLiveTTS, conn_options=None):
        from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
        super().__init__(
            tts=volc_tts_obj,
            conn_options=conn_options or DEFAULT_API_CONNECT_OPTIONS,
        )
        self._tts = volc_tts_obj

    @staticmethod
    def _push_pcm(output_emitter: tts.AudioEmitter, pcm_data: bytes) -> None:
        frame_size = 1920
        for offset in range(0, len(pcm_data), frame_size):
            chunk = pcm_data[offset: offset + frame_size]
            if len(chunk) < frame_size:
                chunk += b"\x00" * (frame_size - len(chunk))
            output_emitter.push(chunk)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        request_id = str(uuid.uuid4())
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
            stream=True,
        )
        buffer = ""
        first_audio_ms: Optional[float] = None
        ack_sent = False
        started = time.perf_counter()
        ws = None
        min_segment_chars = int(os.environ.get("VOLCENGINE_TTS_MIN_SEGMENT_CHARS", "6"))
        max_segment_chars = int(os.environ.get("VOLCENGINE_TTS_MAX_SEGMENT_CHARS", "14"))

        def should_flush_segment(text: str) -> bool:
            stripped = text.strip()
            if len(stripped) < min_segment_chars:
                return False
            if re.search(r"[。！？!?；;，,、]\s*$", stripped):
                return True
            return len(stripped) >= max_segment_chars and not is_semantically_incomplete(stripped)

        async def get_ws():
            nonlocal ws
            if ws is None:
                ws = await self._tts._client.open_ws()
            return ws

        async def synthesize_segment(segment_text: str) -> None:
            nonlocal first_audio_ms, ack_sent
            segment_emotion = detect_emotion(segment_text)
            if segment_emotion in {"gentle", "neutral", "curious"}:
                segment_emotion = self._tts.emotion_hint
            text = prepare_tts_text(strip_markdown(segment_text), segment_emotion)
            if not text:
                return

            if not ack_sent and os.environ.get("VOICEMATE_REALTIME_ACK_ENABLED", "1").lower() not in {"0", "false", "no"}:
                ack_pcm = self._tts.cached_ack_pcm(segment_emotion)
                if ack_pcm:
                    ack_sent = True
                    if first_audio_ms is None:
                        first_audio_ms = (time.perf_counter() - started) * 1000
                        logger.info("TTS cached ack latency=%.0fms emotion=%s", first_audio_ms, segment_emotion)
                    output_emitter.start_segment(segment_id=str(uuid.uuid4()))
                    self._push_pcm(output_emitter, ack_pcm)
                    output_emitter.end_segment()

            segment_id = str(uuid.uuid4())
            pcm_data = await self._tts._client.synthesize_bytes_on_ws(
                await get_ws(),
                text,
                speed=self._tts.speed_hint,
                emotion=segment_emotion,
                audio_format="pcm",
            )
            if first_audio_ms is None:
                first_audio_ms = (time.perf_counter() - started) * 1000
                logger.info("TTS first audio latency=%.0fms chars=%d", first_audio_ms, len(text))
            output_emitter.start_segment(segment_id=segment_id)
            self._push_pcm(output_emitter, pcm_data)
            output_emitter.end_segment()

        try:
            from volcengine_audio import VolcengineTTSFunctions

            async for item in self._input_ch:
                if isinstance(item, str):
                    buffer += item
                    while should_flush_segment(buffer):
                        stripped = buffer.strip()
                        if re.search(r"[。！？!?；;，,、]\s*$", stripped):
                            segment = buffer
                            buffer = ""
                        else:
                            segment = buffer[:max_segment_chars]
                            buffer = buffer[max_segment_chars:]
                        await synthesize_segment(segment)
                        if not buffer.strip():
                            buffer = ""
                            break
                else:
                    if buffer.strip():
                        await synthesize_segment(buffer)
                        buffer = ""
                    output_emitter.flush()

            if buffer.strip():
                await synthesize_segment(buffer)
            if ws is not None:
                await ws.send(VolcengineTTSFunctions.finish_connection_payload())
            output_emitter.flush()
        finally:
            if ws is not None:
                await ws.close()


class SilentLiveTTS(tts.TTS):
    """Safety output when Volcengine TTS credentials are unavailable."""

    def __init__(self):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )

    @property
    def provider(self) -> str:
        return "silent"

    def synthesize(self, text: str, **kwargs) -> tts.ChunkedStream:
        return SilentLiveTTSChunkedStream(self, text)


class SilentLiveTTSChunkedStream(tts.ChunkedStream):
    def __init__(self, silent_tts_obj: SilentLiveTTS, text: str):
        from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
        super().__init__(
            tts=silent_tts_obj,
            input_text=text,
            conn_options=DEFAULT_API_CONNECT_OPTIONS,
        )

    async def _run(self, emitter: tts.AudioEmitter) -> None:
        emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
        )
        emitter.push(b"\x00" * 1920)
        emitter.flush()


class VoiceMateAgent(Agent):
    """VoiceMate Voice AI Agent for LiveKit.

    Pipeline: VAD → Volcengine streaming ASR → DeepSeekLLM → Volcengine TTS
    Supports barge-in natively through allow_interruptions=True.
    Persona configurable via room metadata.
    """

    def __init__(self, ctx: JobContext, **kwargs):
        room_metadata = {}
        try:
            if ctx.room.metadata:
                room_metadata = json.loads(ctx.room.metadata)
        except (json.JSONDecodeError, TypeError):
            pass

        self._persona = room_metadata.get("persona", DEFAULT_PERSONA)
        self._voice = room_metadata.get("voice", TTS_VOICE)
        self._conv_id = str(uuid.uuid4())[:8]
        self._ctx = ctx
        self._last_user_text = ""
        self._last_analysis = companion_orchestrator.analyze("")

        instructions = companion_orchestrator.system_prompt(
            self._persona,
            self._conv_id,
            mode="realtime",
        )

        self._volcengine_stt = VolcengineSTT()
        self._deepseek_llm = DeepSeekLLM()
        self._volc_tts = VolcengineLiveTTS()
        self._silent_tts = SilentLiveTTS()
        self._active_tts = self._volc_tts if self._volc_tts.is_available else self._silent_tts
        if not self._volc_tts.is_available:
            logger.error("Volcengine realtime TTS credentials are not configured; using silent safety output")

        logger.info(
            f"VoiceMateAgent init (persona={self._persona}, "
            f"voice={self._voice}, tts={self._active_tts.provider}) [{self._conv_id}]"
        )

        super().__init__(
            instructions=instructions,
            stt=self._volcengine_stt,
            vad=silero.VAD.load(
                min_speech_duration=float(os.environ.get("VOICEMATE_SILERO_MIN_SPEECH_SECONDS", "0.12")),
                min_silence_duration=float(os.environ.get("VOICEMATE_SILERO_MIN_SILENCE_SECONDS", "0.38")),
                prefix_padding_duration=float(os.environ.get("VOICEMATE_SILERO_PREFIX_PADDING_SECONDS", "0.24")),
                activation_threshold=float(os.environ.get("VOICEMATE_SILERO_ACTIVATION_THRESHOLD", "0.45")),
            ),
            llm=self._deepseek_llm,
            tts=self._active_tts,
            allow_interruptions=True,       # Barge-in
            turn_detection=os.environ.get("VOICEMATE_TURN_DETECTION", "stt").strip().lower(),
            min_endpointing_delay=float(os.environ.get("VOICEMATE_MIN_ENDPOINTING_DELAY", "0.35")),
            max_endpointing_delay=float(os.environ.get("VOICEMATE_MAX_ENDPOINTING_DELAY", "0.85")),
            min_consecutive_speech_delay=float(os.environ.get("VOICEMATE_MIN_CONSECUTIVE_SPEECH_DELAY", "0.25")),
            **kwargs,
        )

    async def on_enter(self):
        logger.info(f"VoiceMate agent entered room [{self._conv_id}]")
        await self._publish_call_event("call_state", state="listening")

    async def _publish_call_event(self, event_type: str, **payload):
        data = {"type": event_type, **payload}
        try:
            await self._ctx.room.local_participant.publish_data(
                json.dumps(data, ensure_ascii=False),
                reliable=True,
                topic="voicemate.transcript",
            )
        except Exception as e:
            logger.warning("Failed to publish call event %s: %s", event_type, e)

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage):
        user_text = ""
        if new_message and new_message.content:
            for c in new_message.content:
                if isinstance(c, str):
                    user_text += c
                elif hasattr(c, 'text') and c.text:
                    user_text += c.text

        logger.info(f"User turn: {user_text[:80]} [{self._conv_id}]")
        user_text = user_text.strip()
        if user_text:
            self._last_user_text = user_text
            self._last_analysis = companion_orchestrator.analyze(user_text)
            self._volc_tts.emotion_hint = self._last_analysis.tts_emotion
            self._volc_tts.speed_hint = self._last_analysis.tts_speed
            await self._publish_call_event("user_transcript", text=user_text, is_final=True)
            await self._publish_call_event(
                "emotion_state",
                emotion=self._last_analysis.emotion,
                label=self._last_analysis.status_label,
                intensity=self._last_analysis.intensity,
                need=self._last_analysis.need,
            )
            if self._volcengine_stt.last_asr_latency_ms is not None:
                await self._publish_call_event("metrics", label="ASR", value_ms=self._volcengine_stt.last_asr_latency_ms)
            await self._publish_call_event("call_state", state="thinking")

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings,
    ) -> AsyncGenerator[llm.ChatChunk, None]:
        if self._last_user_text:
            try:
                system_text = companion_orchestrator.system_prompt(
                    self._persona,
                    self._conv_id,
                    mode="realtime",
                    user_text=self._last_user_text,
                )
                chat_ctx.add_message(role="system", content=system_text)
            except Exception as e:
                logger.warning("Failed to inject orchestrator prompt: %s", e)

        tool_choice = getattr(model_settings, "tool_choice", None)
        stream = self._deepseek_llm.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            tool_choice=tool_choice,
        )
        full_text: list[str] = []
        llm_started = time.perf_counter()
        first_token_ms: Optional[float] = None
        async with stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - llm_started) * 1000
                        logger.info("LLM first token latency=%.0fms [%s]", first_token_ms, self._conv_id)
                        await self._publish_call_event("metrics", label="LLM", value_ms=first_token_ms)
                        await self._publish_call_event("call_state", state="speaking")
                    full_text.append(chunk.delta.content)
                yield chunk
        reply = "".join(full_text).strip()
        if reply:
            logger.info("LLM full response latency=%.0fms chars=%d [%s]", (time.perf_counter() - llm_started) * 1000, len(reply), self._conv_id)
            await self._publish_call_event("call_state", state="listening")
            if self._last_user_text:
                history_store.append(self._conv_id, self._last_user_text, reply)
                companion_orchestrator.record_turn(
                    conversation_id=self._conv_id,
                    user_text=self._last_user_text,
                    assistant_text=reply,
                )
            await self._publish_call_event("ai_turn_complete", text=reply)


# ── LiveKit Agent Entry Point ───────────────────────────────────────────────

async def entrypoint(ctx: JobContext):
    """LiveKit agent entry point - called when a job is assigned."""
    logger.info(f"Job assigned: {ctx.job.id}, room: {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    agent = VoiceMateAgent(ctx)
    session = AgentSession()

    # AgentSession handles subscribing to participant audio,
    # running VAD → STT → LLM → TTS pipeline with barge-in.
    await session.start(agent, room=ctx.room)
    if session.room_io.linked_participant:
        logger.info(
            "Linked participant for audio: %s",
            session.room_io.linked_participant.identity,
        )

    room_done = asyncio.Event()

    def _on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info("Participant left room: %s", participant.identity)
        if not ctx.room.remote_participants:
            room_done.set()

    def _on_room_disconnected(reason):
        logger.info("LiveKit room disconnected: %s", reason)
        room_done.set()

    ctx.room.on("participant_disconnected", _on_participant_disconnected)
    ctx.room.on("disconnected", _on_room_disconnected)
    try:
        if not ctx.room.remote_participants:
            logger.info("No remote participant yet; keeping agent alive for late media publish")
        await room_done.wait()
    finally:
        try:
            ctx.room.off("participant_disconnected", _on_participant_disconnected)
            ctx.room.off("disconnected", _on_room_disconnected)
        except Exception:
            pass
    logger.info(f"Room session ended for {ctx.room.name}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not DEEPSEEK_API_KEY:
        logger.warning(
            "DEEPSEEK_API_KEY not set. The LiveKit agent can start, "
            "but LLM calls will use a local error reply until a key is configured."
        )

    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        logger.warning("LiveKit credentials not fully configured.")

    logger.info("Starting VoiceMate LiveKit Agent")
    logger.info(f"  LiveKit: {LIVEKIT_URL}")
    logger.info(f"  DeepSeek: {DEEPSEEK_MODEL}")
    logger.info("  Chat TTS provider: volcengine")
    logger.info("  Realtime TTS provider: %s", REALTIME_TTS_PROVIDER)
    logger.info("  Volcengine voice: %s", os.environ.get("VOLCENGINE_TTS_VOICE_TYPE", "not configured"))
    logger.info("  ASR provider: %s", ASR_PROVIDER)
    logger.info("  Turn detection: %s", os.environ.get("VOICEMATE_TURN_DETECTION", "stt"))
    logger.info(f"  Persona: {DEFAULT_PERSONA}")
    logger.info(f"  Barge-in: enabled")
    if os.environ.get("VOICEMATE_PREWARM_TTS_ACKS", "1").lower() not in {"0", "false", "no"}:
        try:
            asyncio.run(VolcengineLiveTTS().prewarm_ack_cache())
        except Exception as e:
            logger.warning("Realtime TTS ack prewarm failed: %s", e)

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=LIVEKIT_AGENT_NAME,
            ws_url=LIVEKIT_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )
    )


if __name__ == "__main__":
    main()
