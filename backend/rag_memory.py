from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class MemoryHit:
    text: str
    score: float
    kind: str


class LocalRAGMemory:
    def __init__(self, root: Path, *, dimensions: int = 128):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "rag_memory.sqlite3"
        self.dimensions = dimensions
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    vector TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_conversation ON rag_items(conversation_id, created_at)")

    def add_turn(self, conversation_id: str, user_text: str, assistant_text: str, emotion: str) -> None:
        text = self._compact_memory_text(user_text, assistant_text, emotion)
        if not text:
            return
        vector = self._encode(text)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rag_items(conversation_id, kind, text, vector, created_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, "turn", text, ",".join(f"{v:.5f}" for v in vector), datetime.now().isoformat(timespec="seconds")),
            )

    def search(self, conversation_id: str, query: str, *, limit: int = 5) -> list[MemoryHit]:
        query_vector = self._encode(query)
        rows = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT kind, text, vector FROM rag_items
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT 200
                """,
                (conversation_id,),
            ).fetchall()
        hits: list[MemoryHit] = []
        for kind, text, vector_text in rows:
            vector = [float(v) for v in vector_text.split(",") if v]
            score = self._cosine(query_vector, vector)
            if score >= 0.12:
                hits.append(MemoryHit(text=text, score=score, kind=kind))
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]

    def render(self, conversation_id: str, query: str, *, limit: int = 5) -> str:
        hits = self.search(conversation_id, query, limit=limit)
        if not hits:
            return "暂无相关记忆。"
        return "\n".join(f"- {hit.text}" for hit in hits)

    @staticmethod
    def _compact_memory_text(user_text: str, assistant_text: str, emotion: str) -> str:
        user_text = re.sub(r"\s+", " ", user_text).strip()
        assistant_text = re.sub(r"\s+", " ", assistant_text).strip()
        if len(user_text) < 3:
            return ""
        return f"用户说：{user_text[:120]}；情绪：{emotion}；回应：{assistant_text[:120]}"

    def _encode(self, text: str) -> list[float]:
        clean = re.sub(r"\s+", "", text.lower())
        grams: list[str] = []
        for size in (1, 2, 3):
            grams.extend(clean[i:i + size] for i in range(max(0, len(clean) - size + 1)))
        vector = [0.0] * self.dimensions
        for gram in grams:
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=4).digest()
            index = int.from_bytes(digest, "little") % self.dimensions
            vector[index] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or not right:
            return 0.0
        size = min(len(left), len(right))
        return sum(left[i] * right[i] for i in range(size))
