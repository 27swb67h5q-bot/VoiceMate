#!/usr/bin/env python3
"""
VoiceMate LiveKit Agent

Connects to a local LiveKit server, receives user voice in real-time,
processes with DeepSeek LLM, and responds with edge-tts voice synthesis.

Architecture:
  User mic → LiveKit Room → (VAD) → STT (Whisper) → DeepSeek LLM → edge-tts TTS → LiveKit Room → User speaker

Supports:
  - Real-time voice via LiveKit Agent pipeline
  - Built-in VAD + endpointing
  - DeepSeek LLM for response generation
  - edge-tts for voice synthesis (reuses server.py approach)
  - Barge-in (user interruption during AI speech)
  - Persona system prompts from existing VoiceMate project

Usage:
  # Install deps (already in venv):
  #   pip install livekit livekit-agents edge-tts openai webrtcvad faster-whisper

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
from typing import Optional, AsyncIterator, AsyncIterable

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
    resolve_edge_voice,
    MiMoTTS,
    VOICEMATE_TTS_PROVIDER,
    is_semantically_incomplete,
    AUDIO_DIR,
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
REALTIME_TTS_PROVIDER = os.environ.get("VOICEMATE_REALTIME_TTS_PROVIDER", VOICEMATE_TTS_PROVIDER).strip().lower()
ASR_MODEL = (os.environ.get("VOICEMATE_ASR_MODEL", "base").strip() or "base")
ASR_DEVICE = (os.environ.get("VOICEMATE_ASR_DEVICE", "cpu").strip() or "cpu")
ASR_COMPUTE_TYPE = (os.environ.get("VOICEMATE_ASR_COMPUTE_TYPE", "int8").strip() or "int8")
ASR_API_KEY = os.environ.get("VOICE_TOOLS_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY", "")
ASR_API_BASE_URL = (
    os.environ.get("VOICE_TOOLS_OPENAI_BASE_URL")
    or os.environ.get("OPENAI_BASE_URL")
    or "https://api.openai.com/v1"
)

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


# ── Custom STT: Whisper ──────────────────────────────────────────────────────

class WhisperSTT(stt.STT):
    """Speech-to-Text using Whisper (local faster-whisper or API)."""

    def __init__(self):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        self._local_model_size = ASR_MODEL
        self._use_local_model = self._local_model_size.lower() not in {
            "api",
            "openai",
            "remote",
        }
        self._local_model = None
        self._client = None
        if not self._use_local_model:
            from openai import AsyncOpenAI
            if not ASR_API_KEY:
                logger.warning("Whisper API mode requested, but VOICE_TOOLS_OPENAI_KEY/OPENAI_API_KEY is not set.")
            self._client = AsyncOpenAI(
                api_key=ASR_API_KEY or "missing",
                base_url=ASR_API_BASE_URL,
            )
        self._speaker_profile: Optional[tuple[float, ...]] = None

    def _model_ref(self) -> str:
        if Path(self._local_model_size).exists():
            return self._local_model_size

        model_repo = os.environ.get("VOICEMATE_ASR_REPO", f"Systran/faster-whisper-{self._local_model_size}")
        model_dir = Path(os.environ.get(
            "VOICEMATE_ASR_MODEL_DIR",
            str(Path(__file__).parent / "models" / f"faster-whisper-{self._local_model_size}"),
        ))
        if (model_dir / "model.bin").exists():
            return str(model_dir)

        try:
            from huggingface_hub import snapshot_download
            endpoint = os.environ.get("HF_ENDPOINT") or os.environ.get("VOICEMATE_HF_ENDPOINT")
            logger.info("Downloading ASR model %s -> %s", model_repo, model_dir)
            snapshot_download(
                model_repo,
                local_dir=str(model_dir),
                endpoint=endpoint,
                allow_patterns=[
                    "config.json",
                    "model.bin",
                    "tokenizer.json",
                    "vocabulary.*",
                    "preprocessor_config.json",
                ],
                max_workers=4,
            )
            if (model_dir / "model.bin").exists():
                return str(model_dir)
        except Exception as e:
            logger.error("ASR model download failed: %s", e)

        return self._local_model_size

    async def _load_local_model(self):
        from faster_whisper import WhisperModel
        import concurrent.futures
        size = self._local_model_size or "base"
        device = ASR_DEVICE
        compute_type = ASR_COMPUTE_TYPE
        with concurrent.futures.ThreadPoolExecutor() as pool:
            try:
                self._local_model = await asyncio.get_event_loop().run_in_executor(
                    pool,
                    lambda: WhisperModel(self._model_ref(), device=device, compute_type=compute_type),
                )
                logger.info("Loaded faster-whisper model: %s (%s/%s)", size, device, compute_type)
            except Exception:
                if device.lower() == "cpu":
                    raise
                logger.warning(
                    "Failed to load faster-whisper on %s/%s; falling back to cpu/int8",
                    device,
                    compute_type,
                    exc_info=True,
                )
                self._local_model = await asyncio.get_event_loop().run_in_executor(
                    pool,
                    lambda: WhisperModel(self._model_ref(), device="cpu", compute_type="int8"),
                )
                logger.info("Loaded faster-whisper model: %s (cpu/int8 fallback)", size)

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
                "STT buffer: frames=%d sr=%d channels<=%d duration=%.2fs rms=%.1f model=%s",
                frame_count,
                sample_rate,
                channels,
                duration_seconds,
                rms,
                self._local_model_size if self._use_local_model else "api",
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

            if self._use_local_model and self._local_model is None:
                await self._load_local_model()

            if self._local_model:
                import concurrent.futures
                temp_path = str(AUDIO_DIR / f"asr_{uuid.uuid4().hex[:8]}.wav")
                try:
                    with open(temp_path, "wb") as f:
                        f.write(wav_bytes)
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        segments, _ = await asyncio.get_event_loop().run_in_executor(
                            pool, lambda: self._local_model.transcribe(
                                temp_path,
                                language=lang,
                                beam_size=int(os.environ.get("VOICEMATE_WHISPER_BEAM_SIZE", "3")),
                                temperature=0.0,
                                condition_on_previous_text=False,
                                without_timestamps=True,
                                vad_filter=True,
                                vad_parameters={
                                    "min_speech_duration_ms": int(os.environ.get("VOICEMATE_WHISPER_MIN_SPEECH_MS", "300")),
                                    "min_silence_duration_ms": int(os.environ.get("VOICEMATE_WHISPER_MIN_SILENCE_MS", "520")),
                                    "speech_pad_ms": int(os.environ.get("VOICEMATE_WHISPER_SPEECH_PAD_MS", "240")),
                                },
                            )
                        )
                    segment_list = list(segments)
                    max_no_speech_prob = max(
                        (getattr(seg, "no_speech_prob", 0.0) for seg in segment_list),
                        default=0.0,
                    )
                    if max_no_speech_prob > ASR_MAX_NO_SPEECH_PROB:
                        logger.info("STT rejected no-speech probability: %.2f", max_no_speech_prob)
                        text = ""
                    else:
                        text = "".join(seg.text for seg in segment_list).strip()
                except Exception as e:
                    logger.warning(f"Local Whisper failed: {e}")
                    text = ""
                finally:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
            else:
                # API-based Whisper
                try:
                    if self._client is None:
                        raise RuntimeError("Whisper API client is not configured")
                    transcript = await self._client.audio.transcriptions.create(
                        model="whisper-1",
                        file=("audio.wav", wav_bytes, "audio/wav"),
                        language=lang,
                        response_format="text",
                    )
                    text = transcript.strip()
                except Exception as e:
                    logger.warning(f"Whisper API failed: {e}")
                    text = ""

            if not text or _is_noise_text(text):
                logger.info("STT: empty transcription")
                return stt.SpeechEvent(
                    type=stt.SpeechEventType.END_OF_SPEECH,
                    alternatives=[],
                )

            if SPEAKER_LOCK_ENABLED and duration_seconds >= SPEAKER_LOCK_MIN_SECONDS and rms >= SPEAKER_LOCK_MIN_RMS:
                if self._speaker_profile is None:
                    self._speaker_profile = voice_features
                    logger.info("Speaker lock enrolled from first clear utterance")
                else:
                    alpha = max(0.0, min(SPEAKER_LOCK_LEARN_RATE, 1.0))
                    self._speaker_profile = tuple(
                        old * (1.0 - alpha) + new * alpha
                        for old, new in zip(self._speaker_profile, voice_features)
                    )

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


class EdgeTTS(tts.TTS):
    """Text-to-Speech using edge-tts (Microsoft Edge neural voices)."""

    def __init__(self):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._voice = TTS_VOICE

    @property
    def provider(self) -> str:
        return "edge-tts"

    def synthesize(self, text: str, **kwargs) -> tts.ChunkedStream:
        return EdgeTTSChunkedStream(self, text, conn_options=kwargs.get("conn_options"))


class EdgeTTSChunkedStream(tts.ChunkedStream):
    """Produces PCM audio frames from edge-tts, with Windows SAPI fallback."""

    def __init__(self, edge_tts_obj: EdgeTTS, text: str, conn_options=None):
        from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
        super().__init__(
            tts=edge_tts_obj,
            input_text=text,
            conn_options=conn_options or DEFAULT_API_CONNECT_OPTIONS,
        )
        self._tts = edge_tts_obj
        self._text = text

    async def _decode_to_pcm(self, input_bytes: bytes, input_format: str) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", input_format, "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", "24000", "-ac", "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pcm_data, stderr = await proc.communicate(input=input_bytes)
        if proc.returncode != 0:
            logger.error("ffmpeg decode failed: %s", stderr.decode(errors="replace")[:200])
            return b""
        return pcm_data

    async def _edge_pcm(self, text: str, emotion: str) -> bytes:
        import edge_tts
        profile = EMOTION_TTS_PROFILES.get(emotion, EMOTION_TTS_PROFILES["gentle"])
        effective_voice = resolve_edge_voice(self._tts._voice, emotion)
        communicate = edge_tts.Communicate(
            text,
            effective_voice,
            rate=profile.get("rate", "+0%"),
            pitch=profile.get("pitch", "+0Hz"),
        )
        mp3_buffer = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_buffer.extend(chunk["data"])
        if not mp3_buffer:
            return b""
        return await self._decode_to_pcm(bytes(mp3_buffer), "mp3")

    async def _sapi_pcm(self, text: str) -> bytes:
        import tempfile
        import shlex
        wav_path = Path(tempfile.gettempdir()) / f"voicemate_sapi_{uuid.uuid4().hex}.wav"
        txt_path = Path(tempfile.gettempdir()) / f"voicemate_sapi_{uuid.uuid4().hex}.txt"
        try:
            txt_path.write_text(text, encoding="utf-8")
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                f"$t = Get-Content -LiteralPath {shlex.quote(str(txt_path))} -Raw -Encoding UTF8; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.SetOutputToWaveFile({shlex.quote(str(wav_path))}); "
                "$s.Speak($t); $s.Dispose()"
            )
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await proc.communicate()
            if proc.returncode != 0 or not wav_path.exists():
                logger.error("Windows SAPI fallback failed: %s", stderr.decode(errors="replace")[:200])
                return b""
            return await self._decode_to_pcm(wav_path.read_bytes(), "wav")
        finally:
            for path in (wav_path, txt_path):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    async def _run(self, emitter: tts.AudioEmitter) -> None:
        emotion = detect_emotion(self._text)
        text = prepare_tts_text(strip_markdown(self._text), emotion)
        if not text:
            text = "嗯。"

        pcm_data = b""
        try:
            pcm_data = await self._edge_pcm(text, emotion)
        except Exception as e:
            logger.error("edge-tts synthesis error: %s", e)

        if not pcm_data:
            logger.warning("edge-tts produced no audio; using Windows SAPI fallback")
            try:
                pcm_data = await self._sapi_pcm(text)
            except Exception as e:
                logger.error("Windows SAPI synthesis error: %s", e)

        if not pcm_data:
            pcm_data = b"\x00" * 1920 * 5

        request_id = str(uuid.uuid4())
        emitter.initialize(
            request_id=request_id,
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


class MiMoLiveTTS(tts.TTS):
    """LiveKit TTS adapter for Xiaomi MiMo V2.5 TTS."""

    def __init__(self):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._mimo = MiMoTTS()

    @property
    def provider(self) -> str:
        return "xiaomi-mimo"

    @property
    def is_available(self) -> bool:
        return self._mimo.is_available

    def synthesize(self, text: str, **kwargs) -> tts.ChunkedStream:
        return MiMoLiveTTSChunkedStream(self, text)


class MiMoLiveTTSChunkedStream(tts.ChunkedStream):
    """Generates MiMo WAV, decodes to PCM, then pushes 20 ms LiveKit frames."""

    def __init__(self, mimo_tts_obj: MiMoLiveTTS, text: str):
        from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
        super().__init__(
            tts=mimo_tts_obj,
            input_text=text,
            conn_options=DEFAULT_API_CONNECT_OPTIONS,
        )
        self._tts = mimo_tts_obj
        self._text = text

    async def _run(self, emitter: tts.AudioEmitter) -> None:
        emotion = detect_emotion(self._text)
        text = strip_markdown(self._text)

        try:
            audio_path, _duration_ms = await self._tts._mimo.synthesize(text, emotion=emotion)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", audio_path,
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", "24000", "-ac", "1",
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            pcm_data, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(f"ffmpeg decode failed: {stderr.decode(errors='replace')[:200]}")
                return
            if not pcm_data or len(pcm_data) < 960:
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
                    chunk += b'\x00' * (frame_size - len(chunk))
                emitter.push(chunk)
                offset = end

            emitter.flush()
        except Exception as e:
            logger.error(f"MiMo TTS synthesis error: {e}")


class VoiceMateAgent(Agent):
    """VoiceMate Voice AI Agent for LiveKit.

    Pipeline: VAD → WhisperSTT → DeepSeekLLM → edge-tts TTS
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

        instructions = PERSONAS.get(self._persona, PERSONAS[DEFAULT_PERSONA])

        self._whisper_stt = WhisperSTT()
        self._deepseek_llm = DeepSeekLLM()
        self._edge_tts = EdgeTTS()
        self._mimo_tts = MiMoLiveTTS()
        self._active_tts = self._edge_tts
        if REALTIME_TTS_PROVIDER == "mimo":
            if self._mimo_tts.is_available:
                self._active_tts = self._mimo_tts
            else:
                logger.warning("VOICEMATE_REALTIME_TTS_PROVIDER=mimo but MIMO_API_KEY is not configured; using edge-tts")

        logger.info(
            f"VoiceMateAgent init (persona={self._persona}, "
            f"voice={self._voice}, tts={self._active_tts.provider}) [{self._conv_id}]"
        )

        super().__init__(
            instructions=instructions,
            stt=self._whisper_stt,
            vad=silero.VAD.load(
                min_speech_duration=float(os.environ.get("VOICEMATE_SILERO_MIN_SPEECH_SECONDS", "0.12")),
                min_silence_duration=float(os.environ.get("VOICEMATE_SILERO_MIN_SILENCE_SECONDS", "0.55")),
                prefix_padding_duration=float(os.environ.get("VOICEMATE_SILERO_PREFIX_PADDING_SECONDS", "0.30")),
                activation_threshold=float(os.environ.get("VOICEMATE_SILERO_ACTIVATION_THRESHOLD", "0.45")),
            ),
            llm=self._deepseek_llm,
            tts=self._active_tts,
            allow_interruptions=True,       # Barge-in
            min_endpointing_delay=float(os.environ.get("VOICEMATE_MIN_ENDPOINTING_DELAY", "0.55")),
            max_endpointing_delay=float(os.environ.get("VOICEMATE_MAX_ENDPOINTING_DELAY", "1.25")),
            min_consecutive_speech_delay=float(os.environ.get("VOICEMATE_MIN_CONSECUTIVE_SPEECH_DELAY", "0.35")),
            **kwargs,
        )

    async def on_enter(self):
        logger.info(f"VoiceMate agent entered room [{self._conv_id}]")

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage):
        user_text = ""
        if new_message and new_message.content:
            for c in new_message.content:
                if isinstance(c, str):
                    user_text += c
                elif hasattr(c, 'text') and c.text:
                    user_text += c.text

        logger.info(f"User turn: {user_text[:80]} [{self._conv_id}]")


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
            "but LLM calls will use the error fallback until a key is configured."
        )

    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        logger.warning("LiveKit credentials not fully configured.")

    logger.info("Starting VoiceMate LiveKit Agent")
    logger.info(f"  LiveKit: {LIVEKIT_URL}")
    logger.info(f"  DeepSeek: {DEEPSEEK_MODEL}")
    logger.info(f"  TTS Voice: {TTS_VOICE}")
    logger.info(f"  Persona: {DEFAULT_PERSONA}")
    logger.info(f"  Barge-in: enabled")

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
