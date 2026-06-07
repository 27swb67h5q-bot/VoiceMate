from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
from collections import OrderedDict
from hashlib import sha256

VERSION = "001"
APP_ID_LENGTH = 24

PRIV_PUBLISH_STREAM = 0
PRIV_PUBLISH_AUDIO_STREAM = 1
PRIV_PUBLISH_VIDEO_STREAM = 2
PRIV_PUBLISH_DATA_STREAM = 3
PRIV_SUBSCRIBE_STREAM = 4


def generate_rtc_token(
    *,
    app_id: str,
    app_key: str,
    room_id: str,
    user_id: str,
    ttl_seconds: int = 86400,
    can_publish: bool = True,
    can_subscribe: bool = True,
) -> str:
    if len(app_id) != APP_ID_LENGTH:
        raise ValueError("Volcengine RTC AppId must be 24 characters")
    if not app_key:
        raise ValueError("Volcengine RTC AppKey is required")
    if not room_id:
        raise ValueError("Volcengine RTC room_id is required")
    if not user_id:
        raise ValueError("Volcengine RTC user_id is required")

    now = int(time.time())
    expire_at = now + max(60, int(ttl_seconds))
    privileges: dict[int, int] = {}
    if can_subscribe:
        privileges[PRIV_SUBSCRIBE_STREAM] = expire_at
    if can_publish:
        privileges[PRIV_PUBLISH_STREAM] = expire_at
        privileges[PRIV_PUBLISH_AUDIO_STREAM] = expire_at
        privileges[PRIV_PUBLISH_VIDEO_STREAM] = expire_at
        privileges[PRIV_PUBLISH_DATA_STREAM] = expire_at
    if not privileges:
        raise ValueError("At least one RTC privilege is required")

    message = b"".join(
        [
            _pack_uint32(secrets.randbelow(99999999) + 1),
            _pack_uint32(now),
            _pack_uint32(expire_at),
            _pack_string(room_id),
            _pack_string(user_id),
            _pack_map_uint32(privileges),
        ]
    )
    signature = hmac.new(app_key.encode("utf-8"), message, sha256).digest()
    content = _pack_bytes(message) + _pack_bytes(signature)
    return VERSION + app_id + base64.b64encode(content).decode("utf-8")


def _pack_uint16(value: int) -> bytes:
    return struct.pack("<H", int(value))


def _pack_uint32(value: int) -> bytes:
    return struct.pack("<I", int(value))


def _pack_string(value: str) -> bytes:
    return _pack_bytes(value.encode("utf-8"))


def _pack_bytes(value: bytes) -> bytes:
    return _pack_uint16(len(value)) + value


def _pack_map_uint32(value: dict[int, int]) -> bytes:
    ordered = OrderedDict(sorted(value.items(), key=lambda item: int(item[0])))
    packed = _pack_uint16(len(ordered))
    for key, val in ordered.items():
        packed += _pack_uint16(key) + _pack_uint32(val)
    return packed
