"""v1.6 conversation mode: after a voice reply the assistant keeps listening for follow-ups without the name.

Pure logic (session timer, junk filters, stop phrases, echo matching) is mic-free so it can be unit-tested;
``FollowListener`` turns the shared mic stream into finished utterances (Silero VAD gate, Vosk fed only
while someone talks, so silence costs ~1 % of a core).
"""

from __future__ import annotations

import difflib
import logging
import queue
import re
import threading
import time
from collections import deque

import numpy as np

log = logging.getLogger("jarvis")

# setting «Слушать после ответа»: key → seconds (None = always)
FOLLOW_MODES = ("off", "30s", "2m", "10m", "always")
FOLLOW_SECONDS: dict[str, int | None] = {"off": 0, "30s": 30, "2m": 120, "10m": 600, "always": None}
DEFAULT_FOLLOW = "2m"


def norm_follow(mode) -> str:
    m = str(mode or "").strip().lower()
    return m if m in FOLLOW_MODES else DEFAULT_FOLLOW


class FollowSession:
    """Conversation window. start()/touch() after each exchange resets the timer; hold() freezes it while
    the assistant thinks or speaks; end() closes it (voice «хватит», timeout, settings)."""

    def __init__(self, mode: str = DEFAULT_FOLLOW) -> None:
        self.mode = norm_follow(mode)
        self.until = 0.0          # monotonic deadline (timed modes)
        self.on = False
        self.held = False
        self.started_at = 0.0
        self.ended_reason = ""

    @property
    def always(self) -> bool:
        return self.mode == "always"

    @property
    def duration(self) -> int | None:
        return FOLLOW_SECONDS[self.mode]

    def configure(self, mode: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.mode = norm_follow(mode)
        if self.mode == "off":
            self.end("off")
        elif self.on and not self.always:
            self.until = now + float(self.duration or 0)

    def start(self, now: float | None = None) -> bool:
        """Open (or extend) the window. False when the setting is «выкл»."""
        now = time.monotonic() if now is None else now
        if self.mode == "off":
            return False
        if not self.on:
            self.started_at = now
        self.on = True
        self.ended_reason = ""
        if not self.always:
            self.until = now + float(self.duration or 0)
        return True

    touch = start

    def hold(self, on: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.held and not on and self.on and not self.always:
            self.until = max(self.until, now + float(self.duration or 0))  # timer restarts after her reply
        self.held = bool(on)

    def end(self, reason: str = "") -> bool:
        was = self.on
        self.on = False
        self.held = False
        self.until = 0.0
        self.ended_reason = reason
        return was

    def active(self, now: float | None = None) -> bool:
        if not self.on or self.mode == "off":
            return False
        if self.always or self.held:
            return True
        now = time.monotonic() if now is None else now
        if now >= self.until:
            self.end("timeout")
            return False
        return True

    def remaining(self, now: float | None = None) -> float | None:
        """Seconds left; None = no limit («всегда»); 0 = closed."""
        if not self.active(now):
            return 0.0
        if self.always:
            return None
        now = time.monotonic() if now is None else now
        if self.held:
            return float(self.duration or 0)
        return max(0.0, self.until - now)

    def snapshot(self, now: float | None = None) -> dict:
        act = self.active(now)
        rem = self.remaining(now) if act else 0.0
        return {"active": act, "mode": self.mode, "always": self.always,
                "remaining": None if rem is None else int(round(rem))}


def fmt_left(sec: float | None) -> str:
    if sec is None:
        return "всегда"
    s = max(0, int(round(sec)))
    return f"{s // 60}:{s % 60:02d}"


# ─── text helpers ─────────────────────────────────────────────────────────────

def norm_text(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[^\w\s']+", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def _strip_name(t: str, assistant_name: str = "") -> str:
    names = {"джарвис", "jarvis", "джервис"}
    n = norm_text(assistant_name)
    if n:
        names.add(n)
    words = t.split()
    while words and (words[0] in names or words[0] in ("эй", "hey", "ну", "окей", "ok", "okay")):
        words = words[1:]
    return " ".join(words)


_STOP = {
    "хватит", "хватит слушать", "стоп", "стоп слушать", "стоп слушай", "пока", "пока пока", "всем пока",
    "перестань слушать", "не слушай", "можешь не слушать", "больше не слушай", "отбой", "все спасибо",
    "спасибо все", "спасибо это все", "это все", "все пока", "спасибо пока", "до свидания", "хватит пока",
    "закончили", "конец связи", "отключись", "не слушай меня", "можешь отдыхать", "отдыхай",
    "stop", "stop listening", "bye", "bye bye", "goodbye", "good bye", "that's all", "thats all", "that's it",
    "thats it", "thanks that's all", "thank you that's all", "enough", "stop it", "over and out", "you can rest",
}


def is_stop_phrase(text: str, assistant_name: str = "") -> bool:
    """«хватит», «пока», «стоп слушать», «Джарвис, хватит», «stop listening» … — ends the conversation window.
    Only whole short phrases count: «хватит играть в игры» is a normal request."""
    t = _strip_name(norm_text(text), assistant_name)
    if t in _STOP:
        return True
    tail = re.sub(r"\s*\b(пожалуйста|please|уже|ладно|ну|okay|ok|окей|так|now|then)$", "", t).strip()
    head = re.sub(r"^(ладно|ну|ok|окей|okay|так|все|well|alright)\s+", "", t).strip()
    return tail in _STOP or head in _STOP


_FILLERS = {"э", "ээ", "эм", "эмм", "мм", "ммм", "м", "а", "ах", "ох", "ой", "угу", "ага", "ну", "хм", "хмм",
            "ха", "хаха", "аха", "о", "у", "и", "да да", "uh", "um", "umm", "hmm", "hm", "mm", "mhm", "ah", "oh",
            "huh", "ha", "haha", "the", "a", "i", "so", "well", "yeah yeah"}
_SHORT_OK = {"да", "нет", "ага да", "давай", "конечно", "yes", "no", "yeah", "nope", "sure", "ok", "okay",
             "окей", "стоп", "пауза", "дальше", "громче", "тише", "next", "pause", "louder", "quieter"}


def junk_reason(text: str, conf: float, dur_sec: float, *, engine: str = "vosk", always: bool = False) -> str:
    """Why an overheard phrase should NOT go to the model ('' = keep it).
    engine: "vosk" (word confidence) | "whisper" (segment confidence)."""
    t = norm_text(text)
    letters = re.sub(r"[\W\d_]+", "", t)
    if len(letters) < 2:
        return "пусто"
    if dur_sec < 0.35:
        return "слишком коротко"
    if t in _FILLERS:
        return "междометие"
    min_conf = (0.55 if always else 0.45) if engine == "whisper" else (0.7 if always else 0.6)
    if conf < min_conf:
        return f"неуверенно ({conf:.2f})"
    words = t.split()
    if always and len(words) == 1 and t not in _SHORT_OK and len(letters) < 4:
        return "одно короткое слово"
    if len(set(words)) == 1 and len(words) >= 3:
        return "повтор одного слова"  # «да да да», «ля ля ля» — music / noise
    return ""


def _tokens(t: str) -> list[str]:
    return [w for w in norm_text(t).split() if len(w) >= 2]


def _tok_match(a: str, toks: list[str]) -> bool:
    if a in toks:
        return True
    return any(difflib.SequenceMatcher(None, a, b).ratio() >= 0.8 for b in toks if abs(len(a) - len(b)) <= 3)


def echo_match(text: str, candidates, threshold: float = 0.7) -> bool:
    """Does a mic phrase repeat what the PC (or her own voice) just played? Mic STT and loopback STT
    differ in details, so it is fuzzy: whole-string similarity or ≥70 % of the words found nearby."""
    mt = norm_text(text)
    toks = _tokens(mt)
    if not toks:
        return False
    for c in candidates or ():
        ct = norm_text(c)
        if not ct:
            continue
        if difflib.SequenceMatcher(None, mt, ct).ratio() >= max(threshold, 0.72):
            return True
        if len(toks) < 2:
            # a single word only counts as an echo when the PC said practically just that
            if len(_tokens(ct)) <= 2 and _tok_match(toks[0], _tokens(ct)):
                return True
            continue
        if mt in ct and len(mt) >= 8:
            return True
        ctoks = _tokens(ct)
        hit = sum(1 for w in toks if _tok_match(w, ctoks))
        if hit >= 2 and hit / len(toks) >= threshold:
            return True
    return False


# ─── utterance segmentation (mic or PC loopback) ──────────────────────────────

class UtteranceSegmenter:
    """Feed 16 kHz int16 chunks (any size; 80 ms works best). Returns a finished utterance dict
    {pcm, dur, speech_sec, t_end, text, conf} or None. Silero VAD (energy fallback) with hysteresis;
    an optional streaming recognizer is fed only inside an utterance."""

    def __init__(self, *, hang_sec: float = 0.7, max_sec: float = 15.0, min_speech: float = 0.3,
                 preroll: int = 4, stt_factory=None, use_silero: bool = True, min_level: float = 120.0) -> None:
        from .vad import EndpointDetector
        self._det_cls = EndpointDetector
        self.use_silero = use_silero
        self.hang_sec = hang_sec
        self.max_sec = max_sec
        self.min_speech = min_speech
        self.stt_factory = stt_factory
        self.min_level = min_level
        self.pre: deque = deque(maxlen=preroll)
        self.reset()

    def reset(self) -> None:
        self.det = self._det_cls(use_silero=self.use_silero)
        self.pre.clear()
        self.buf: list = []
        self.in_utt = False
        self.voiced_run = 0
        self.t = 0.0             # stream time (s) since reset
        self.t_start = 0.0
        self.last_voice = 0.0
        self.voiced_sec = 0.0
        self.stt = None

    def _rms(self, a: np.ndarray) -> float:
        return float(np.sqrt(np.mean(a.astype(np.float32) ** 2))) if a.size else 0.0

    def feed(self, chunk) -> dict | None:
        a = np.asarray(chunk, dtype=np.int16).reshape(-1)
        dt = a.size / 16000.0
        self.t += dt
        lvl = self._rms(a)
        v = bool(self.det.voiced(a, lvl, self.t)) and lvl > self.min_level
        if not self.in_utt:
            self.pre.append(a)
            self.voiced_run = self.voiced_run + 1 if v else 0
            if self.voiced_run >= 2:  # ≥160 ms of voice: not a click
                self.in_utt = True
                self.buf = list(self.pre)
                self.pre.clear()
                self.t_start = self.t - dt * len(self.buf)
                self.last_voice = self.t
                self.voiced_sec = dt * 2
                if self.stt_factory is not None:
                    try:
                        self.stt = self.stt_factory()
                        for c in self.buf if self.stt is not None else ():
                            self.stt.feed(c)
                    except Exception as e:
                        log.debug("segmenter stt failed: %s", e)
                        self.stt = None
            return None
        self.buf.append(a)
        if self.stt is not None:
            try:
                self.stt.feed(a)
            except Exception as e:
                log.debug("segmenter stt feed failed: %s", e)
                self.stt = None
        if v:
            self.last_voice = self.t
            self.voiced_sec += dt
        if self.t - self.last_voice > self.hang_sec or self.t - self.t_start > self.max_sec:
            return self._finish()
        return None

    def _finish(self) -> dict | None:
        pcm = np.concatenate(self.buf) if self.buf else np.zeros(0, dtype=np.int16)
        text, conf = "", 0.0
        if self.stt is not None:
            try:
                text, conf = self.stt.final()
            except Exception as e:
                log.debug("segmenter stt final failed: %s", e)
        speech = self.voiced_sec
        out = {"pcm": pcm, "dur": pcm.size / 16000.0, "speech_sec": round(speech, 2),
               "t_end": time.monotonic(), "text": text, "conf": float(conf or 0.0)}
        self.in_utt = False
        self.voiced_run = 0
        self.buf = []
        self.stt = None
        if getattr(self.det, "vad", None) is not None:
            self.det = self._det_cls(use_silero=self.use_silero)  # fresh Silero state per utterance
        # (the energy fallback keeps its noise floor: re-calibrating on ongoing speech would make it deaf)
        if speech < self.min_speech:
            return None
        return out


class FollowListener:
    """MicHub → UtteranceSegmenter → on_utterance(utt dict). Listens only while ``enabled`` is set
    (the core clears it while the assistant speaks / thinks / records, so it never hears itself)."""

    def __init__(self, hub, on_utterance, *, hang_sec: float = 0.7, stt_factory=None) -> None:
        self.hub = hub
        self.on_utterance = on_utterance
        self.hang_sec = hang_sec
        self.stt_factory = stt_factory
        self.enabled = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.heard = 0

    def set_hang(self, sec: float) -> None:
        self.hang_sec = max(0.5, float(sec) + 0.1)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="follow-listen", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.enabled.clear()

    def _run(self) -> None:
        sid = None
        q: queue.Queue | None = None
        seg: UtteranceSegmenter | None = None
        idle_since = time.monotonic()
        try:
            while not self._stop.is_set():
                if not self.enabled.is_set():
                    if seg is not None:
                        seg = None  # drop a half-heard phrase (she started speaking)
                    if sid is not None and time.monotonic() - idle_since > 4.0:
                        self.hub.unsubscribe(sid)
                        sid, q = None, None
                    if q is not None:
                        try:
                            while True:
                                q.get_nowait()
                        except queue.Empty:
                            pass
                    self.enabled.wait(0.25)
                    continue
                idle_since = time.monotonic()
                if sid is None:
                    try:
                        sid, q = self.hub.subscribe()
                        self.error = None
                    except Exception as e:
                        self.error = str(e)
                        log.warning("follow listener: mic unavailable: %s", e)
                        self._stop.wait(3.0)
                        continue
                if seg is None:
                    seg = UtteranceSegmenter(hang_sec=self.hang_sec, max_sec=15.0, stt_factory=self.stt_factory)
                    try:
                        while True:
                            q.get_nowait()  # audio queued while disabled is stale
                    except queue.Empty:
                        pass
                try:
                    c = q.get(timeout=0.3)
                except queue.Empty:
                    continue
                if not self.enabled.is_set():
                    continue
                utt = seg.feed(c)
                if utt is not None and self.enabled.is_set():
                    self.heard += 1
                    try:
                        self.on_utterance(utt)
                    except Exception:
                        log.exception("follow utterance handler failed")
        finally:
            if sid is not None:
                self.hub.unsubscribe(sid)
