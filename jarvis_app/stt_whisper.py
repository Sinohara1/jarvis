"""Optional Whisper speech recognition (faster-whisper / CTranslate2), v1.5.x.

Why: the small Vosk models (~45 MB) are fast and stream while you speak, but mangle short commands,
names and anything English. Whisper is a large multilingual model: Russian, Ukrainian, English and
mixed RU/EN phrases come out as real words with punctuation, English parts in Latin letters.

How it fits without rewriting the voice pipeline:
  * Vosk keeps streaming during recording exactly as before (fallback + the name wake word);
  * when the phrase ends, the same 16 kHz WAV is decoded by Whisper (~0.15–0.4 s on an NVIDIA GPU,
    ~1–3 s on CPU depending on the model); if Whisper is not ready / fails / returns junk, the Vosk
    text (or the cloud) is used as before;
  * the result still goes through stt_normalize + the English command fixer (voice_contacts).

Everything is downloaded on demand into %LOCALAPPDATA%\\Jarvis\\whisper:
  models\\<name>\\   CTranslate2 model from Hugging Face (0.5–1.6 GB)
  cuda\\             cuBLAS 12.9 DLLs from NVIDIA's PyPI wheel (~0.55 GB download, ~0.77 GB on disk) —
                    only for GPU mode; the NVIDIA driver itself must be installed.

Honest limits:
  * the Python package (faster-whisper + ctranslate2 + av + tokenizers) must be bundled into the exe
    (build_exe.ps1 adds it when installed in .venv; ~+60–90 MB) — without it the option shows
    «не входит в эту сборку»;
  * RTX 50xx (Blackwell, sm_120): the PyPI CTranslate2 wheel ships kernels up to sm_86 + PTX, so the
    driver JIT-compiles them on the FIRST GPU load (can take a few minutes, then cached by the driver);
    INT8 is disabled on sm_120 by CTranslate2, so GPU mode uses float16. If CUDA fails we fall back
    to CPU int8 automatically (slower; pick «small» for CPU);
  * Whisper is not streaming: recognition starts after the pause, so it adds its decode time;
  * on silence/noise Whisper can «hallucinate» stock phrases («Продолжение следует…»): those are
    filtered and low-confidence results fall back to Vosk/cloud.
"""

from __future__ import annotations

import gc
import hashlib
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile

log = logging.getLogger("jarvis")
IS_WIN = sys.platform == "win32"

# ─── catalog ─────────────────────────────────────────────────────────────────

_SMALL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
_V3_FILES = ("config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json")

MODELS: dict[str, dict] = {
    "small": {
        "label": "Small — 0,5 ГБ, быстро даже без видеокарты",
        "repos": ["Systran/faster-whisper-small"], "files": _SMALL_FILES,
        "bin_size": 483_546_902, "bin_sha256": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
        "mb": 488,
    },
    "medium": {
        "label": "Medium — 1,5 ГБ, точнее, лучше с видеокартой",
        "repos": ["Systran/faster-whisper-medium"], "files": _SMALL_FILES,
        "bin_size": 1_527_906_378, "bin_sha256": "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
        "mb": 1533,
    },
    "large-v3-turbo": {
        "label": "Large-v3 Turbo — 1,6 ГБ, лучшее качество (видеокарта NVIDIA)",
        "repos": ["deepdml/faster-whisper-large-v3-turbo-ct2", "dropbox-dash/faster-whisper-large-v3-turbo"],
        "files": _V3_FILES,
        "bin_size": 1_617_884_929, "bin_sha256": "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
        "mb": 1624,
    },
}
DEFAULT_MODEL = "large-v3-turbo"
DEVICES = ("auto", "cuda", "cpu")
LANG_MODES = ("pair", "speech", "auto")   # «мой язык + английский» | только мой язык | любой

CUBLAS = {
    "url": "https://files.pythonhosted.org/packages/20/e2/fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/"
           "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
    "sha256": "623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
    "size": 553_162_896,
    "members": ("nvidia/cublas/bin/cublas64_12.dll", "nvidia/cublas/bin/cublasLt64_12.dll"),
}

# Stock phrases Whisper invents on silence / music (YouTube subtitle credits in its training data).
_HALLUCINATIONS = (
    "продолжение следует", "субтитры сделал", "субтитры создавал", "субтитры подготовил", "субтитры делал",
    "редактор субтитров", "корректор а", "dimatorzok", "спасибо за просмотр", "подписывайтесь на канал",
    "подпишись на канал", "ставьте лайки", "thank you for watching", "thanks for watching",
    "subtitles by the amara", "amara.org", "please subscribe", "like and subscribe", "дякую за перегляд",
    "субтитри", "vielen dank fürs zuschauen", "untertitel im auftrag", "napisy stworzone",
)


def _root() -> str:
    from .config import DATA_DIR
    return os.path.join(DATA_DIR, "whisper")


def model_dir(name: str) -> str:
    return os.path.join(_root(), "models", name)


def cuda_dir() -> str:
    return os.path.join(_root(), "cuda")


def norm_model(name: str | None) -> str:
    n = str(name or "").strip().lower()
    return n if n in MODELS else DEFAULT_MODEL


def installed(name: str) -> bool:
    m = MODELS.get(name)
    if not m:
        return False
    d = model_dir(name)
    try:
        if os.path.getsize(os.path.join(d, "model.bin")) != m["bin_size"]:
            return False
    except OSError:
        return False
    return all(os.path.isfile(os.path.join(d, f)) for f in m["files"])


def cuda_installed() -> bool:
    d = cuda_dir()
    return all(os.path.isfile(os.path.join(d, os.path.basename(x))) for x in CUBLAS["members"])


def _cublas_on_path() -> bool:
    """cuBLAS 12 already reachable (CUDA Toolkit / another app on PATH)?"""
    for p in (os.environ.get("PATH") or "").split(os.pathsep):
        if p and os.path.isfile(os.path.join(p, "cublas64_12.dll")) and os.path.isfile(os.path.join(p, "cublasLt64_12.dll")):
            return True
    return False


# ─── package / GPU probes ────────────────────────────────────────────────────

_import_err: str | None = None
_probe_lock = threading.Lock()
_gpu_cache: list = []


def available() -> bool:
    """faster-whisper importable (bundled into this build)?"""
    global _import_err
    with _probe_lock:
        if _import_err is not None:
            return _import_err == ""
        try:
            _add_dll_dirs()
            import faster_whisper  # noqa: F401
            _import_err = ""
        except Exception as e:  # ImportError, missing DLL…
            _import_err = f"{type(e).__name__}: {e}"[:200]
            log.info("whisper: faster-whisper unavailable: %s", _import_err)
        return _import_err == ""


def import_error() -> str:
    return _import_err or ""


def package_present() -> bool:
    """Cheap check for the settings page (no 1–2 s import of ctranslate2/av on the UI thread): the real
    import result once it was tried, else whether the packages are in this build at all."""
    if _import_err is not None:
        return _import_err == ""
    try:
        import importlib.util
        return importlib.util.find_spec("faster_whisper") is not None and importlib.util.find_spec("ctranslate2") is not None
    except Exception:
        return False


def nvidia_gpu(probe: bool = True) -> str:
    """Name of the first NVIDIA GPU ('' if none / no driver). Cached; ``probe=False`` never runs nvidia-smi."""
    if _gpu_cache:
        return _gpu_cache[0]
    if not probe:
        return ""
    name = ""
    if IS_WIN:
        try:
            import ctypes
            ctypes.WinDLL("nvcuda.dll")  # the driver's CUDA library
            name = "NVIDIA"
            try:
                import subprocess
                r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True,
                                   timeout=5, creationflags=0x08000000)
                first = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
                if first and first[0].strip():
                    name = first[0].strip()
            except Exception:
                pass
        except OSError:
            name = ""
    _gpu_cache.append(name)
    return name


def _add_dll_dirs() -> None:
    """Make our downloaded cuBLAS visible to ctranslate2.dll (must run before importing ctranslate2)."""
    os.environ.setdefault("CUDA_CACHE_MAXSIZE", str(2 * 1024 ** 3))  # keep the sm_120 PTX-JIT result
    d = cuda_dir()
    if IS_WIN and os.path.isdir(d):
        try:
            os.add_dll_directory(d)  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            pass
        if d not in (os.environ.get("PATH") or ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def pick_device(pref: str, gpu: str, cublas: bool) -> tuple[str, str]:
    """(device, compute_type). float16 on CUDA (INT8 is disabled on RTX 50xx by CTranslate2 and gives
    nothing on 16 GB anyway), int8 on CPU."""
    p = pref if pref in DEVICES else "auto"
    if p == "cpu" or not gpu or not cublas:
        return "cpu", "int8"
    return "cuda", "float16"


# ─── text helpers (pure, unit-tested) ────────────────────────────────────────

def choose_language(probs, allowed, fallback: str) -> str:
    """Best language among ``allowed`` from Whisper's [(code, p), …]; ``fallback`` if none listed."""
    allow = [a for a in allowed if a]
    best, bp = "", -1.0
    for code, p in probs or ():
        if (not allow or code in allow) and float(p) > bp:
            best, bp = code, float(p)
    return best or fallback


def allowed_languages(mode: str, speech_lang: str) -> list[str]:
    sl = speech_lang or "ru"
    if mode == "speech":
        return [sl]
    if mode == "auto":
        return []
    return [sl] if sl == "en" else [sl, "en"]


def is_hallucination(text: str) -> bool:
    t = re.sub(r"[^\w\s.]", " ", (text or "").lower()).strip()
    t = re.sub(r"\s+", " ", t)
    if not t:
        return True
    return any(h in t for h in _HALLUCINATIONS) and len(t) <= 90


def clean_text(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    t = t.strip(" -–—")
    # «Открой телеграм...» → drop trailing ellipsis Whisper adds to cut-off phrases
    t = re.sub(r"\.{2,}$|…$", "", t).strip()
    return t


def segments_confidence(segs) -> float:
    """0..1 from Whisper segment avg_logprob (weighted by text length), penalised by no_speech_prob."""
    import math
    tot, acc = 0, 0.0
    for s in segs or ():
        n = max(1, len((getattr(s, "text", "") or "").strip()))
        p = math.exp(max(-5.0, min(0.0, float(getattr(s, "avg_logprob", -1.0)))))
        ns = float(getattr(s, "no_speech_prob", 0.0) or 0.0)
        if ns > 0.6:
            p *= (1.0 - ns)
        acc += p * n
        tot += n
    return round(acc / tot, 3) if tot else 0.0


def build_prompt(assistant_name: str, names, lang: str) -> str:
    """Short vocabulary prompt: assistant name, app names, contacts. NOT used by default: on test phrases
    (large-v3-turbo / small, RU + EN + mixed) any initial prompt made results slightly worse and less
    stable than none; names are fixed afterwards by stt_normalize + voice_contacts instead."""
    words: list[str] = []
    for w in [assistant_name, "Telegram", "Spotify", "Discord", "YouTube", "Steam", "Chrome", *(names or [])]:
        w = str(w or "").strip()
        if w and w.lower() not in {x.lower() for x in words}:
            words.append(w)
    words = words[:24]
    lead = {"ru": "Команды ассистенту:", "uk": "Команди асистенту:", "de": "Befehle:", "pl": "Polecenia:"}.get(lang, "Commands:")
    return f"{lead} {', '.join(words)}."


def is_prompt_echo(text: str, prompt: str) -> bool:
    """Whisper on near-silence sometimes returns the initial prompt itself. A single word that is in the
    prompt («Телеграм») is a real command, so only long matches / the prompt's lead count as echo."""
    if not prompt or not text:
        return False
    t = re.sub(r"\s+", " ", text.lower()).strip(" .,:")
    p = prompt.lower()
    if len(t) >= 20 and t in p:
        return True
    lead = p.split(":", 1)[0].strip()
    return bool(lead) and lead in t


_CYR = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
_LAT = re.compile(r"[A-Za-z]")


def fix_lang(text: str, lang: str, speech_lang: str) -> str:
    """The language must agree with the script Whisper actually wrote: «Который час?» tagged "en" (seen
    once on CPU) would switch the reply to English."""
    cyr, lat = len(_CYR.findall(text or "")), len(_LAT.findall(text or ""))
    if lang == "en" and cyr > lat:
        return speech_lang if speech_lang in ("ru", "uk") else "ru"
    if lang in ("ru", "uk") and lat > 0 and cyr == 0:
        return "en"
    return lang


def wav_to_float(wav: bytes):
    """16 kHz mono int16 WAV bytes → float32 numpy in [-1, 1] (None if the format is different)."""
    import io
    import wave
    import numpy as np
    with wave.open(io.BytesIO(wav)) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            return None
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


# ─── downloads ───────────────────────────────────────────────────────────────

class Cancelled(Exception):
    pass


_dl_lock = threading.Lock()
_cancel = threading.Event()


def cancel_download() -> None:
    _cancel.set()


def _fetch(url: str, dest: str, *, expect_size: int = 0, sha256: str = "", on_bytes=None) -> None:
    import requests
    h = hashlib.sha256() if sha256 else None
    tmp = dest + ".part"
    with requests.get(url, stream=True, timeout=30, allow_redirects=True,
                      headers={"User-Agent": "Jarvis-desktop"}) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if _cancel.is_set():
                    raise Cancelled()
                f.write(chunk)
                if h:
                    h.update(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
    size = os.path.getsize(tmp)
    if expect_size and size != expect_size:
        os.remove(tmp)
        raise RuntimeError(f"размер файла не совпал ({size} вместо {expect_size})")
    if h and h.hexdigest() != sha256:
        os.remove(tmp)
        raise RuntimeError("контрольная сумма не совпала — файл повреждён")
    os.replace(tmp, dest)


def download_model(name: str, on_progress=None) -> str:
    """Download a CTranslate2 Whisper model into %LOCALAPPDATA%\\Jarvis\\whisper\\models\\<name>.
    on_progress(done, total). Tries the mirrors in order. Returns the model dir."""
    name = norm_model(name)
    m = MODELS[name]
    with _dl_lock:
        _cancel.clear()
        final = model_dir(name)
        if installed(name):
            return final
        os.makedirs(os.path.dirname(final), exist_ok=True)
        total = m["bin_size"] + 6_000_000
        done = [0]
        last = [0.0]

        def bump(n: int) -> None:
            done[0] += n
            if on_progress and time.monotonic() - last[0] > 0.3:
                last[0] = time.monotonic()
                on_progress(done[0], total)

        err: Exception | None = None
        for repo in m["repos"]:
            tmpdir = tempfile.mkdtemp(prefix="whisper_", dir=os.path.dirname(final))
            try:
                done[0] = 0
                for f in m["files"]:
                    url = f"https://huggingface.co/{repo}/resolve/main/{f}"
                    if f == "model.bin":
                        _fetch(url, os.path.join(tmpdir, f), expect_size=m["bin_size"], sha256=m["bin_sha256"],
                               on_bytes=bump)
                    else:
                        _fetch(url, os.path.join(tmpdir, f), on_bytes=bump)
                if os.path.exists(final):
                    shutil.rmtree(final, ignore_errors=True)
                os.replace(tmpdir, final)
                if on_progress:
                    on_progress(total, total)
                log.info("whisper model %s installed from %s", name, repo)
                return final
            except Cancelled:
                raise
            except Exception as e:
                err = e
                log.warning("whisper model %s from %s failed: %s", name, repo, e)
            finally:
                if os.path.isdir(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"не удалось скачать модель Whisper: {err}")


def download_cuda(on_progress=None) -> str:
    """cuBLAS 12.9 for GPU mode: NVIDIA's official PyPI wheel, sha256-checked; only the two DLLs are
    kept (wheel deleted). Returns the dir."""
    with _dl_lock:
        _cancel.clear()
        d = cuda_dir()
        if cuda_installed():
            return d
        os.makedirs(d, exist_ok=True)
        whl = os.path.join(d, "cublas.whl")
        done, last = [0], [0.0]

        def bump(n: int) -> None:
            done[0] += n
            if on_progress and time.monotonic() - last[0] > 0.3:
                last[0] = time.monotonic()
                on_progress(done[0], CUBLAS["size"])
        try:
            _fetch(CUBLAS["url"], whl, expect_size=CUBLAS["size"], sha256=CUBLAS["sha256"], on_bytes=bump)
            with zipfile.ZipFile(whl) as z:
                for mem in CUBLAS["members"]:
                    out = os.path.join(d, os.path.basename(mem))
                    with z.open(mem) as src, open(out + ".part", "wb") as dst:
                        shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
                    os.replace(out + ".part", out)
        finally:
            for p in (whl, whl + ".part"):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        log.info("whisper: cuBLAS installed into %s", d)
        return d


def delete_model(name: str) -> bool:
    d = model_dir(norm_model(name))
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    return not os.path.isdir(d)


# ─── engine ──────────────────────────────────────────────────────────────────

class WhisperEngine:
    """One loaded faster-whisper model (process-wide singleton via ``engine()``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.model = None
        self.name = ""
        self.device = ""
        self.compute = ""
        self.loading = False
        self.error = ""
        self.gpu_error = ""
        self.load_sec = 0.0

    def ready(self, name: str | None = None) -> bool:
        return self.model is not None and (name is None or name == self.name)

    def load(self, name: str, device_pref: str = "auto") -> bool:
        name = norm_model(name)
        if self.ready(name) and (device_pref in ("auto", self.device)):
            return True
        if not available():
            self.error = "faster-whisper не входит в эту сборку"
            return False
        if not installed(name):
            self.error = "модель не скачана"
            return False
        self.loading = True
        try:
            _add_dll_dirs()
            from faster_whisper import WhisperModel
            gpu = nvidia_gpu()
            device, compute = pick_device(device_pref, gpu, cuda_installed() or _cublas_on_path())
            t0 = time.monotonic()
            model = None
            if device == "cuda":
                try:
                    model = WhisperModel(model_dir(name), device="cuda", compute_type=compute)
                    self._warm(model)
                    self.gpu_error = ""
                except Exception as e:
                    self.gpu_error = f"{type(e).__name__}: {e}"[:200]
                    log.warning("whisper: CUDA load failed (%s) → CPU int8", self.gpu_error)
                    model, device, compute = None, "cpu", "int8"
                    gc.collect()
            if model is None:
                threads = max(2, min(8, (os.cpu_count() or 4) - 2))
                model = WhisperModel(model_dir(name), device="cpu", compute_type="int8", cpu_threads=threads)
                self._warm(model)
            with self._lock:
                old = self.model
                self.model, self.name, self.device, self.compute = model, name, device, compute
            del old
            self.load_sec = round(time.monotonic() - t0, 2)
            self.error = ""
            log.info("whisper: %s loaded on %s/%s in %.2fs (gpu=%r)", name, device, compute, self.load_sec, gpu)
            return True
        except Exception as e:
            self.error = f"не удалось загрузить Whisper: {type(e).__name__}: {e}"[:220]
            log.exception("whisper load failed")
            return False
        finally:
            self.loading = False

    @staticmethod
    def _warm(model) -> None:
        """First call compiles/loads kernels (on RTX 50xx: PTX JIT) — do it now, not on his first phrase."""
        import numpy as np
        segs, _info = model.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1,
                                       without_timestamps=True, condition_on_previous_text=False)
        list(segs)

    def unload(self) -> None:
        with self._lock:
            self.model, self.name, self.device, self.compute = None, "", "", ""
        gc.collect()

    def transcribe(self, wav: bytes, *, lang_mode: str = "pair", speech_lang: str = "ru",
                   prompt: str = "") -> tuple[str, float, str]:
        """→ (text, confidence 0..1, language). ('', 0, '') when not loaded / junk."""
        audio = wav_to_float(wav) if wav else None
        if audio is None or audio.size < 16000 * 0.25:
            return "", 0.0, ""
        with self._lock:
            model = self.model
            if model is None:
                return "", 0.0, ""
            allowed = allowed_languages(lang_mode, speech_lang)
            lang = allowed[0] if len(allowed) == 1 else None
            if lang is None:
                try:
                    _l, _p, probs = model.detect_language(audio)
                    lang = choose_language(probs, allowed, speech_lang or "ru")
                except Exception as e:  # older faster-whisper: let transcribe detect
                    log.info("whisper detect_language failed: %s", e)
                    lang = None
            gpu = self.device == "cuda"
            segs, info = model.transcribe(
                audio, language=lang, beam_size=5 if gpu else 2, best_of=1,
                temperature=[0.0, 0.2, 0.4], condition_on_previous_text=False, without_timestamps=True,
                initial_prompt=prompt or None, no_speech_threshold=0.6, log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4, vad_filter=False)
            segs = list(segs)
        text = clean_text(" ".join((s.text or "").strip() for s in segs))
        conf = segments_confidence(segs)
        if not text or is_hallucination(text):
            return "", 0.0, lang or getattr(info, "language", "") or ""
        if is_prompt_echo(text, prompt):
            return "", 0.0, lang or ""  # it just echoed the vocabulary prompt
        return text, conf, fix_lang(text, lang or getattr(info, "language", "") or "", speech_lang)

    def info(self) -> dict:
        return {"loaded": self.model is not None, "model": self.name, "device": self.device,
                "compute": self.compute, "loading": self.loading, "error": self.error,
                "gpu_error": self.gpu_error, "load_sec": self.load_sec}


_engine = WhisperEngine()


def engine() -> WhisperEngine:
    return _engine


def catalog() -> list[dict]:
    return [{"key": k, "label": v["label"], "mb": v["mb"], "installed": installed(k)} for k, v in MODELS.items()]
