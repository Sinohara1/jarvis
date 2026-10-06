"""Microphone capture (sounddevice), VAD recording with live local STT, sentence-streamed speech
(local Piper → sounddevice, or edge-tts → Windows MCI), and UI beeps."""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import os
import queue
import re
import sys
import tempfile
import threading
import time
import wave

from .config import TMP_DIR, ensure_dir

log = logging.getLogger("jarvis")
IS_WIN = sys.platform == "win32"

SAMPLE_RATE = 16000
BLOCK = 1280  # 80 ms — matches the wake-word model frame


# ─── WAV helpers ─────────────────────────────────────────────────────────────

def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def rms_int16(chunk) -> float:
    """RMS of an int16 numpy array or bytes."""
    try:
        import numpy as np
        a = chunk if hasattr(chunk, "dtype") else np.frombuffer(chunk, dtype=np.int16)
        if a.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(a.astype(np.float32) ** 2)))
    except Exception:
        arr = array.array("h")
        arr.frombytes(bytes(chunk))
        if not arr:
            return 0.0
        return math.sqrt(sum(x * x for x in arr) / len(arr))


# ─── Mic hub ─────────────────────────────────────────────────────────────────

class MicHub:
    """One shared 16 kHz mono int16 input stream; consumers subscribe and get
    80 ms numpy chunks through their own queue."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[int, queue.Queue] = {}
        self._next = 1
        self._stream = None
        self.error: str | None = None

    def _callback(self, indata, frames, time_info, status) -> None:  # audio thread
        chunk = indata[:, 0].copy()
        for q in list(self._subs.values()):
            try:
                q.put_nowait(chunk)
            except queue.Full:
                pass

    def subscribe(self) -> tuple[int, queue.Queue]:
        with self._lock:
            sid = self._next
            self._next += 1
            q: queue.Queue = queue.Queue(maxsize=400)
            self._subs[sid] = q
            if self._stream is None:
                try:
                    import sounddevice as sd
                    self._stream = sd.InputStream(
                        samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=BLOCK, callback=self._callback,
                    )
                    self._stream.start()
                    self.error = None
                    log.info("mic stream started")
                except Exception as e:
                    self._stream = None
                    self.error = str(e)
                    self._subs.pop(sid, None)
                    log.error("mic open failed: %s", e)
                    raise
            return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)
            if not self._subs and self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
                log.info("mic stream stopped")

    def close(self) -> None:
        with self._lock:
            self._subs.clear()
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None


class Recording:
    """Collect mic audio until stop() (hold mode) or end-of-speech (auto).

    End of speech = `silence_sec` without voice after speech started (Silero VAD, energy fallback).
    Optional `stt` (StreamingSTT) is fed live so the transcript is ready right at the end."""

    def __init__(self, hub: MicHub, *, auto_stop: bool, max_sec: float = 30.0,
                 no_speech_timeout: float = 6.0, silence_sec: float = 0.6,
                 on_level=None, stt=None, use_silero: bool = True) -> None:
        self.hub = hub
        self.auto_stop = auto_stop
        self.max_sec = max_sec
        self.no_speech_timeout = no_speech_timeout
        self.silence_sec = silence_sec
        self.on_level = on_level
        self.stt = stt
        self.use_silero = use_silero
        self._stop = threading.Event()
        self.cancelled = False
        self.speech_detected = False
        self.t_speech_end: float | None = None  # monotonic time of the last voiced chunk
        self.t_end: float | None = None         # when recording stopped
        self.text = ""
        self.conf = 0.0
        self.stt_sec = 0.0
        self.vad_kind = ""

    def stop(self) -> None:
        self._stop.set()

    def cancel(self) -> None:
        self.cancelled = True
        self._stop.set()

    def run(self) -> bytes | None:
        """Blocking. Returns WAV bytes, or None if cancelled / no speech."""
        from .vad import EndpointDetector
        det = EndpointDetector(use_silero=self.use_silero)
        self.vad_kind = det.kind
        sid, q = self.hub.subscribe()
        chunks: list = []
        start = time.monotonic()
        last_voice = None
        voiced_run = 0
        stt_busy = 0.0
        try:
            while not self._stop.is_set():
                try:
                    c = q.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - start > self.max_sec:
                        break
                    continue
                now = time.monotonic()
                chunks.append(c)
                lvl = rms_int16(c)
                if self.on_level:
                    try:
                        self.on_level(min(1.0, lvl / 4000.0))
                    except Exception:
                        pass
                el = now - start
                if det.voiced(c, lvl, el):
                    voiced_run += 1
                    if voiced_run >= 2 or self.speech_detected:  # ≥160 ms: not a click / the beep
                        self.speech_detected = True
                        last_voice = now
                else:
                    voiced_run = 0
                if self.stt is not None:
                    t = time.monotonic()
                    try:
                        self.stt.feed(c)
                    except Exception as e:
                        log.warning("local stt feed failed: %s", e)
                        self.stt = None
                    stt_busy += time.monotonic() - t
                if el > self.max_sec:
                    break
                if self.auto_stop:
                    if not self.speech_detected and el > self.no_speech_timeout:
                        break
                    if self.speech_detected and last_voice and now - last_voice > self.silence_sec:
                        break
        finally:
            self.hub.unsubscribe(sid)
        self.t_end = time.monotonic()
        self.t_speech_end = last_voice if last_voice else self.t_end
        if self.cancelled or not chunks:
            return None
        if self.auto_stop and not self.speech_detected:
            return None
        import numpy as np
        pcm = np.concatenate(chunks).astype(np.int16)
        if pcm.size < SAMPLE_RATE * 0.3:
            return None
        if self.stt is not None and self.speech_detected:
            t = time.monotonic()
            try:
                self.text, self.conf = self.stt.final()
            except Exception as e:
                log.warning("local stt final failed: %s", e)
            self.stt_sec = time.monotonic() - t
            log.info("local stt: %r conf=%.2f (final %.0f ms, live feed %.2fs over %.1fs audio)",
                     self.text[:120], self.conf, self.stt_sec * 1000, stt_busy, pcm.size / SAMPLE_RATE)
        return pcm_to_wav(pcm.tobytes())


# ─── Text → sentences (for speaking a streamed reply as it arrives) ──────────

_SENT_END = re.compile(r"([.!?…]+[»\")]*)(\s+)")


class SentenceSplitter:
    """push(delta) → complete sentences ready to speak; flush() → the rest.
    Short fragments are glued to the next sentence; very long ones are cut at a comma.
    The FIRST piece is also cut at a clause boundary (", " ": " " — ") once it is long enough:
    the first sound comes sooner and its synthesis is shorter; the rest plays gaplessly after it."""

    def __init__(self, min_len: int = 12, max_len: int = 220, first_clause: int = 28) -> None:
        self.buf = ""
        self.min_len = min_len
        self.max_len = max_len
        self.first_clause = first_clause
        self.count = 0

    def push(self, delta: str) -> list[str]:
        self.buf += delta or ""
        out: list[str] = []
        while True:
            cut = None
            for m in _SENT_END.finditer(self.buf):
                end = m.end(1)
                head = self.buf[:end].strip()
                # first sentence may be short («Привет!») — speed matters most there
                if len(head) >= (2 if self.count == 0 else self.min_len) and not _is_abbrev(self.buf[:end]):
                    cut = (end, m.end())
                    break
            if cut is None and self.count == 0 and self.first_clause and len(self.buf) > self.first_clause + 2:
                m = _CLAUSE.search(self.buf, self.first_clause)
                if m:
                    cut = (m.start() + 1 if m.group(0).startswith((",", ";", ":")) else m.start(), m.end())
            if cut is None and "\n" in self.buf:
                i = self.buf.index("\n")
                if self.buf[:i].strip():
                    cut = (i, i + 1)
            if cut is None and len(self.buf) > self.max_len:
                i = max(self.buf.rfind(", ", 0, self.max_len), self.buf.rfind("; ", 0, self.max_len),
                        self.buf.rfind(" — ", 0, self.max_len))
                cut = (i + 1, i + 2) if i > 20 else (self.max_len, self.max_len)
            if cut is None:
                return out
            sent = self.buf[:cut[0]].strip()
            self.buf = self.buf[cut[1]:]
            if sent:
                out.append(sent)
                self.count += 1

    def flush(self) -> list[str]:
        rest = self.buf.strip()
        self.buf = ""
        if rest:
            self.count += 1
            return [rest]
        return []


_CLAUSE = re.compile(r"[,;:]\s+|\s+[—–]\s+")
_ABBREV = re.compile(r"(?:\b(?:т|т\.\s?е|т\.\s?к|т\.\s?д|т\.\s?п|т\.\s?н|др|пр|см|г|гг|ул|д|им|тыс|млн|млрд|руб|коп|мин|сек|стр|рис|напр)\.)$",
                     re.IGNORECASE)


def _is_abbrev(text: str) -> bool:
    t = text.rstrip()
    if re.search(r"\b[А-ЯA-Z]\.$", t):  # initials «А. С. Пушкин»
        return True
    return bool(_ABBREV.search(t))


def split_sentences(text: str) -> list[str]:
    sp = SentenceSplitter()
    return sp.push(text) + sp.flush()


# ─── TTS: local Piper (default) or edge-tts (online) → playback ──────────────

_MD_RE = re.compile(r"[*_`#>\[\]]+")


def speakable(text: str) -> str:
    t = _MD_RE.sub("", text or "")
    t = re.sub(r"https?://\S+", "ссылка", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:1200]


async def _edge_synth(text: str, voice: str, rate: int, path: str) -> None:
    import edge_tts
    rate_s = f"{'+' if rate >= 0 else ''}{int(rate)}%"
    comm = edge_tts.Communicate(text, voice, rate=rate_s)
    await comm.save(path)


def synth_to_file(text: str, voice: str, rate: int, path: str, attempts: int = 3) -> None:
    """edge-tts occasionally returns NoAudioReceived; retry a couple of times."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            asyncio.run(_edge_synth(text, voice, rate, path))
            if os.path.getsize(path) > 0:
                return
            last = RuntimeError("empty audio")
        except Exception as e:  # network hiccup / NoAudioReceived
            last = e
        time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"TTS failed: {last}")


class _MCI:
    def __init__(self) -> None:
        import ctypes
        self._send = ctypes.windll.winmm.mciSendStringW
        self._buf = ctypes.create_unicode_buffer(256)

    def cmd(self, s: str) -> str:
        err = self._send(s, self._buf, 255, 0)
        if err:
            raise RuntimeError(f"MCI error {err} for: {s.split(' ')[0]}")
        return self._buf.value


class VoiceTurn:
    """Timestamps of one voice exchange (monotonic) for the latency log / UI."""

    def __init__(self, source: str = "voice") -> None:
        self.source = source
        self.t: dict[str, float] = {}
        self.stt = ""          # "local" | "cloud" | "wake-local" | "whisper"
        self.lang = ""         # language detected by the recogniser (Whisper), "" = unknown
        self.fallback = ""     # Vosk text kept in case the Whisper pass fails (engine = whisper)
        self.model = ""
        self.cancelled = False
        self.reported = False

    def mark(self, name: str, at: float | None = None) -> None:
        if name not in self.t:
            self.t[name] = time.monotonic() if at is None else at

    def breakdown(self) -> dict:
        t = self.t
        out: dict = {"stt_mode": self.stt, "model": self.model}

        def d(a, b):
            return round(t[b] - t[a], 3) if a in t and b in t else None
        out["endpoint"] = d("speech_end", "rec_end")
        out["stt"] = d("rec_end", "text")
        out["llm_first"] = d("text", "llm_first")
        out["first_sentence"] = d("text", "sentence")
        out["tts"] = d("sentence", "synth")
        out["play"] = d("synth", "play")
        out["first_audio"] = d("speech_end", "play")
        out["text_to_audio"] = d("text", "play")
        return out


class _Clip:
    __slots__ = ("pcm", "sr", "path", "text")

    def __init__(self, text: str, pcm=None, sr: int = 0, path: str | None = None) -> None:
        self.text, self.pcm, self.sr, self.path = text, pcm, sr, path


class SpeechStream:
    """Sentences of one reply. feed() as text arrives, end() when done. Stale after Speaker.stop()."""

    def __init__(self, speaker: "Speaker", gen: int, turn: VoiceTurn | None) -> None:
        self.sp, self.gen, self.turn = speaker, gen, turn
        self.closed = False
        self.lang: str | None = None  # language of the last sentence (voice stays stable within a reply)

    def feed(self, text: str) -> None:
        self.sp._enqueue(self, text)

    def end(self) -> None:
        self.sp._end_stream(self)


class Speaker:
    """Two-stage pipeline: synth thread (Piper or edge-tts) → player thread (sounddevice / MCI).
    The next sentence is synthesized while the current one plays. stop() interrupts everything."""

    def __init__(self, get_settings, on_state=None, on_turn=None, piper=None, lang_pref=None,
                 on_voice_missing=None) -> None:
        self.get_settings = get_settings
        self.lang_pref = lang_pref or (lambda: "ru")  # callable → preferred language (answer language)
        self.on_voice_missing = on_voice_missing      # callable(piper key) — e.g. start the download
        self.on_state = on_state  # callable(bool speaking)
        self.on_turn = on_turn    # callable(VoiceTurn) when its first audio starts
        self.piper = piper
        self.silent = False       # tests: play zeros (device still opened, timings real)
        self.null_output = False  # tests without a sound card: no device, real-time sleep instead
        self._lock = threading.Lock()
        self._gen = 0
        self._pending = 0          # sentences queued/synthesizing/playing (current gen)
        self._open: set[int] = set()
        self._q_text: queue.Queue = queue.Queue()
        self._q_audio: queue.Queue = queue.Queue()
        self._stop_evt = threading.Event()
        self._busy = threading.Event()
        self._out = None
        self._out_sr = 0
        self._playing = False
        self.last_engine = ""
        self.last_voice = ""
        ensure_dir(TMP_DIR)
        threading.Thread(target=self._synth_loop, name="tts-synth", daemon=True).start()
        threading.Thread(target=self._play_loop, name="tts-play", daemon=True).start()

    @property
    def speaking(self) -> bool:
        return self._busy.is_set()

    @property
    def idle(self) -> bool:
        """Nothing queued, synthesizing or playing, and no reply stream open."""
        with self._lock:
            return self._pending == 0 and not self._open and not self._playing

    # ── public ──
    def open_stream(self, turn: VoiceTurn | None = None) -> SpeechStream:
        with self._lock:
            st = SpeechStream(self, self._gen, turn)
            self._open.add(id(st))
            return st

    def say(self, text: str, *, interrupt: bool = False, turn: VoiceTurn | None = None,
            lang: str | None = None) -> None:
        t = speakable(text)
        if not t:
            return
        if interrupt:
            self.stop()
        st = self.open_stream(turn)
        st.lang = lang
        for s in split_sentences(t):
            st.feed(s)
        st.end()

    def prewarm(self, sr: int = 22050) -> None:
        """Open the output device while the model is still thinking (saves ~50–150 ms on Windows)."""
        if not self.null_output and self.engine_for(self.get_settings()) == "piper":
            self._q_audio.put(("prewarm", sr))

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._open.clear()
            self._pending = 0
            for q in (self._q_text, self._q_audio):
                try:
                    while True:
                        item = q.get_nowait()
                        if item and len(item) > 2 and isinstance(item[2], _Clip) and item[2].path:
                            _rm(item[2].path)
                except queue.Empty:
                    pass
            busy = self._busy.is_set()
            playing = self._playing
        if busy:
            self._stop_evt.set()
            if not playing:  # waiting between sentences → nothing will report the end
                self._set_state(False)

    def shutdown(self) -> None:
        self.stop()
        self._q_text.put(None)
        self._q_audio.put(None)

    # ── internals ──
    def _enqueue(self, st: SpeechStream, text: str) -> None:
        t = speakable(text)
        if not t:
            return
        with self._lock:
            if st.gen != self._gen or st.closed:
                return
            self._pending += 1
        if st.turn:
            st.turn.mark("sentence")
        from .lang import detect
        try:
            lg = detect(t, prefer=st.lang or self.lang_pref())
        except Exception:
            lg = "ru"
        st.lang = lg
        self._q_text.put((st.gen, st, t, lg))

    def _end_stream(self, st: SpeechStream) -> None:
        with self._lock:
            st.closed = True
            self._open.discard(id(st))
            idle = self._pending == 0 and not self._open
        if idle and self._busy.is_set():
            self._set_state(False)

    def _done_one(self, gen: int) -> None:
        with self._lock:
            if gen == self._gen and self._pending > 0:
                self._pending -= 1
            idle = self._pending == 0 and not self._open
        if idle:
            self._close_out(drain=True)
            if self._busy.is_set():
                self._set_state(False)

    def _set_state(self, v: bool) -> None:
        if v == self._busy.is_set():
            return
        if v:
            self._busy.set()
        else:
            self._busy.clear()
        if self.on_state:
            try:
                self.on_state(v)
            except Exception:
                pass

    def piper_key(self, s: dict, lang: str | None = None) -> str:
        from .tts_local import voice_for_lang
        return voice_for_lang(s, lang or self.lang_pref() or "ru")

    def engine_for(self, s: dict, lang: str | None = None) -> str:
        if s.get("tts_engine", "piper") == "piper" and self.piper is not None \
                and self.piper.ready(self.piper_key(s, lang)):
            return "piper"
        return "edge"

    def synth_clip(self, text: str, s: dict, lang: str | None = None) -> _Clip:
        """Synthesize one sentence in the voice of its language; Piper ↔ edge-tts fall back to each other."""
        from .lang import edge_voice
        lang = lang or self.lang_pref() or "ru"
        key = self.piper_key(s, lang)
        piper_ok = self.piper is not None and self.piper.ready(key)
        if s.get("tts_engine", "piper") == "piper" and not piper_ok and key and self.on_voice_missing:
            try:
                self.on_voice_missing(key)  # this sentence goes through Edge, the next ones offline
            except Exception:
                pass
        eng = "piper" if (s.get("tts_engine", "piper") == "piper" and piper_ok) else "edge"
        order = [eng] + (["edge"] if eng == "piper" else (["piper"] if piper_ok else []))
        last: Exception | None = None
        for e in order:
            try:
                if e == "piper":
                    pcm, sr = self.piper.synth(text, key, int(s.get("tts_rate", 0)))
                    self.last_engine, self.last_voice = "piper", key
                    return _Clip(text, pcm=pcm, sr=sr)
                fd, path = tempfile.mkstemp(prefix="tts_", suffix=".mp3", dir=TMP_DIR)
                os.close(fd)
                try:
                    synth_to_file(text, edge_voice(s, lang), int(s.get("tts_rate", 0)), path)
                    self.last_voice = edge_voice(s, lang)
                except Exception:
                    _rm(path)
                    raise
                self.last_engine = "edge"
                return _Clip(text, path=path)
            except Exception as ex:
                last = ex
                log.warning("TTS %s failed: %s", e, ex)
        raise RuntimeError(f"TTS failed: {last}")

    def _synth_loop(self) -> None:
        while True:
            item = self._q_text.get()
            if item is None:
                return
            gen, st, text, lang = item
            # stay at most ~2 sentences ahead of playback (no wasted CPU on a reply that gets interrupted)
            while self._q_audio.qsize() >= 2 and gen == self._gen:
                time.sleep(0.02)
            if gen != self._gen:
                continue
            try:
                t0 = time.monotonic()
                clip = self.synth_clip(text, self.get_settings(), lang)
                if st.turn:
                    st.turn.mark("synth")
                log.debug("tts %s %.2fs: %s", self.last_engine, time.monotonic() - t0, text[:60])
            except Exception as e:
                log.warning("TTS failed: %s", e)
                self._done_one(gen)
                continue
            if gen != self._gen:
                if clip.path:
                    _rm(clip.path)
                continue
            self._q_audio.put((gen, st, clip))

    def _play_loop(self) -> None:
        mci = None
        while True:
            try:
                item = self._q_audio.get(timeout=1.0)
            except queue.Empty:
                with self._lock:
                    idle = self._pending == 0 and not self._open
                if idle:
                    self._close_out(drain=True)
                continue
            if item is None:
                self._close_out(drain=False)
                return
            if item[0] == "prewarm":
                try:
                    self._open_out(item[1])
                except Exception as e:
                    log.debug("prewarm failed: %s", e)
                continue
            gen, st, clip = item
            if gen != self._gen:
                if clip.path:
                    _rm(clip.path)
                continue
            self._stop_evt.clear()
            self._playing = True
            self._set_state(True)
            s = self.get_settings()
            vol = 0 if self.silent else int(s.get("tts_volume", 85))
            try:
                if clip.pcm is not None:
                    self._play_pcm(clip, vol, st)
                elif clip.path and IS_WIN:
                    if mci is None:
                        mci = _MCI()
                    self._close_out(drain=True)
                    self._play_mci(mci, clip.path, vol, st)
                elif st.turn:
                    st.turn.mark("play")
                    self._report(st.turn)
            except Exception as e:
                log.warning("playback failed: %s", e)
                self._close_out(drain=False)
            finally:
                self._playing = False
                if clip.path:
                    _rm(clip.path)
                if self._stop_evt.is_set():
                    self._close_out(drain=False)
                self._done_one(gen)

    def _report(self, turn: VoiceTurn | None) -> None:
        if turn is not None and not turn.reported:
            turn.reported = True
            if self.on_turn:
                try:
                    self.on_turn(turn)
                except Exception:
                    log.exception("on_turn failed")

    def _play_pcm(self, clip: _Clip, volume: int, st: SpeechStream) -> None:
        import numpy as np
        data = clip.pcm
        if self.null_output:
            if st.turn:
                st.turn.mark("play")
                self._report(st.turn)
            end = time.monotonic() + len(data) / float(clip.sr)
            while time.monotonic() < end and not self._stop_evt.is_set() and st.gen == self._gen:
                time.sleep(0.02)
            return
        if volume < 100:
            data = (data.astype(np.float32) * (max(0, volume) / 100.0)).astype(np.int16)
        if self._out is not None and self._out_sr != clip.sr:
            self._close_out(drain=True)
        if self._out is None:
            self._open_out(clip.sr)
            data = np.concatenate([np.zeros(int(clip.sr * 0.03), dtype=np.int16), data])  # device warm-up
        block = 1024
        first = True
        for i in range(0, len(data), block):
            if self._stop_evt.is_set() or st.gen != self._gen:
                self._stop_evt.set()
                return
            self._out.write(data[i:i + block])
            if first:
                first = False
                if st.turn:
                    st.turn.mark("play")
                    self._report(st.turn)

    def _open_out(self, sr: int) -> None:
        if self._out is not None and self._out_sr == sr:
            return
        self._close_out(drain=True)
        import sounddevice as sd
        out = sd.OutputStream(samplerate=sr, channels=1, dtype="int16", latency="low")
        out.start()
        self._out, self._out_sr = out, sr

    def _close_out(self, drain: bool) -> None:
        out = self._out
        if out is None:
            return
        self._out = None
        try:
            if drain:
                out.stop()   # waits for queued audio
            else:
                out.abort()  # interrupt now
            out.close()
        except Exception:
            pass

    def _play_mci(self, mci: _MCI, path: str, volume: int, st: SpeechStream) -> None:
        alias = "jarvis_tts"
        try:
            mci.cmd(f"close {alias}")
        except Exception:
            pass
        mci.cmd(f'open "{path}" type mpegvideo alias {alias}')
        try:
            try:
                mci.cmd(f"setaudio {alias} volume to {max(0, min(1000, volume * 10))}")
            except Exception:
                pass
            mci.cmd(f"play {alias}")
            if st.turn:
                st.turn.mark("play")
                self._report(st.turn)
            t0 = time.monotonic()
            while not self._stop_evt.is_set() and st.gen == self._gen:
                time.sleep(0.03)
                try:
                    mode = mci.cmd(f"status {alias} mode")
                except Exception:
                    break
                if mode != "playing" and time.monotonic() - t0 > 0.3:
                    break
                if time.monotonic() - t0 > 120:
                    break
        finally:
            try:
                mci.cmd(f"stop {alias}")
            except Exception:
                pass
            try:
                mci.cmd(f"close {alias}")
            except Exception:
                pass


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ─── Beeps ───────────────────────────────────────────────────────────────────

def _tone_wav(freqs: list[tuple[float, float]], volume: float = 0.25) -> bytes:
    rate = 22050
    samples = array.array("h")
    for f, dur in freqs:
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / (rate * 0.01), (n - i) / (rate * 0.03))
            samples.append(int(32767 * volume * env * math.sin(2 * math.pi * f * i / rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


_BEEPS = {
    "listen": [(660, 0.07), (880, 0.09)],
    "stop": [(880, 0.07), (590, 0.09)],
    "nudge": [(520, 0.12), (520, 0.12)],
    "phase": [(523, 0.12), (659, 0.12), (784, 0.16)],
}
_beep_paths: dict[str, str] = {}


def beep(kind: str) -> None:
    if not IS_WIN:
        return
    try:
        import winsound
        path = _beep_paths.get(kind)
        if not path or not os.path.isfile(path):
            ensure_dir(TMP_DIR)
            path = os.path.join(TMP_DIR, f"beep_{kind}.wav")
            with open(path, "wb") as f:
                f.write(_tone_wav(_BEEPS.get(kind, _BEEPS["listen"])))
            _beep_paths[kind] = path
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as e:
        log.debug("beep failed: %s", e)
