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


# ─── bilingual second pass (v1.5.x) ──────────────────────────────────────────
# The Russian small model can only spell English in Cyrillic («райт нехто он телеграм…»). When a
# Russian transcript looks like an English command (stt_commands.wants_english_pass) the same WAV is
# decoded once more with the small English model (~41 MB, downloaded lazily the first time such a
# phrase is heard; ~0.5 s load once, then ~50–150 ms per short phrase) and the better reading wins.

# English model used for that pass: the small one (41 MB) or, on request («stt_en_model»: "lgraph"),
# vosk-model-en-us-0.22-lgraph (128 MB, WER 7.8 vs 9.9 on LibriSpeech, still fast on CPU).
EN_PASS_MODELS = {"lgraph": ("vosk-model-en-us-0.22-lgraph", 130_557_655)}

_en_dl: dict[str, threading.Event] = {}


def _en_path(variant: str) -> str | None:
    if variant in EN_PASS_MODELS:
        from .namewake import find_model
        return find_model(EN_PASS_MODELS[variant][0])
    return model_path("en")


def _download_en(variant: str) -> str:
    if variant in EN_PASS_MODELS:
        from .namewake import download_model
        name, size = EN_PASS_MODELS[variant]
        return download_model(None, url=f"https://alphacephei.com/vosk/models/{name}.zip", name=name, size=size)
    return download("en")


def english_model(*, allow_download: bool = True, variant: str = "small"):
    """The loaded English Vosk model, or None. Never blocks on a download: if the model is missing
    and ``allow_download`` it is fetched in the background and used from the next phrase on. A missing
    «lgraph» model falls back to the small one meanwhile."""
    variant = variant if variant in EN_PASS_MODELS else "small"
    try:
        path = _en_path(variant)
    except Exception:
        path = None
    if path:
        try:
            return get_model(path)
        except Exception as e:
            log.warning("english stt model (%s) load failed: %s", variant, e)
            return None
    ev = _en_dl.setdefault(variant, threading.Event())
    if allow_download and not ev.is_set():
        ev.set()

        def work() -> None:
            try:
                log.info("bilingual stt: downloading the English Vosk model %s (once)", variant)
                p = _download_en(variant)
                get_model(p)
                log.info("bilingual stt: English model %s ready", variant)
            except Exception as e:
                log.warning("bilingual stt: English model %s download failed: %s", variant, e)
                ev.clear()
        threading.Thread(target=work, name=f"vosk-download-en-{variant}", daemon=True).start()
    if variant != "small":
        return english_model(allow_download=False, variant="small")
    return None


def english_pass(wav: bytes | None, *, allow_download: bool = True, variant: str = "small") -> tuple[str, float] | None:
    """Decode ``wav`` (16 kHz mono) with the English model → (text, conf), or None if unavailable."""
    if not wav:
        return None
    model = english_model(allow_download=allow_download, variant=variant)
    if model is None:
        return None
    try:
        return transcribe_wav(model, wav)
    except Exception as e:
        log.warning("bilingual stt pass failed: %s", e)
        return None
