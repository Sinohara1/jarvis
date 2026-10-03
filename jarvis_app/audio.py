"""Microphone capture (sounddevice), simple VAD recording, edge-tts speech
output played via Windows MCI (no console windows), and UI beeps."""

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
    """Collect mic audio until stop() (hold mode) or VAD end-of-speech (auto)."""

    def __init__(self, hub: MicHub, *, auto_stop: bool, max_sec: float = 30.0,
                 no_speech_timeout: float = 6.0, silence_sec: float = 1.2,
                 on_level=None) -> None:
        self.hub = hub
        self.auto_stop = auto_stop
        self.max_sec = max_sec
        self.no_speech_timeout = no_speech_timeout
        self.silence_sec = silence_sec
        self.on_level = on_level
        self._stop = threading.Event()
        self.cancelled = False
        self.speech_detected = False

    def stop(self) -> None:
        self._stop.set()

    def cancel(self) -> None:
        self.cancelled = True
        self._stop.set()

    def run(self) -> bytes | None:
        """Blocking. Returns WAV bytes, or None if cancelled / no speech."""
        sid, q = self.hub.subscribe()
        chunks: list = []
        start = time.monotonic()
        noise = None
        last_voice = None
        try:
            while not self._stop.is_set():
                try:
                    c = q.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - start > self.max_sec:
                        break
                    continue
                chunks.append(c)
                lvl = rms_int16(c)
                if self.on_level:
                    try:
                        self.on_level(min(1.0, lvl / 4000.0))
                    except Exception:
                        pass
                now = time.monotonic()
                el = now - start
                if noise is None or el < 0.4:
                    noise = lvl if noise is None else (noise * 0.7 + lvl * 0.3)
                thr = max(450.0, (noise or 0) * 2.5)
                if lvl > thr and el > 0.15:
                    self.speech_detected = True
                    last_voice = now
                if el > self.max_sec:
                    break
                if self.auto_stop:
                    if not self.speech_detected and el > self.no_speech_timeout:
                        break
                    if self.speech_detected and last_voice and now - last_voice > self.silence_sec:
                        break
        finally:
            self.hub.unsubscribe(sid)
        if self.cancelled or not chunks:
            return None
        if self.auto_stop and not self.speech_detected:
            return None
        import numpy as np
        pcm = np.concatenate(chunks).astype(np.int16)
        if pcm.size < SAMPLE_RATE * 0.3:
            return None
        return pcm_to_wav(pcm.tobytes())


# ─── TTS (edge-tts) + MCI playback ───────────────────────────────────────────

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


class Speaker:
    """Queue of utterances spoken on a dedicated thread. stop() interrupts."""

    def __init__(self, get_settings, on_state=None) -> None:
        self.get_settings = get_settings
        self.on_state = on_state  # callable(bool speaking)
        self._q: queue.Queue = queue.Queue()
        self._stop_flag = threading.Event()
        self._busy = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)
        self._thread.start()
        ensure_dir(TMP_DIR)

    @property
    def speaking(self) -> bool:
        return self._busy.is_set()

    def say(self, text: str, *, interrupt: bool = False) -> None:
        t = speakable(text)
        if not t:
            return
        if interrupt:
            self.stop()
        self._q.put(t)

    def stop(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        if self._busy.is_set():
            self._stop_flag.set()

    def shutdown(self) -> None:
        self.stop()
        self._q.put(None)

    def _set_state(self, v: bool) -> None:
        if v:
            self._busy.set()
        else:
            self._busy.clear()
        if self.on_state:
            try:
                self.on_state(v)
            except Exception:
                pass

    def _run(self) -> None:
        mci = None
        while True:
            text = self._q.get()
            if text is None:
                return
            self._stop_flag.clear()
            s = self.get_settings()
            fd, path = tempfile.mkstemp(prefix="tts_", suffix=".mp3", dir=TMP_DIR)
            os.close(fd)
            try:
                self._set_state(True)
                synth_to_file(text, s.get("voice", "ru-RU-DmitryNeural"),
                              int(s.get("tts_rate", 0)), path)
                if self._stop_flag.is_set():
                    continue
                if IS_WIN:
                    if mci is None:
                        mci = _MCI()
                    self._play_mci(mci, path, int(s.get("tts_volume", 85)))
            except Exception as e:
                log.warning("TTS failed: %s", e)
            finally:
                if self._q.empty():
                    self._set_state(False)
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _play_mci(self, mci: _MCI, path: str, volume: int) -> None:
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
            t0 = time.monotonic()
            while not self._stop_flag.is_set():
                time.sleep(0.05)
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
