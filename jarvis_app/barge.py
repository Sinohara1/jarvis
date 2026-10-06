"""Barge-in: while the assistant is speaking, a new phrase stops her and is captured.

Headphones work best. The mic stays open only while she speaks; a short loud run
(not a click) cuts TTS immediately, then the rest of the phrase is recorded.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from .audio import SAMPLE_RATE, pcm_to_wav, rms_int16

log = logging.getLogger("jarvis.barge")


class BargeListener:
    def __init__(self, hub, on_phrase, on_cut=None, *, rms_gate: float = 1400.0, onset_sec: float = 0.4,
                 silence_sec: float = 0.6, max_sec: float = 18.0) -> None:
        self.hub = hub
        self.on_phrase = on_phrase  # (wav: bytes) -> None
        self.on_cut = on_cut
        self.rms_gate = rms_gate
        self.onset_sec = onset_sec
        self.silence_sec = silence_sec
        self.max_sec = max_sec
        self.armed = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cut = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="barge-in", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.armed.clear()

    def _run(self) -> None:
        sid = None
        q: queue.Queue | None = None
        try:
            while not self._stop.is_set():
                if not self.armed.is_set():
                    if sid is not None:
                        self.hub.unsubscribe(sid)
                        sid, q = None, None
                    self.armed.wait(0.25)
                    continue
                if sid is None:
                    try:
                        sid, q = self.hub.subscribe()
                    except Exception as e:
                        log.warning("barge mic unavailable: %s", e)
                        self._stop.wait(2.0)
                        continue
                    try:
                        while True:
                            q.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    c = q.get(timeout=0.3)
                except queue.Empty:
                    continue
                if not self.armed.is_set():
                    continue
                if rms_int16(c) < self.rms_gate:
                    continue
                wav = self._capture(q, c)
                if wav and self.on_phrase:
                    try:
                        self.on_phrase(wav)
                    except Exception:
                        log.exception("barge phrase failed")
        finally:
            if sid is not None:
                self.hub.unsubscribe(sid)

    def _capture(self, q: queue.Queue, first) -> bytes | None:
        """Keep reading until silence. Cut TTS once speech has held for onset_sec."""
        chunks = [first]
        start = time.monotonic()
        last_voice = start
        voiced = 1.0 / 50.0  # first chunk already loud; ~20 ms frames
        cut = False
        while not self._stop.is_set() and time.monotonic() - start < self.max_sec:
            try:
                c = q.get(timeout=0.2)
            except queue.Empty:
                if cut and time.monotonic() - last_voice > self.silence_sec:
                    break
                continue
            chunks.append(c)
            now = time.monotonic()
            if rms_int16(c) >= self.rms_gate * 0.72:
                voiced += 0.02
                last_voice = now
            if not cut and voiced >= self.onset_sec:
                cut = True
                self._cut.set()
                if self.on_cut:
                    try:
                        self.on_cut()
                    except Exception:
                        log.exception("barge cut failed")
            if cut and now - last_voice > self.silence_sec:
                break
        self._cut.clear()
        if not cut:
            return None
        import numpy as np
        pcm = np.concatenate(chunks).astype(np.int16)
        if pcm.size < SAMPLE_RATE * 0.35:
            return None
        return pcm_to_wav(pcm.tobytes())
