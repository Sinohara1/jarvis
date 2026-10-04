"""Local speech-to-text with Vosk (the same small Russian model the name wake word uses).

The recognizer is fed *while* the user speaks, so when the pause is detected the text is ready
in ~10–50 ms instead of a 1–2 s round trip to Gemini, and the voice turn costs one API request
instead of two. Measured on edge-tts questions: ~16 % of one CPU core while speaking.
"""

from __future__ import annotations

import json
import logging
import threading

log = logging.getLogger("jarvis")

_models: dict[str, object] = {}
_lock = threading.Lock()


def get_model(path: str):
    """Load a Vosk model once per process (name wake + dictation share it: ~0.7 s, ~100 MB RAM)."""
    with _lock:
        m = _models.get(path)
        if m is None:
            import vosk
            vosk.SetLogLevel(-1)
            m = vosk.Model(path)
            _models[path] = m
        return m


def model_path() -> str | None:
    from .namewake import find_model
    return find_model()


def is_loaded() -> bool:
    return bool(_models)


class StreamingSTT:
    """Feed 16 kHz int16 chunks during recording; final() → (text, mean word confidence)."""

    def __init__(self, model) -> None:
        import vosk
        self.rec = vosk.KaldiRecognizer(model, 16000)
        self.rec.SetWords(True)
        self.words: list[dict] = []
        self.fed = 0

    def feed(self, chunk) -> None:
        b = chunk.tobytes() if hasattr(chunk, "tobytes") else bytes(chunk)
        self.fed += len(b) // 2
        if self.rec.AcceptWaveform(b):
            self._take(self.rec.Result())

    def _take(self, raw: str) -> None:
        try:
            self.words.extend(json.loads(raw).get("result") or [])
        except ValueError:
            pass

    def final(self) -> tuple[str, float]:
        self._take(self.rec.FinalResult())
        return words_text(self.words), words_conf(self.words)


def words_text(words: list[dict]) -> str:
    t = " ".join(str(w.get("word", "")) for w in words).strip()
    return (t[:1].upper() + t[1:]) if t else ""


def words_conf(words: list[dict]) -> float:
    if not words:
        return 0.0
    return sum(float(w.get("conf", 1.0)) for w in words) / len(words)


def acceptable(text: str, conf: float, min_conf: float = 0.55) -> bool:
    """Is the local transcript good enough to skip the cloud? Empty / one-letter / low confidence → no."""
    t = (text or "").strip()
    if len(t.replace(" ", "")) < 2:
        return False
    return conf >= min_conf
