"""Local offline TTS: Piper (onnxruntime + espeak-ng phonemes) with Russian voices.

Voices (~60 MB each) are downloaded once into %LOCALAPPDATA%\\Jarvis\\models\\piper with progress
and an md5 check. A sentence takes ~0.1 s on a desktop CPU (RTF ≈ 0.03), vs 1–10 s for edge-tts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

import numpy as np

from .config import DATA_DIR, bundle_dir, ensure_dir

log = logging.getLogger("jarvis")

PIPER_DIR = os.path.join(DATA_DIR, "models", "piper")
HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU"
VOICE_SIZE = 63_201_294

# key → (label, md5 of .onnx, md5 of .onnx.json, licence of the dataset)
PIPER_VOICES: dict[str, dict] = {
    "ru_RU-dmitri-medium": {"label": "Дмитрий", "gender": "м", "md5": "589ccc91745a1e2353508ff62c5941b7",
                            "md5_json": "4eaf0d090190ecb8d958d40c76fd85e8", "license": "CC0"},
    "ru_RU-denis-medium": {"label": "Денис", "gender": "м", "md5": "76c2f14e521fef3ed574f97ad492728e",
                           "md5_json": "e3df5957c07647cab05cf9910ef3ede0", "license": "CC0"},
    "ru_RU-irina-medium": {"label": "Ирина", "gender": "ж", "md5": "21fbe77fdc68bdc35d7adb6bf4f52199",
                           "md5_json": "e239bb7f22d5de4a44ec6b1cb6c06bb5", "license": "RHVoice"},
    "ru_RU-ruslan-medium": {"label": "Руслан", "gender": "м", "md5": "731eb188e63b4c57320e38047ba2d850",
                            "md5_json": "ae6e273bd38d6ecb05c2d1969b24db0c", "license": "CC BY-NC-SA 4.0"},
}
DEFAULT_VOICE = "ru_RU-dmitri-medium"
# edge voice → closest local voice (persona presets still name edge voices)
EDGE_TO_PIPER = {"ru-RU-DmitryNeural": "ru_RU-dmitri-medium", "ru-RU-SvetlanaNeural": "ru_RU-irina-medium"}


def voice_files(key: str, root: str = PIPER_DIR) -> tuple[str, str]:
    p = os.path.join(root, f"{key}.onnx")
    return p, p + ".json"


def installed(key: str, root: str = PIPER_DIR) -> bool:
    m, j = voice_files(key, root)
    try:
        return os.path.getsize(m) > 1_000_000 and os.path.getsize(j) > 100
    except OSError:
        return False


def voices_payload() -> list[dict]:
    return [{"key": k, "label": v["label"], "gender": v["gender"], "installed": installed(k),
             "license": v["license"]} for k, v in PIPER_VOICES.items()]


def espeak_dir() -> str | None:
    """Trimmed espeak-ng-data (ru+en) bundled into Jarvis.exe; None → the piper package's own copy."""
    p = os.path.join(bundle_dir(), "piper_data", "espeak-ng-data")
    return p if os.path.isdir(p) else None


def available() -> bool:
    try:
        import piper  # noqa: F401
        return True
    except Exception:
        return False


_dl_lock = threading.Lock()


def download_voice(key: str, on_progress=None, root: str = PIPER_DIR) -> str:
    """Download one voice (+json) with progress and md5 verification. Returns the .onnx path."""
    import requests
    if key not in PIPER_VOICES:
        raise ValueError(f"неизвестный голос {key}")
    meta = PIPER_VOICES[key]
    name = key.split("-")[1]
    with _dl_lock:
        mpath, jpath = voice_files(key, root)
        if installed(key, root):
            return mpath
        ensure_dir(root)
        for url_suffix, dest, md5, size in ((f"{key}.onnx.json", jpath, meta["md5_json"], 0),
                                            (f"{key}.onnx", mpath, meta["md5"], VOICE_SIZE)):
            url = f"{HF_BASE}/{name}/medium/{url_suffix}"
            tmp = dest + ".part"
            h = hashlib.md5()
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0) or size
                done, last = 0, 0.0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if on_progress and size and time.monotonic() - last > 0.3:
                            last = time.monotonic()
                            on_progress(done, total)
            if h.hexdigest() != md5:
                os.remove(tmp)
                raise RuntimeError("файл голоса повреждён (md5 не совпал) — попробуй ещё раз")
            os.replace(tmp, dest)
        if on_progress:
            on_progress(VOICE_SIZE, VOICE_SIZE)
        log.info("piper voice installed: %s", mpath)
        return mpath


class PiperTTS:
    """Keeps one loaded voice; synth(text) → (int16 mono samples, sample_rate)."""

    def __init__(self, root: str = PIPER_DIR, threads: int | None = None) -> None:
        self.root = root
        self.threads = threads or max(1, min(4, (os.cpu_count() or 4) // 2))  # leave cores for games
        self._voice = None
        self._key: str | None = None
        self._lock = threading.Lock()

    def ready(self, key: str) -> bool:
        return installed(key, self.root) and available()

    def load(self, key: str):
        with self._lock:
            if self._voice is not None and self._key == key:
                return self._voice
            import onnxruntime as ort
            from pathlib import Path
            from piper import PiperVoice
            from piper.config import PiperConfig
            mpath, jpath = voice_files(key, self.root)
            t0 = time.monotonic()
            with open(jpath, "r", encoding="utf-8") as f:
                cfg = PiperConfig.from_dict(json.load(f))
            so = ort.SessionOptions()
            so.intra_op_num_threads = self.threads
            so.inter_op_num_threads = 1
            so.log_severity_level = 3
            sess = ort.InferenceSession(mpath, sess_options=so, providers=["CPUExecutionProvider"])
            kw = {}
            ed = espeak_dir()
            if ed:
                kw["espeak_data_dir"] = Path(ed)
            v = PiperVoice(session=sess, config=cfg, **kw)
            list(v.synthesize("Да."))  # warm-up: espeak init + first inference
            self._voice, self._key = v, key
            log.info("piper voice %s loaded in %.2fs (%d threads)", key, time.monotonic() - t0, self.threads)
            return v

    def synth(self, text: str, key: str, rate: int = 0) -> tuple[np.ndarray, int]:
        from piper import SynthesisConfig
        v = self.load(key)
        ls = 1.0 / (1.0 + max(-50, min(50, int(rate))) / 100.0)
        cfg = SynthesisConfig(length_scale=ls * float(v.config.length_scale or 1.0))
        with self._lock:
            chunks = list(v.synthesize(text, cfg))
        if not chunks:
            return np.zeros(0, dtype=np.int16), v.config.sample_rate
        sr = chunks[0].sample_rate
        gap = np.zeros(int(sr * 0.12), dtype=np.int16)  # short pause between sentences inside one clip
        parts: list[np.ndarray] = []
        for i, c in enumerate(chunks):
            if i:
                parts.append(gap)
            parts.append(c.audio_int16_array)
        return np.concatenate(parts), sr
