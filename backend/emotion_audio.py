from __future__ import annotations

import math
import struct
from dataclasses import dataclass


@dataclass
class AudioEmotion:
    emotion: str
    confidence: float
    arousal: int
    valence: int
    rms: float
    zcr: float
    tempo: float


def analyze_pcm_emotion(pcm_data: bytes, sample_rate: int = 16000) -> AudioEmotion:
    sample_count = len(pcm_data) // 2
    if sample_count <= 8:
        return AudioEmotion("neutral", 0.0, 1, 0, 0.0, 0.0, 0.0)

    samples = struct.unpack_from(f"<{sample_count}h", pcm_data)
    abs_values = [abs(s) for s in samples]
    rms = math.sqrt(sum(s * s for s in samples) / sample_count)
    zcr = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0 <= b) or (a >= 0 > b)) / max(sample_count - 1, 1)

    frame = max(int(sample_rate * 0.08), 1)
    energies: list[float] = []
    for i in range(0, sample_count - frame + 1, frame):
        chunk = samples[i:i + frame]
        energies.append(math.sqrt(sum(s * s for s in chunk) / frame))
    if not energies:
        energies = [rms]

    active_threshold = max(180.0, rms * 0.42)
    active_frames = sum(1 for value in energies if value >= active_threshold)
    tempo = active_frames / max(len(energies), 1)
    variation = (max(energies) - min(energies)) / max(rms, 1.0)
    peak_ratio = max(abs_values) / max((sum(abs_values) / len(abs_values)), 1.0)

    if rms < 220:
        return AudioEmotion("lonely", 0.45, 1, -1, rms, zcr, tempo)
    if rms > 1400 and zcr > 0.13 and variation > 1.2:
        return AudioEmotion("angry", 0.72, 3, -2, rms, zcr, tempo)
    if tempo > 0.66 and zcr > 0.10 and peak_ratio < 8.0:
        return AudioEmotion("cheerful", 0.58, 2, 2, rms, zcr, tempo)
    if variation > 1.4 and tempo > 0.48:
        return AudioEmotion("anxious", 0.56, 3, -2, rms, zcr, tempo)
    if rms < 520 and tempo < 0.48:
        return AudioEmotion("comforting", 0.50, 1, -1, rms, zcr, tempo)
    return AudioEmotion("neutral", 0.35, 1, 0, rms, zcr, tempo)
