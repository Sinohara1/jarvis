"""Local speech-to-text with Vosk: small model per spoken language (ru shared with the name wake word;
uk/en/de/pl downloaded on demand, ~40–80 MB each, into %LOCALAPPDATA%\\Jarvis\\models).

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


def model_name(code: str = "ru") -> str:
    from .lang import LANGS, norm
    return LANGS[norm(code)]["vosk"][0]


def model_path(code: str = "ru") -> str | None:
    from .namewake import find_model
    return find_model(model_name(code))


def download(code: str, on_progress=None) -> str:
    from .lang import LANGS, norm
    from .namewake import download_model
    name, size = LANGS[norm(code)]["vosk"]
    return download_model(on_progress, url=f"https://alphacephei.com/vosk/models/{name}.zip", name=name, size=size)


def is_loaded(code: str | None = None) -> bool:
    if code is None:
        return bool(_models)
    p = model_path(code)
    return bool(p) and p in _models


def transcribe_wav(model, wav: bytes) -> tuple[str, float]:
    """Decode a whole recorded WAV (e.g. the request said right after the wake name)."""
    import io
    import wave
    with wave.open(io.BytesIO(wav)) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            return "", 0.0
        data = w.readframes(w.getnframes())
    st = StreamingSTT(model)
    for i in range(0, len(data), 2560):
        st.feed(data[i:i + 2560])
    return st.final()


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
