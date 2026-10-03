"""Offline "hey Jarvis" wake word using openWakeWord's ONNX models directly
(melspectrogram → speech embedding → hey_jarvis classifier) via onnxruntime.

Re-implements openwakeword's streaming feature pipeline without its heavy
dependencies (scipy / scikit-learn / tqdm), which keeps the exe small.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time

import numpy as np

from .config import MODELS_DIR

log = logging.getLogger("jarvis")

CHUNK = 1280  # 80 ms @ 16 kHz


class WakeWordModel:
    def __init__(self, model_dir: str = MODELS_DIR, model_name: str = "hey_jarvis_v0.1.onnx") -> None:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 1
        prov = ["CPUExecutionProvider"]
        self.mel = ort.InferenceSession(os.path.join(model_dir, "melspectrogram.onnx"), so, providers=prov)
        self.emb = ort.InferenceSession(os.path.join(model_dir, "embedding_model.onnx"), so, providers=prov)
        self.cls = ort.InferenceSession(os.path.join(model_dir, model_name), so, providers=prov)
        self.cls_input = self.cls.get_inputs()[0].name
        self.n_frames = int(self.cls.get_inputs()[0].shape[1] or 16)
        self.reset()

    def _melspec(self, x: np.ndarray) -> np.ndarray:
        x = x.astype(np.float32)[None, :]
        out = self.mel.run(None, {"input": x})[0]
        return np.squeeze(out) / 10.0 + 2.0

    def _embed(self, windows: np.ndarray) -> np.ndarray:
        return self.emb.run(None, {"input_1": windows.astype(np.float32)})[0].squeeze()

    def reset(self) -> None:
        self.raw = np.zeros(0, dtype=np.int16)
        self.melbuf = np.ones((76, 32), dtype=np.float32)
        noise = np.random.randint(-1000, 1000, 16000 * 4).astype(np.int16)
        spec = self._melspec(noise)
        wins = [spec[i:i + 76] for i in range(0, spec.shape[0], 8) if spec[i:i + 76].shape[0] == 76]
        self.feat = self._embed(np.expand_dims(np.array(wins), -1))
        self.pending = np.zeros(0, dtype=np.int16)

    def process(self, audio: np.ndarray) -> float:
        """Feed int16 audio; returns the latest score in [0, 1]."""
        self.pending = np.concatenate([self.pending, audio.astype(np.int16)])
        score = 0.0
        while self.pending.shape[0] >= CHUNK:
            chunk, self.pending = self.pending[:CHUNK], self.pending[CHUNK:]
            self.raw = np.concatenate([self.raw, chunk])[-(CHUNK + 480):]
            if self.raw.shape[0] < CHUNK + 480:
                continue
            spec = self._melspec(self.raw)
            self.melbuf = np.vstack([self.melbuf, spec])[-970:]
            win = self.melbuf[-76:][None, :, :, None]
            e = self._embed(win)
            self.feat = np.vstack([self.feat, e[None, :]])[-120:]
            x = self.feat[-self.n_frames:][None, :, :].astype(np.float32)
            score = float(np.squeeze(self.cls.run(None, {self.cls_input: x})[0]))
        return score


class WakeWordListener:
    """Background thread: subscribes to the MicHub and calls on_detect()."""

    def __init__(self, hub, on_detect, threshold: float = 0.5, debounce: float = 2.0) -> None:
        self.hub = hub
        self.on_detect = on_detect
        self.threshold = threshold
        self.debounce = debounce
        self.suspended = threading.Event()  # set while recording/speaking
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.model: WakeWordModel | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wakeword", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            if self.model is None:
                self.model = WakeWordModel()
            sid, q = self.hub.subscribe()
        except Exception as e:
            self.error = str(e)
            log.error("wake word init failed: %s", e)
            return
        log.info("wake word listening (threshold %.2f)", self.threshold)
        last = 0.0
        was_suspended = False
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
                    self.model.reset()
                    was_suspended = False
                score = self.model.process(c)
                now = time.monotonic()
                if score >= self.threshold and now - last > self.debounce:
                    last = now
                    log.info("wake word detected (%.2f)", score)
                    self.model.reset()
                    try:
                        self.on_detect()
                    except Exception:
                        log.exception("wake handler failed")
        finally:
            self.hub.unsubscribe(sid)
