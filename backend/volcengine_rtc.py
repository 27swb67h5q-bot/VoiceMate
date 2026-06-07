from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
from volcengine_rtc_token import generate_rtc_token


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _json_env(name: str) -> dict[str, Any]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"{name} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return value


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class VolcengineRTCConfig:
    app_id: str
    app_key: str
    token_url: str
    access_key_id: str
    secret_access_key: str
    region: str
    openapi_host: str
    openapi_version: str
    business_id: str
    bot_user_id: str
    start_voice_chat: bool
    custom_llm_url: str
    asr_silence_time_ms: int
    asr_expire_time_ms: int
    interrupt_speech_ms: int
    history_length: int
    llm_model: str
    llm_prefill: bool
    llm_thinking_type: str
    tts_voice_type: str
    tts_model: str
    tts_emotion: str
    tts_speech_rate: int

    @classmethod
    def from_env(cls) -> "VolcengineRTCConfig":
        public_host = os.environ.get("VOICEMATE_PUBLIC_HOST", "192.168.10.233")
        public_port = os.environ.get("VOICEMATE_PORT", "8000")
        return cls(
            app_id=os.environ.get("VOLCENGINE_RTC_APP_ID", ""),
            app_key=os.environ.get("VOLCENGINE_RTC_APP_KEY", ""),
            token_url=os.environ.get("VOLCENGINE_RTC_TOKEN_URL", ""),
            access_key_id=os.environ.get("VOLCENGINE_ACCESS_KEY_ID", os.environ.get("VOLCENGINE_RTC_ACCESS_KEY_ID", "")),
            secret_access_key=os.environ.get("VOLCENGINE_SECRET_ACCESS_KEY", os.environ.get("VOLCENGINE_RTC_SECRET_ACCESS_KEY", "")),
            region=os.environ.get("VOLCENGINE_RTC_REGION", "cn-north-1"),
            openapi_host=os.environ.get("VOLCENGINE_RTC_OPENAPI_HOST", "rtc.volcengineapi.com"),
            openapi_version=os.environ.get("VOLCENGINE_RTC_OPENAPI_VERSION", "2024-06-01"),
            business_id=os.environ.get("VOLCENGINE_RTC_BUSINESS_ID", ""),
            bot_user_id=os.environ.get("VOLCENGINE_RTC_BOT_USER_ID", "VoiceMateAI"),
            start_voice_chat=_env_bool("VOLCENGINE_RTC_START_VOICE_CHAT", False),
            custom_llm_url=os.environ.get("VOLCENGINE_CUSTOM_LLM_URL", f"http://{public_host}:{public_port}/v1/volcengine/custom-llm"),
            asr_silence_time_ms=int(os.environ.get("VOLCENGINE_RTC_ASR_SILENCE_TIME_MS", "450")),
            asr_expire_time_ms=int(os.environ.get("VOLCENGINE_RTC_ASR_EXPIRE_TIME_MS", "900")),
            interrupt_speech_ms=int(os.environ.get("VOLCENGINE_RTC_INTERRUPT_SPEECH_MS", "120")),
            history_length=int(os.environ.get("VOLCENGINE_RTC_HISTORY_LENGTH", "4")),
            llm_model=os.environ.get("VOLCENGINE_RTC_LLM_MODEL", "doubao-seed-1-6-flash"),
            llm_prefill=_env_bool("VOLCENGINE_RTC_LLM_PREFILL", True),
            llm_thinking_type=os.environ.get("VOLCENGINE_RTC_LLM_THINKING_TYPE", "disabled"),
            tts_voice_type=os.environ.get("VOLCENGINE_TTS_VOICE_TYPE", "zh_female_qingxinnvsheng_mars_bigtts"),
            tts_model=os.environ.get("VOLCENGINE_RTC_TTS_MODEL", os.environ.get("VOLCENGINE_TTS_MODEL", "seed-tts-2.0-expressive")),
            tts_emotion=os.environ.get("VOLCENGINE_RTC_TTS_EMOTION", "happy"),
            tts_speech_rate=int(os.environ.get("VOLCENGINE_RTC_TTS_SPEECH_RATE", "0")),
        )

    @property
    def can_issue_rtc_token(self) -> bool:
        return bool(self.token_url or self.app_key)

    @property
    def can_call_openapi(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key and self.app_id)

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.can_issue_rtc_token)


class VolcengineOpenAPIClient:
    def __init__(self, config: VolcengineRTCConfig):
        self.config = config

    async def invoke(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.config.can_call_openapi:
            raise RuntimeError("Volcengine OpenAPI AK/SK and AppId are not configured")

        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        now = dt.datetime.utcnow()
        date = now.strftime("%Y%m%d")
        x_date = now.strftime("%Y%m%dT%H%M%SZ")
        query = urlencode({"Action": action, "Version": self.config.openapi_version})
        host = self.config.openapi_host
        headers = {
            "Content-Type": "application/json",
            "Host": host,
            "X-Date": x_date,
        }
        headers["Authorization"] = self._authorization("POST", "/", query, headers, payload, date)
        url = f"https://{host}?{query}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload, headers=headers, timeout=20) as response:
                text = await response.text()
                try:
                    data = json.loads(text)
                except Exception:
                    data = {"raw": text}
                if response.status >= 400:
                    raise RuntimeError(f"Volcengine {action} failed: HTTP {response.status} {data}")
                return data

    def _authorization(
        self,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        payload: bytes,
        date: str,
    ) -> str:
        signed_headers = "content-type;host;x-date"
        canonical_headers = (
            f"content-type:{headers['Content-Type']}\n"
            f"host:{headers['Host']}\n"
            f"x-date:{headers['X-Date']}\n"
        )
        payload_hash = hashlib.sha256(payload).hexdigest()
        canonical_request = "\n".join([method, path, query, canonical_headers, signed_headers, payload_hash])
        credential_scope = f"{date}/{self.config.region}/rtc/request"
        string_to_sign = "\n".join(
            [
                "HMAC-SHA256",
                headers["X-Date"],
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = self._signing_key(date)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return (
            "HMAC-SHA256 "
            f"Credential={self.config.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

    def _signing_key(self, date: str) -> bytes:
        secret = self.config.secret_access_key.encode("utf-8")
        k_date = hmac.new(secret, date.encode("utf-8"), hashlib.sha256).digest()
        k_region = hmac.new(k_date, self.config.region.encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"rtc", hashlib.sha256).digest()
        return hmac.new(k_service, b"request", hashlib.sha256).digest()


class VolcengineRTCService:
    def __init__(self, config: VolcengineRTCConfig | None = None):
        self.config = config or VolcengineRTCConfig.from_env()
        self.openapi = VolcengineOpenAPIClient(self.config)

    async def create_client_session(
        self,
        *,
        room_id: str | None = None,
        user_id: str | None = None,
        persona: str | None = None,
        voice: str | None = None,
        speed: float | None = None,
    ) -> dict[str, Any]:
        room_id = room_id or f"voicemate-{uuid.uuid4().hex[:8]}"
        user_id = user_id or f"ios-{uuid.uuid4().hex[:8]}"
        token = await self._issue_token(room_id=room_id, user_id=user_id)
        task_id = f"vm-{uuid.uuid4().hex[:12]}"
        start_result: dict[str, Any] | None = None
        if self.config.start_voice_chat:
            start_result = await self.start_voice_chat(
                room_id=room_id,
                task_id=task_id,
                persona=persona,
                voice=voice,
                speed=speed,
            )
        return {
            "provider": "volcengine",
            "configured": self.config.configured,
            "app_id": self.config.app_id,
            "room_id": room_id,
            "user_id": user_id,
            "token": token,
            "task_id": task_id,
            "business_id": self.config.business_id,
            "bot_user_id": self.config.bot_user_id,
            "custom_llm_url": self.config.custom_llm_url,
            "voice_chat_started": bool(start_result),
            "voice_chat_result": start_result,
            "needs": self.missing_requirements(),
        }

    async def start_voice_chat(
        self,
        *,
        room_id: str,
        task_id: str | None = None,
        persona: str | None = None,
        voice: str | None = None,
        speed: float | None = None,
    ) -> dict[str, Any]:
        body = self.build_start_voice_chat_body(
            room_id=room_id,
            task_id=task_id,
            persona=persona,
            voice=voice,
            speed=speed,
        )
        return await self.openapi.invoke("StartVoiceChat", body)

    async def update_voice_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.openapi.invoke("UpdateVoiceChat", body)

    async def stop_voice_chat(self, *, room_id: str, task_id: str) -> dict[str, Any]:
        body = {
            "AppId": self.config.app_id,
            "RoomId": room_id,
            "TaskId": task_id,
        }
        body = _deep_merge(body, _json_env("VOLCENGINE_STOP_VOICE_CHAT_CONFIG_JSON"))
        return await self.openapi.invoke("StopVoiceChat", body)

    def build_start_voice_chat_body(
        self,
        *,
        room_id: str,
        task_id: str | None = None,
        persona: str | None = None,
        voice: str | None = None,
        speed: float | None = None,
    ) -> dict[str, Any]:
        task_id = task_id or f"vm-{uuid.uuid4().hex[:12]}"
        body: dict[str, Any] = {
            "AppId": self.config.app_id,
            "RoomId": room_id,
            "TaskId": task_id,
            "BusinessId": self.config.business_id,
            "AgentConfig": {
                "UserId": self.config.bot_user_id,
                "Burst": {"Enable": True},
            },
            "ASRConfig": {
                "VADConfig": {
                    "SilenceTime": self.config.asr_silence_time_ms,
                },
                "ExpireTime": self.config.asr_expire_time_ms,
            },
            "InterruptConfig": {
                "InterruptSpeechDuration": self.config.interrupt_speech_ms,
            },
            "LLMConfig": {
                "Mode": "CustomLLM",
                "CustomLLM": {
                    "URL": self.config.custom_llm_url,
                    "Headers": {
                        "X-VoiceMate-Persona": persona or "",
                    },
                },
                "Model": self.config.llm_model,
                "HistoryLength": self.config.history_length,
                "Prefill": self.config.llm_prefill,
                "ThinkingType": self.config.llm_thinking_type,
            },
            "TTSConfig": {
                "VoiceType": voice or self.config.tts_voice_type,
                "Model": self.config.tts_model,
                "Emotion": self.config.tts_emotion,
                "SpeechRate": int(((speed or 1.0) - 1.0) * 100) + self.config.tts_speech_rate,
            },
            "Context": {
                "TagParse": True,
            },
        }
        if not self.config.business_id:
            body.pop("BusinessId", None)
        return _deep_merge(body, _json_env("VOLCENGINE_START_VOICE_CHAT_CONFIG_JSON"))

    async def _issue_token(self, *, room_id: str, user_id: str) -> str:
        if self.config.token_url:
            payload = {
                "app_id": self.config.app_id,
                "room_id": room_id,
                "user_id": user_id,
                "ttl": int(os.environ.get("VOLCENGINE_RTC_TOKEN_TTL_SECONDS", "3600")),
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(self.config.token_url, json=payload, timeout=12) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        raise RuntimeError(f"Volcengine token service failed: HTTP {response.status} {data}")
                    token = data.get("token") or data.get("Token")
                    if not token:
                        raise RuntimeError("Volcengine token service did not return token")
                    return str(token)

        if self.config.app_key:
            return generate_rtc_token(
                app_id=self.config.app_id,
                app_key=self.config.app_key,
                room_id=room_id,
                user_id=user_id,
                ttl_seconds=int(os.environ.get("VOLCENGINE_RTC_TOKEN_TTL_SECONDS", "86400")),
            )

        raise RuntimeError("VOLCENGINE_RTC_TOKEN_URL or VOLCENGINE_RTC_APP_KEY is required")

    def capabilities(self) -> dict[str, Any]:
        return {
            "configured": self.config.configured,
            "app_id_present": bool(self.config.app_id),
            "app_key_present": bool(self.config.app_key),
            "token_url_present": bool(self.config.token_url),
            "token_provider": "external_url" if self.config.token_url else "official_app_key",
            "openapi_configured": self.config.can_call_openapi,
            "start_voice_chat_enabled": self.config.start_voice_chat,
            "custom_llm_url": self.config.custom_llm_url,
            "missing": self.missing_requirements(),
            "latency_profile": {
                "asr_silence_time_ms": self.config.asr_silence_time_ms,
                "asr_expire_time_ms": self.config.asr_expire_time_ms,
                "interrupt_speech_ms": self.config.interrupt_speech_ms,
                "llm_prefill": self.config.llm_prefill,
                "llm_thinking_type": self.config.llm_thinking_type,
                "history_length": self.config.history_length,
            },
        }

    def missing_requirements(self) -> list[str]:
        missing: list[str] = []
        if not self.config.app_id:
            missing.append("VOLCENGINE_RTC_APP_ID")
        if not (self.config.token_url or self.config.app_key):
            missing.append("VOLCENGINE_RTC_TOKEN_URL or VOLCENGINE_RTC_APP_KEY")
        if self.config.start_voice_chat and not self.config.can_call_openapi:
            missing.append("VOLCENGINE_ACCESS_KEY_ID and VOLCENGINE_SECRET_ACCESS_KEY")
        return missing
