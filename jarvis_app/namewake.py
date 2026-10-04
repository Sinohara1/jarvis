"""Wake word = the assistant's configured name (v1.2), fully offline via Vosk.

Uses the small Russian Vosk model (~45 MB download, once, into %LOCALAPPDATA%\\Jarvis\\models).
Why free recognition instead of a restricted grammar: with a grammar of just [name, "[unk]"]
Vosk forces similar speech onto the name («Алиса, …» → «пятница»), while the full small
model transcribes ordinary speech correctly, so a phrase triggers only when it *starts* with
the name (optionally «эй/окей/слушай …»). «Сегодня пятница» does not trigger «Пятница».

CPU: an energy gate feeds the recognizer only while someone is talking (+ short hangover),
so silence costs ~nothing. Shares the app's single MicHub stream.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import zipfile
from collections import deque

import numpy as np

from .audio import SAMPLE_RATE, pcm_to_wav
from .config import DATA_DIR, MODELS_DIR, ensure_dir

log = logging.getLogger("jarvis")

VOSK_MODEL_NAME = "vosk-model-small-ru-0.22"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"
VOSK_MODEL_SIZE = 46_236_750  # bytes, used for progress when the server sends no length
USER_MODELS_DIR = os.path.join(DATA_DIR, "models")

FILLERS = {"эй", "хей", "хэй", "окей", "ок", "слушай", "привет", "алло", "ну", "а", "так", "ой"}
_LAT = [("sch", "щ"), ("ai", "ай"), ("ay", "ай"), ("ei", "ей"), ("ey", "ей"), ("oi", "ой"), ("oy", "ой"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ya", "я"), ("yu", "ю"),
        ("yo", "ё"), ("ee", "и"), ("oo", "у"), ("ph", "ф"), ("th", "т"), ("j", "дж"), ("a", "а"), ("b", "б"),
        ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"), ("k", "к"), ("l", "л"),
        ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
        ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "й"), ("z", "з")]
KNOWN_LATIN = {"airi": "айри", "jarvis": "джарвис", "friday": "фрайдей", "alexa": "алекса", "siri": "сири", "max": "макс",
               "jack": "джек", "alice": "алиса", "computer": "компьютер", "robot": "робот"}


# ─── names ───────────────────────────────────────────────────────────────────

def norm_word(w: str) -> str:
    return re.sub(r"[^a-zа-я0-9 ]+", "", (w or "").lower().replace("ё", "е")).strip()


def lat_to_cyr(word: str) -> str:
    w = word.lower()
    if w in KNOWN_LATIN:
        return KNOWN_LATIN[w]
    out, i = "", 0
    while i < len(w):
        for a, b in _LAT:
            if w.startswith(a, i):
                out += b
                i += len(a)
                break
        else:
            out += w[i]
            i += 1
    return out.replace("ё", "е")


def name_variants(name: str) -> list[str]:
    """Spoken forms of the assistant name the Russian model can output."""
    base = norm_word(name)
    out: list[str] = []
    if base:
        words = [lat_to_cyr(w) if re.search(r"[a-z]", w) else w for w in base.split()]
        out.append(" ".join(words))
        if re.search(r"[a-z]", base):  # also the letter-by-letter reading: Airi → «аири»
            out.append(" ".join(re.sub(r"й$", "и", w) for w in words))
    if base in ("джарвис", "jarvis") or not out:
        out += ["джарвис", "джервис"]
    return list(dict.fromkeys(v for v in out if v))


def similarity(a: str, b: str) -> float:
    a, b = a.replace(" ", ""), b.replace(" ", "")
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def need_score(threshold: float) -> float:
    """threshold 0.1..0.95 (lower = more eager) → minimum match score."""
    return 0.55 + 0.4 * max(0.0, min(1.0, float(threshold)))


def match_wake(words: list[dict], variants: list[str], threshold: float = 0.5) -> dict | None:
    """words: Vosk result items {word, conf, start, end}. The utterance must START with the name
    (after at most two fillers like «эй»). Returns {score, end, rest} or None."""
    toks = [dict(w, word=norm_word(w.get("word", ""))) for w in words if norm_word(w.get("word", ""))]
    i = 0
    while i < len(toks) and i < 2 and toks[i]["word"] in FILLERS:
        i += 1
    if i >= len(toks):
        return None
    best: dict | None = None
    for v in variants:
        k = len(v.split())
        for span in sorted({k, k + 1, max(1, k - 1)}):
            seg = toks[i:i + span]
            if len(seg) < span:
                continue
            cand = "".join(t["word"] for t in seg)
            conf = min(float(t.get("conf", 1.0)) for t in seg)
            score = similarity(cand, v) * (0.8 + 0.2 * conf)
            if best is None or score > best["score"]:
                best = {"score": round(score, 3), "end": float(seg[-1].get("end", 0.0)), "rest": toks[i + span:],
                        "heard": " ".join(t["word"] for t in seg), "variant": v}
    if best and best["score"] >= need_score(threshold):
        return best
    return None


# ─── model files ─────────────────────────────────────────────────────────────

def _valid_model(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "am", "final.mdl")) and os.path.isdir(os.path.join(path, "graph"))


def find_model() -> str | None:
    for root in (USER_MODELS_DIR, MODELS_DIR):
        p = os.path.join(root, VOSK_MODEL_NAME)
        if _valid_model(p):
            return p
    return None


_dl_lock = threading.Lock()


def download_model(on_progress=None, url: str = VOSK_MODEL_URL, dest_root: str = USER_MODELS_DIR) -> str:
    """Download + unpack the Vosk model once. on_progress(done, total). Returns the model dir."""
    import requests
    with _dl_lock:
        final = os.path.join(dest_root, VOSK_MODEL_NAME)
        if _valid_model(final):
            return final
        ensure_dir(dest_root)
        tmpdir = tempfile.mkdtemp(prefix="vosk_", dir=dest_root)
        try:
            zpath = os.path.join(tmpdir, "model.zip")
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0) or VOSK_MODEL_SIZE
                done, last = 0, 0.0
                with open(zpath, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        if on_progress and time.monotonic() - last > 0.3:
                            last = time.monotonic()
                            on_progress(done, total)
            if on_progress:
                on_progress(done, total)
            with zipfile.ZipFile(zpath) as z:
                bad = z.testzip()
                if bad:
                    raise RuntimeError(f"архив модели повреждён ({bad})")
                for m in z.namelist():
                    if m.startswith("/") or ".." in m.split("/"):
                        raise RuntimeError("подозрительный путь в архиве")
                z.extractall(tmpdir)
            src = os.path.join(tmpdir, VOSK_MODEL_NAME)
            if not _valid_model(src):
                raise RuntimeError("в архиве нет модели")
            if os.path.exists(final):
                shutil.rmtree(final, ignore_errors=True)
            os.replace(src, final)
            log.info("vosk model installed: %s", final)
            return final
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ─── spotting ────────────────────────────────────────────────────────────────

def _rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a.astype(np.float32) ** 2))) if a.size else 0.0


class NameSpotter:
    """Feed 16 kHz int16 chunks; returns a detection dict when an utterance starts with the name.
    Mic-free so it can be tested with WAV files."""

    PREROLL = 4        # chunks (~320 ms) fed when speech starts
    HANG = 9           # chunks (~0.7 s) of trailing silence → utterance is final (was 14 ≈ 1.1 s in 1.2)
    MIN_LEVEL = 320.0
    KEEP_SEC = 25.0

    def __init__(self, model, variants: list[str], threshold: float = 0.5) -> None:
        import vosk
        self._vosk = vosk
        self.model = model
        self.variants = variants
        self.threshold = threshold
        self.rec = None
        self.reset()

    def reset(self) -> None:
        # a fresh recognizer (not Reset()) so word timestamps restart at 0 together with our buffer
        self.rec = self._vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
        self.rec.SetWords(True)
        self.noise: float | None = None
        self.hang = 0
        self.active = False
        self.pre: deque = deque(maxlen=self.PREROLL)
        self.audio = np.zeros(0, dtype=np.int16)   # everything fed since reset (trimmed)
        self.audio_base = 0                          # sample index of audio[0] in recognizer time
        self.fed = 0

    def _push(self, chunk: np.ndarray) -> dict | None:
        self.audio = np.concatenate([self.audio, chunk])
        self.fed += chunk.size
        over = self.audio.size - int(self.KEEP_SEC * SAMPLE_RATE)
        if over > 0:
            self.audio = self.audio[over:]
            self.audio_base += over
        if self.rec.AcceptWaveform(chunk.tobytes()):
            return self._handle(self.rec.Result())
        return None

    def feed(self, chunk: np.ndarray) -> dict | None:
        chunk = np.asarray(chunk, dtype=np.int16)
        lvl = _rms(chunk)
        if self.noise is None:
            self.noise = lvl
        thr = max(self.MIN_LEVEL, self.noise * 2.5)
        voiced = lvl > thr
        if not voiced:
            self.noise = self.noise * 0.97 + lvl * 0.03
        out = None
        if voiced:
            self.hang = self.HANG
            if not self.active:
                self.active = True
                for c in self.pre:
                    out = self._push(c) or out
                self.pre.clear()
            out = self._push(chunk) or out
        elif self.active:
            out = self._push(chunk) or out
            self.hang -= 1
            if self.hang <= 0:
                self.active = False
                out = self._handle(self.rec.FinalResult()) or out
        else:
            self.pre.append(chunk)
        return out

    def _handle(self, raw: str) -> dict | None:
        try:
            res = json.loads(raw)
        except ValueError:
            return None
        words = res.get("result") or []
        if not words:
            return None
        m = match_wake(words, self.variants, self.threshold)
        if not m:
            return None
        rest = [w for w in m["rest"] if w["word"] not in FILLERS]
        req = None
        if rest:
            a = int((m["end"] + 0.05) * SAMPLE_RATE) - self.audio_base
            b = int((float(rest[-1].get("end", 0.0)) + 0.35) * SAMPLE_RATE) - self.audio_base
            a, b = max(0, a), min(self.audio.size, max(b, a))
            if b - a >= int(0.35 * SAMPLE_RATE):
                req = pcm_to_wav(self.audio[a:b].tobytes())
        last_end = float((rest or [{"end": m["end"]}])[-1].get("end", m["end"]))
        from .stt_local import words_conf, words_text
        return {"score": m["score"], "heard": m["heard"], "request_wav": req,
                "rest_text": " ".join(w["word"] for w in rest), "request_text": words_text(rest),
                "request_conf": round(words_conf(rest), 3),
                "lag": max(0.0, self.fed / SAMPLE_RATE - last_end)}  # seconds since the last word ended


class NameWakeListener:
    """Background thread: MicHub → NameSpotter → on_detect(request_wav or None, detection dict).
    Same interface as WakeWordListener (suspended / start / stop) plus set_name()."""

    def __init__(self, hub, on_detect, name: str, threshold: float = 0.5, model_path: str | None = None,
                 debounce: float = 2.0, hang_sec: float | None = None) -> None:
        self.hub = hub
        self.on_detect = on_detect
        self.threshold = threshold
        self.model_path = model_path
        self.debounce = debounce
        self.variants = name_variants(name)
        self.suspended = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._spotter: NameSpotter | None = None
        self.error: str | None = None
        self.ready = threading.Event()
        self.hang_sec = hang_sec

    def set_hang(self, sec: float) -> None:
        self.hang_sec = sec
        if self._spotter is not None:
            self._spotter.HANG = max(5, int(round(sec / 0.08)) + 1)

    def set_name(self, name: str) -> None:
        self.variants = name_variants(name)
        if self._spotter is not None:
            self._spotter.variants = self.variants
        log.info("name wake: now listening for %s", self.variants)

    def set_threshold(self, thr: float) -> None:
        self.threshold = thr
        if self._spotter is not None:
            self._spotter.threshold = thr

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="namewake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            from .stt_local import get_model
            path = self.model_path or find_model()
            if not path:
                raise RuntimeError("модель распознавания не скачана")
            model = get_model(path)  # shared with local dictation
            self._spotter = NameSpotter(model, self.variants, self.threshold)
            if self.hang_sec:
                self._spotter.HANG = max(5, int(round(self.hang_sec / 0.08)) + 1)
            sid, q = self.hub.subscribe()
        except Exception as e:
            self.error = str(e)
            log.error("name wake init failed: %s", e)
            self.ready.set()
            return
        self.ready.set()
        log.info("name wake listening for %s (threshold %.2f)", self.variants, self.threshold)
        last = 0.0
        was_suspended = False
        sp = self._spotter
        try:
            while not self._stop.is_set():
                try:
                    c = q.get(timeout=0.3)
                except queue.Empty:
                    continue
                if self.suspended.is_set():
                    was_suspended = True
                    continue
                if was_suspended:
                    sp.reset()
                    was_suspended = False
                    while not q.empty():  # drop audio queued while we were busy
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            break
                det = sp.feed(c)
                now = time.monotonic()
                if det and now - last > self.debounce:
                    last = now
                    log.info("name wake: «%s» score %.2f%s", det["heard"], det["score"],
                             " + request in the same breath" if det["request_wav"] else "")
                    sp.reset()
                    det["t_speech_end"] = now - det.get("lag", 0.0)
                    try:
                        self.on_detect(det["request_wav"], det)
                    except Exception:
                        log.exception("name wake handler failed")
        finally:
            self.hub.unsubscribe(sid)
