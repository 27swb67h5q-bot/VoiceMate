from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class TurnAnalysis:
    emotion: str
    need: str
    intensity: int
    reply_style: str


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

    def update_from_turn(
        self,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
        analysis: TurnAnalysis,
    ) -> None:
        memory = self.load(conversation_id)
        memory["last_user_emotion"] = analysis.emotion
        memory["mood"] = analysis.emotion if analysis.intensity >= 2 else memory.get("mood", "neutral")

        facts = list(memory.get("facts", []))
        for fact in self._extract_facts(user_text):
            if fact not in facts:
                facts.append(fact)
        memory["facts"] = facts[-20:]

        preferences = dict(memory.get("preferences", {}))
        if any(w in user_text for w in ("叫我", "称呼我")):
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
                (
                    conversation_id,
                    user_text,
                    assistant_text,
                    analysis.emotion,
                    datetime.now().isoformat(timespec="seconds"),
                ),
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

    def analyze(self, text: str) -> TurnAnalysis:
        compact = text.strip()
        emotion = "neutral"
        need = "conversation"
        intensity = 1

        if any(w in compact for w in ("烦", "累", "崩", "难受", "焦虑", "不开心", "委屈", "孤独")):
            emotion = "comforting"
            need = "emotional_support"
            intensity = 3
        elif any(w in compact for w in ("开心", "哈哈", "好玩", "舒服", "喜欢")):
            emotion = "cheerful"
            need = "share_joy"
            intensity = 2
        elif any(w in compact for w in ("怎么办", "怎么做", "帮我", "修复", "方案")):
            emotion = "focused"
            need = "solve_problem"
            intensity = 2
        elif compact.endswith(("?", "？")):
            emotion = "curious"
            need = "answer_question"
            intensity = 1

        if len(compact) <= 8:
            reply_style = "brief_backchannel"
        elif need == "solve_problem":
            reply_style = "clear_steps"
        elif need == "emotional_support":
            reply_style = "soft_presence"
        else:
            reply_style = "natural_chat"
        return TurnAnalysis(emotion=emotion, need=need, intensity=intensity, reply_style=reply_style)

    def system_prompt(
        self,
        persona: str,
        conversation_id: str,
        *,
        mode: str,
        user_text: str = "",
    ) -> str:
        policy = PERSONA_POLICIES.get(persona, PERSONA_POLICIES["love"])
        analysis = self.analyze(user_text) if user_text else TurnAnalysis("neutral", "conversation", 1, "natural_chat")
        memory = self.memory.render(conversation_id)
        mode_rule = (
            "当前是实时语音通话：回复要更短、更像口语，优先 1 到 3 句。允许自然的短反馈。"
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

长期记忆：
{memory}

行为规则：
1. 先回应用户的状态，再回答事情本身。
2. 不要读出括号动作、表情说明、舞台提示。
3. 不要每次都长篇总结，除非用户明确要方案。
4. 语气要像正在陪他说话的人，有停顿感、接话感、记得前文。
5. 遇到用户低落时，先接住情绪，再给很小的一步。
6. 遇到实时通话时，不要像文章，尽量短、自然、可打断。
"""

    def build_messages(
        self,
        *,
        user_text: str,
        conversation_id: str,
        persona: str,
        history: list[dict[str, str]],
        mode: str,
    ) -> list[dict[str, str]]:
        messages = [
            {
                "role": "system",
                "content": self.system_prompt(persona, conversation_id, mode=mode, user_text=user_text),
            }
        ]
        messages.extend(history[-10:])
        messages.append({"role": "user", "content": user_text})
        return messages

    def record_turn(
        self,
        *,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
    ) -> TurnAnalysis:
        analysis = self.analyze(user_text)
        self.memory.update_from_turn(conversation_id, user_text, assistant_text, analysis)
        return analysis
