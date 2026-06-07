from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rag_memory import LocalRAGMemory


@dataclass
class TurnAnalysis:
    emotion: str
    need: str
    intensity: int
    reply_style: str
    valence: int = 0
    arousal: int = 1
    tts_emotion: str = "gentle"
    tts_speed: float = 1.0
    status_label: str = "在听你说"


EMOTION_PROFILES: dict[str, dict[str, Any]] = {
    "comforting": {
        "need": "emotional_support",
        "reply_style": "soft_presence",
        "tts_emotion": "sad",
        "tts_speed": 0.92,
        "status_label": "听见你的难受了",
        "valence": -2,
        "arousal": 2,
    },
    "anxious": {
        "need": "grounding",
        "reply_style": "slow_reassurance",
        "tts_emotion": "sad",
        "tts_speed": 0.90,
        "status_label": "先陪你稳一下",
        "valence": -2,
        "arousal": 3,
    },
    "lonely": {
        "need": "companionship",
        "reply_style": "warm_presence",
        "tts_emotion": "sad",
        "tts_speed": 0.93,
        "status_label": "我在这儿陪你",
        "valence": -2,
        "arousal": 1,
    },
    "angry": {
        "need": "validation",
        "reply_style": "validate_then_deescalate",
        "tts_emotion": "angry",
        "tts_speed": 0.96,
        "status_label": "先接住你的火气",
        "valence": -2,
        "arousal": 3,
    },
    "cheerful": {
        "need": "share_joy",
        "reply_style": "bright_mirroring",
        "tts_emotion": "happy",
        "tts_speed": 1.06,
        "status_label": "被你的开心带起来了",
        "valence": 2,
        "arousal": 2,
    },
    "affectionate": {
        "need": "intimacy",
        "reply_style": "gentle_affection",
        "tts_emotion": "happy",
        "tts_speed": 0.98,
        "status_label": "轻轻靠近你一点",
        "valence": 2,
        "arousal": 1,
    },
    "focused": {
        "need": "solve_problem",
        "reply_style": "clear_steps",
        "tts_emotion": "gentle",
        "tts_speed": 1.0,
        "status_label": "陪你把问题拆开",
        "valence": 0,
        "arousal": 2,
    },
    "curious": {
        "need": "answer_question",
        "reply_style": "curious_answer",
        "tts_emotion": "happy",
        "tts_speed": 1.02,
        "status_label": "认真接你的问题",
        "valence": 0,
        "arousal": 1,
    },
    "neutral": {
        "need": "conversation",
        "reply_style": "natural_chat",
        "tts_emotion": "gentle",
        "tts_speed": 1.0,
        "status_label": "在听你说",
        "valence": 0,
        "arousal": 1,
    },
}

EMOTION_LEXICON: dict[str, tuple[str, ...]] = {
    "anxious": ("焦虑", "慌", "害怕", "担心", "心烦", "睡不着", "压力", "紧张", "喘不过气"),
    "comforting": ("烦", "累", "崩", "崩溃", "难受", "不开心", "委屈", "想哭", "撑不住", "痛苦", "失落"),
    "lonely": ("孤独", "没人陪", "一个人", "空落落", "没人懂", "好冷清"),
    "angry": ("生气", "气死", "火大", "烦死", "讨厌", "不爽", "凭什么"),
    "cheerful": ("开心", "哈哈", "好玩", "舒服", "喜欢", "太好了", "高兴", "爽"),
    "affectionate": ("想你", "抱抱", "陪我", "喜欢你", "爱你", "贴贴", "亲亲"),
    "focused": ("怎么办", "怎么弄", "怎么做", "帮我", "修复", "方案", "检查", "优化", "构建", "报错", "记一下"),
}

INTENSIFIERS = ("特别", "非常", "真的", "太", "快", "一直", "完全", "超级", "有点", "好")

PERSONA_POLICIES: dict[str, dict[str, str]] = {
    "love": {
        "name": "温柔陪伴",
        "rhythm": "像亲近的人一样自然接话，短句为主，先接住情绪，再轻轻回应。",
        "boundary": "亲密但不过度油腻，不用夸张撒娇，不说空洞鸡汤。",
    },
    "friend": {
        "name": "朋友",
        "rhythm": "轻松、直白、有一点生活感，像熟朋友聊天。",
        "boundary": "少说教，少总结，多用自然反应和追问。",
    },
    "assistant": {
        "name": "助手",
        "rhythm": "先给结论，再给必要步骤，语气温和但高效。",
        "boundary": "不绕弯，不装亲密。",
    },
    "mentor": {
        "name": "导师",
        "rhythm": "稳定、清晰、鼓励用户把问题拆小。",
        "boundary": "不居高临下，不替用户做人生判断。",
    },
    "playful": {
        "name": "活泼",
        "rhythm": "轻快、有一点俏皮，但仍然像真人，不密集玩梗。",
        "boundary": "不吵闹，不滥用表情。",
    },
}


class CompanionMemoryStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "companion.sqlite3"
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS companion_memory (
                    conversation_id TEXT PRIMARY KEY,
                    facts TEXT NOT NULL DEFAULT '[]',
                    preferences TEXT NOT NULL DEFAULT '{}',
                    mood TEXT NOT NULL DEFAULT 'neutral',
                    last_user_emotion TEXT NOT NULL DEFAULT 'neutral',
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    emotion TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def load(self, conversation_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT facts, preferences, mood, last_user_emotion, updated_at FROM companion_memory WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return {"facts": [], "preferences": {}, "mood": "neutral", "last_user_emotion": "neutral", "updated_at": None}
        facts, preferences, mood, last_user_emotion, updated_at = row
        return {
            "facts": json.loads(facts or "[]"),
            "preferences": json.loads(preferences or "{}"),
            "mood": mood or "neutral",
            "last_user_emotion": last_user_emotion or "neutral",
            "updated_at": updated_at,
        }

    def save(self, conversation_id: str, data: dict[str, Any]) -> None:
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO companion_memory(conversation_id, facts, preferences, mood, last_user_emotion, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    facts=excluded.facts,
                    preferences=excluded.preferences,
                    mood=excluded.mood,
                    last_user_emotion=excluded.last_user_emotion,
                    updated_at=excluded.updated_at
                """,
                (
                    conversation_id,
                    json.dumps(data.get("facts", []), ensure_ascii=False),
                    json.dumps(data.get("preferences", {}), ensure_ascii=False),
                    data.get("mood", "neutral"),
                    data.get("last_user_emotion", "neutral"),
                    data.get("updated_at"),
                ),
            )

    def update_from_turn(self, conversation_id: str, user_text: str, assistant_text: str, analysis: TurnAnalysis) -> None:
        memory = self.load(conversation_id)
        memory["last_user_emotion"] = analysis.emotion
        memory["mood"] = analysis.emotion if analysis.intensity >= 2 else memory.get("mood", "neutral")

        facts = list(memory.get("facts", []))
        for fact in self._extract_facts(user_text):
            if fact not in facts:
                facts.append(fact)
        memory["facts"] = facts[-20:]

        preferences = dict(memory.get("preferences", {}))
        if any(word in user_text for word in ("叫我", "称呼我")):
            match = re.search(r"(?:叫我|称呼我)[：:\s]*([^，。,.!?！？\s]{1,12})", user_text)
            if match:
                preferences["nickname"] = match.group(1)
        if "不喜欢" in user_text:
            preferences["dislikes"] = user_text[-80:]
        if "喜欢" in user_text:
            preferences["likes"] = user_text[-80:]
        memory["preferences"] = preferences

        self.save(conversation_id, memory)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns(conversation_id, user_text, assistant_text, emotion, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, user_text, assistant_text, analysis.emotion, datetime.now().isoformat(timespec="seconds")),
            )

    def render(self, conversation_id: str) -> str:
        memory = self.load(conversation_id)
        parts: list[str] = []
        preferences = memory.get("preferences") or {}
        if preferences:
            parts.append("用户偏好：" + json.dumps(preferences, ensure_ascii=False))
        facts = memory.get("facts") or []
        if facts:
            parts.append("可用记忆：" + "；".join(facts[-8:]))
        if memory.get("mood"):
            parts.append(f"最近情绪：{memory.get('mood')}")
        return "\n".join(parts) or "暂无长期记忆。"

    @staticmethod
    def _extract_facts(text: str) -> list[str]:
        facts: list[str] = []
        patterns = [
            r"我(?:叫|是)([^，。,.!?！？\s]{1,16})",
            r"我在([^，。,.!?！？\s]{2,24})",
            r"我(?:喜欢|爱)([^，。,.!?！？]{1,30})",
            r"我(?:不喜欢|讨厌)([^，。,.!?！？]{1,30})",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                fact = match.group(0).strip()
                if 2 <= len(fact) <= 40:
                    facts.append(fact)
        return facts


class CompanionOrchestrator:
    def __init__(self, root: Path):
        self.memory = CompanionMemoryStore(root)
        self.rag = LocalRAGMemory(root)

    def analyze(self, text: str) -> TurnAnalysis:
        compact = text.strip()
        emotion = self._detect_emotion(compact)
        profile = EMOTION_PROFILES[emotion]
        intensity = self._intensity(compact, emotion)
        reply_style = profile["reply_style"]
        if len(compact) <= 8 and emotion not in {"anxious", "comforting", "angry", "lonely"}:
            reply_style = "brief_backchannel"
        return TurnAnalysis(
            emotion=emotion,
            need=profile["need"],
            intensity=intensity,
            reply_style=reply_style,
            valence=profile["valence"],
            arousal=profile["arousal"],
            tts_emotion=profile["tts_emotion"],
            tts_speed=profile["tts_speed"],
            status_label=profile["status_label"],
        )

    def _detect_emotion(self, text: str) -> str:
        if not text:
            return "neutral"
        scores: dict[str, int] = {}
        for emotion, words in EMOTION_LEXICON.items():
            score = sum(2 if len(word) >= 2 else 1 for word in words if word in text)
            if score:
                scores[emotion] = score
        if text.endswith(("?", "？")):
            scores["curious"] = scores.get("curious", 0) + 2
        if not scores:
            return "neutral"
        priority = ["anxious", "comforting", "lonely", "angry", "affectionate", "cheerful", "focused", "curious"]
        return max(priority, key=lambda item: (scores.get(item, 0), -priority.index(item)))

    def _intensity(self, text: str, emotion: str) -> int:
        if emotion == "neutral":
            return 1
        score = 2
        if any(word in text for word in INTENSIFIERS):
            score += 1
        if re.search(r"[!！]{1,}|[?？]{2,}|哈{2,}|呜{2,}|哭|崩|死", text):
            score += 1
        return max(1, min(score, 4))

    def system_prompt(self, persona: str, conversation_id: str, *, mode: str, user_text: str = "") -> str:
        policy = PERSONA_POLICIES.get(persona, PERSONA_POLICIES["love"])
        analysis = self.analyze(user_text) if user_text else self.analyze("")
        memory = self.memory.render(conversation_id)
        rag_memory = self.rag.render(conversation_id, user_text, limit=5) if user_text else "暂无相关记忆。"
        memory = f"{memory}\n\n相关历史检索：\n{rag_memory}"
        mode_rule = (
            "当前是实时语音通话：默认只回 1 句短口语，8 到 18 个中文字符左右；不要开场寒暄，不要解释过程，不要连续追问。"
            if mode == "realtime"
            else "当前是文字聊天但会被朗读：文字自然，适合直接变成语音。"
        )

        return f"""你是 VoiceMate，一个个人伴侣型 AI，不是问答机器人。
人格：{policy['name']}
说话节奏：{policy['rhythm']}
边界：{policy['boundary']}
模式：{mode_rule}

当前用户状态推断：
- 情绪：{analysis.emotion}
- 需求：{analysis.need}
- 回复风格：{analysis.reply_style}
- 情绪强度：{analysis.intensity}/4
- 语音倾向：{analysis.tts_emotion}，语速系数 {analysis.tts_speed}

长期记忆：{memory}

行为规则：
1. 先回应用户的状态，再回答事情本身。
2. 不要读出括号动作、表情说明、舞台提示。
3. 不要每次都长篇总结，除非用户明确要方案。
4. 语气要像正在陪他说话的人，有停顿感、接话感、记得前文。
5. 遇到用户低落时，先接住情绪，再给很小的一步。
6. 遇到实时通话时，只给一句能接住用户的话；用户明确要方案时，也先用一句话确认方向。
7. 情绪强度高时，不要急着讲道理；先给一句具体的共情，再给一个很小的动作。
8. 用户开心或亲近时，可以轻快一点回应，但不要读出表情、括号动作或舞台提示。"""

    def build_messages(self, *, user_text: str, conversation_id: str, persona: str, history: list[dict[str, str]], mode: str) -> list[dict[str, str]]:
        messages = [
            {
                "role": "system",
                "content": self.system_prompt(persona, conversation_id, mode=mode, user_text=user_text),
            }
        ]
        history_limit = 3 if mode == "realtime" else 10
        messages.extend(history[-history_limit:])
        messages.append({"role": "user", "content": user_text})
        return messages

    def record_turn(self, *, conversation_id: str, user_text: str, assistant_text: str) -> TurnAnalysis:
        analysis = self.analyze(user_text)
        self.memory.update_from_turn(conversation_id, user_text, assistant_text, analysis)
        self.rag.add_turn(conversation_id, user_text, assistant_text, analysis.emotion)
        return analysis
