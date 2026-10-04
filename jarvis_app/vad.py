"""Voice activity detection for end-of-speech: Silero VAD (MIT, 2.3 MB ONNX, bundled in models/)
via onnxruntime, with the old energy gate as a fallback. ~0.3 ms per 32 ms frame on one core."""

from __future__ import annotations

import logging
import os
import threading

import numpy as np

from .config import MODELS_DIR

log = logging.getLogger("jarvis")

SILERO_PATH = os.path.join(MODELS_DIR, "silero_vad.onnx")
FRAME = 512      # samples @16 kHz (32 ms) — what the model expects
CONTEXT = 64     # samples of the previous frame prepended (Silero v5 wrapper behaviour)

_session = None
_session_err: str | None = None
_session_lock = threading.Lock()


def _get_session():
    global _session, _session_err
    with _session_lock:
        if _session is None and _session_err is None:
            try:
                import onnxruntime as ort
                so = ort.SessionOptions()
                so.intra_op_num_threads = 1
                so.inter_op_num_threads = 1
                so.log_severity_level = 3
                _session = ort.InferenceSession(SILERO_PATH, sess_options=so, providers=["CPUExecutionProvider"])
            except Exception as e:  # missing file / onnxruntime problem → energy fallback
                _session_err = str(e)
                log.warning("silero vad unavailable, using energy VAD: %s", e)
        return _session


class SileroVAD:
    """Stateful speech probability for a stream of int16 chunks of any size."""

    def __init__(self) -> None:
        self.sess = _get_session()
        if self.sess is None:
            raise RuntimeError(_session_err or "silero vad unavailable")
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._ctx = np.zeros((1, CONTEXT), dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    def frame_prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._ctx, frame.reshape(1, -1)], axis=1)
        out, self._state = self.sess.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._ctx = x[:, -CONTEXT:]
        return float(out[0][0])

    def process(self, chunk) -> float:
        """Feed int16 samples; returns the max speech probability of the complete frames inside
        (or -1.0 if no complete frame yet)."""
        a = np.asarray(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, a])
        best = -1.0
        while self._buf.size >= FRAME:
            fr, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            best = max(best, self.frame_prob(fr))
        return best


class EndpointDetector:
    """Decides 'voiced' per 80 ms chunk. Silero with hysteresis when available, else adaptive energy.
    Usage: voiced = det.voiced(chunk, level, elapsed)."""

    ON, OFF = 0.5, 0.3

    def __init__(self, use_silero: bool = True) -> None:
        self.vad: SileroVAD | None = None
        if use_silero:
            try:
                self.vad = SileroVAD()
            except Exception:
                self.vad = None
        self.kind = "silero" if self.vad else "energy"
        self._in_speech = False
        self.noise: float | None = None
        self.last_prob = 0.0

    def voiced(self, chunk, level: float, elapsed: float) -> bool:
        if self.vad is not None:
            p = self.vad.process(chunk)
            if p < 0:
                return self._in_speech
            self.last_prob = p
            self._in_speech = p >= (self.OFF if self._in_speech else self.ON)
            return self._in_speech and level > 120.0  # ignore digital silence / tiny hum
        # energy fallback (v1.2 behaviour)
        if self.noise is None or elapsed < 0.4:
            self.noise = level if self.noise is None else (self.noise * 0.7 + level * 0.3)
        thr = max(450.0, (self.noise or 0) * 2.5)
        return level > thr and elapsed > 0.15
