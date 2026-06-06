from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Timer:
    store: "MetricsStore"
    name: str
    started: float

    def stop(self, **tags: str) -> float:
        value_ms = (time.perf_counter() - self.started) * 1000
        self.store.record(self.name, value_ms, tags)
        return value_ms


class MetricsStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "metrics.sqlite3"
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    value_ms REAL NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_name_created ON metrics(name, created_at)")

    def timer(self, name: str) -> Timer:
        return Timer(self, name, time.perf_counter())

    def record(self, name: str, value_ms: float, tags: dict[str, str] | None = None) -> None:
        tag_text = ",".join(f"{k}={v}" for k, v in sorted((tags or {}).items()))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO metrics(name, value_ms, tags, created_at) VALUES (?, ?, ?, ?)",
                (name, float(value_ms), tag_text, time.time()),
            )

    def summary(self, *, window_seconds: int = 3600) -> dict[str, Any]:
        cutoff = time.time() - window_seconds
        rows: list[tuple[str, float]] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, value_ms FROM metrics WHERE created_at >= ? ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        grouped: dict[str, list[float]] = {}
        for name, value in rows:
            grouped.setdefault(name, []).append(float(value))
        return {
            name: {
                "count": len(values),
                "avg_ms": round(sum(values) / len(values), 1),
                "p50_ms": round(self._percentile(values, 0.50), 1),
                "p90_ms": round(self._percentile(values, 0.90), 1),
                "latest_ms": round(values[0], 1),
            }
            for name, values in sorted(grouped.items())
        }

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
        return ordered[index]
