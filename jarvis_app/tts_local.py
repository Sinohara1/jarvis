"""Local offline TTS: Piper (onnxruntime + espeak-ng phonemes): ru/uk/en/de/pl voices + the user's own.

Voices (~60 MB each) are downloaded once into %LOCALAPPDATA%\\Jarvis\\models\\piper with progress
and an md5 check. A sentence takes ~0.1 s on a desktop CPU (RTF ≈ 0.03), vs 1–10 s for edge-tts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time

import numpy as np

from .config import DATA_DIR, bundle_dir, ensure_dir

log = logging.getLogger("jarvis")

PIPER_DIR = os.path.join(DATA_DIR, "models", "piper")
CUSTOM_DIR = os.path.join(DATA_DIR, "voices")
HF_ROOT = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICE_SIZE = 63_201_294  # typical medium voice (progress fallback)
CUSTOM_PREFIX = "custom:"


def _v(lang, label, gender, path, md5, md5_json, size, license, speaker=None, file=None):
    d = {"lang": lang, "label": label, "gender": gender, "path": path, "md5": md5, "md5_json": md5_json,
         "size": size, "license": license}
    if speaker is not None:
        d["speaker"] = speaker
    if file:
        d["file"] = file
    return d


# key → catalogue entry. md5/size from rhasspy/piper-voices voices.json. «path» is the HF folder.
# Several keys may share one file (multi-speaker model): «file» + «speaker».
_UK = "uk_UA-ukrainian_tts-medium"
_UK_MD5 = ("3366c3d4f31cb77966fb14d042956b4f", "3bf9b2b1fcc8e599947cdda4af130498", 76_735_663)
PIPER_VOICES: dict[str, dict] = {
    "ru_RU-dmitri-medium": _v("ru", "Дмитрий", "м", "ru/ru_RU/dmitri/medium", "589ccc91745a1e2353508ff62c5941b7",
                              "4eaf0d090190ecb8d958d40c76fd85e8", VOICE_SIZE, "CC0"),
    "ru_RU-denis-medium": _v("ru", "Денис", "м", "ru/ru_RU/denis/medium", "76c2f14e521fef3ed574f97ad492728e",
                             "e3df5957c07647cab05cf9910ef3ede0", VOICE_SIZE, "CC0"),
    "ru_RU-irina-medium": _v("ru", "Ирина", "ж", "ru/ru_RU/irina/medium", "21fbe77fdc68bdc35d7adb6bf4f52199",
                             "e239bb7f22d5de4a44ec6b1cb6c06bb5", VOICE_SIZE, "RHVoice"),
    "ru_RU-ruslan-medium": _v("ru", "Руслан", "м", "ru/ru_RU/ruslan/medium", "731eb188e63b4c57320e38047ba2d850",
                              "ae6e273bd38d6ecb05c2d1969b24db0c", VOICE_SIZE, "CC BY-NC-SA 4.0"),
    "uk_UA-mykyta-medium": _v("uk", "Микита", "м", "uk/uk_UA/ukrainian_tts/medium", *_UK_MD5, "CC0", speaker=1, file=_UK),
    "uk_UA-lada-medium": _v("uk", "Лада", "ж", "uk/uk_UA/ukrainian_tts/medium", *_UK_MD5, "CC0", speaker=0, file=_UK),
    "uk_UA-tetiana-medium": _v("uk", "Тетяна", "ж", "uk/uk_UA/ukrainian_tts/medium", *_UK_MD5, "CC0", speaker=2, file=_UK),
    "en_US-ryan-medium": _v("en", "Ryan (US)", "м", "en/en_US/ryan/medium", "8f06d3aff8ded5a7f13f907e6bec32ac",
                            "f173a2b5202b3e4128ccc3ed8195306c", VOICE_SIZE, "CC BY-NC-SA 4.0"),
    "en_US-lessac-medium": _v("en", "Lessac (US)", "ж", "en/en_US/lessac/medium", "2fc642b535197b6305c7c8f92dc8b24f",
                              "c1f2b7bddefe113f3255ff9ef234cfd3", VOICE_SIZE, "Blizzard 2013 (non-commercial)"),
    "en_US-amy-medium": _v("en", "Amy (US)", "ж", "en/en_US/amy/medium", "778d28aeb95fcdf8a882344d9df142fc",
                           "7f37dadb26340c90ebc8088e0b252310", VOICE_SIZE, "Mimic 3"),
    "en_GB-alan-medium": _v("en", "Alan (UK)", "м", "en/en_GB/alan/medium", "8f6b35eeb8ef6269021c6cb6d2414c9b",
                            "b11d9afd0a8f5372c42a52fbd6e021d4", VOICE_SIZE, "Mimic 3"),
    "de_DE-thorsten-medium": _v("de", "Thorsten", "м", "de/de_DE/thorsten/medium", "a129b00fb3078df43c96bab6c94535c0",
                                "843a7bd7272724f750534dd5a26d1aad", VOICE_SIZE, "CC0"),
    "de_DE-kerstin-low": _v("de", "Kerstin", "ж", "de/de_DE/kerstin/low", "1d5e5788cfddb04cbb34418f2841931e",
                            "a96995af6c1c5b37ed3d62b5107e1d9a", 63_104_526, "CC0"),
    "de_DE-ramona-low": _v("de", "Ramona", "ж", "de/de_DE/ramona/low", "b4aaf3673170a0d96519cdc992c23fda",
                           "d61011961f01f349331b1a1b1b0ca58a", 63_104_526, "M-AILABS"),
    "pl_PL-darkman-medium": _v("pl", "Darkman", "м", "pl/pl_PL/darkman/medium", "27bf2d71e934b112657544fd0b100a7a",
                               "1c13180312cca98cb75ca39b31972056", VOICE_SIZE, "CC0"),
    "pl_PL-gosia-medium": _v("pl", "Gosia", "ж", "pl/pl_PL/gosia/medium", "ecf817530e575025166e454adde1f382",
                             "e57055b9eec14e570617af2b716bd5c3", VOICE_SIZE, "CC0"),
    "pl_PL-mc_speech-medium": _v("pl", "Magda (MC Speech)", "ж", "pl/pl_PL/mc_speech/medium",
                                 "a927e2f2c882bb40cbc2e5f3356ce19b", "3f506e68bb9531b11e94e5f5dda5dd21", VOICE_SIZE, "CC0"),
}
DEFAULT_VOICE = "ru_RU-dmitri-medium"
# per language: default voice by gender of the Russian voice
LANG_DEFAULTS = {"ru": {"м": "ru_RU-dmitri-medium", "ж": "ru_RU-irina-medium"},
                 "uk": {"м": "uk_UA-mykyta-medium", "ж": "uk_UA-tetiana-medium"},
                 "en": {"м": "en_US-ryan-medium", "ж": "en_US-lessac-medium"},
                 "de": {"м": "de_DE-thorsten-medium", "ж": "de_DE-kerstin-low"},
                 "pl": {"м": "pl_PL-darkman-medium", "ж": "pl_PL-gosia-medium"}}
# edge voice → closest local voice (persona presets still name edge voices)
EDGE_TO_PIPER = {"ru-RU-DmitryNeural": "ru_RU-dmitri-medium", "ru-RU-SvetlanaNeural": "ru_RU-irina-medium"}
KEY_RE = re.compile(r"(?:[a-z]{2,3}_[A-Z]{2}-[a-z0-9_]+-(?:x_low|low|medium|high)|custom:[a-z0-9_.-]{1,60})")
LINKS = ("https://huggingface.co/rhasspy/piper-voices", "https://rhasspy.github.io/piper-samples/")


def is_custom(key: str) -> bool:
    return str(key or "").startswith(CUSTOM_PREFIX)


def file_key(key: str) -> str:
    meta = PIPER_VOICES.get(key)
    return (meta.get("file") or key) if meta else key


def voice_files(key: str, root: str = PIPER_DIR, custom_root: str = CUSTOM_DIR) -> tuple[str, str]:
    if is_custom(key):
        name = key[len(CUSTOM_PREFIX):]
        p = os.path.join(custom_root, name, f"{name}.onnx")
    else:
        p = os.path.join(root, f"{file_key(key)}.onnx")
    return p, p + ".json"


def installed(key: str, root: str = PIPER_DIR, custom_root: str = CUSTOM_DIR) -> bool:
    if not key or not KEY_RE.fullmatch(key):
        return False
    m, j = voice_files(key, root, custom_root)
    try:
        return os.path.getsize(m) > 1_000_000 and os.path.getsize(j) > 100
    except OSError:
        return False


def speaker_of(key: str) -> int | None:
    meta = PIPER_VOICES.get(key)
    return meta.get("speaker") if meta else None


def lang_from_config(cfg: dict) -> tuple[str, str]:
    """(our 2-letter code, full code like «uk_UA») from a voice .onnx.json."""
    lg = cfg.get("language") or {}
    full = str(lg.get("code") or "")
    fam = str(lg.get("family") or "") or full.split("_")[0]
    if not fam:
        fam = str((cfg.get("espeak") or {}).get("voice") or "").split("-")[0]
    return fam.lower()[:3], full or fam


_custom_cache: dict[str, tuple[float, dict]] = {}


def custom_meta(key: str, custom_root: str = CUSTOM_DIR) -> dict | None:
    m, j = voice_files(key, custom_root=custom_root)
    try:
        mt = os.path.getmtime(j)
        hit = _custom_cache.get(j)
        if hit and hit[0] == mt:
            return hit[1]
        with open(j, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        code, full = lang_from_config(cfg)
        meta = {"key": key, "label": str(cfg.get("jarvis_label") or key[len(CUSTOM_PREFIX):]), "lang": code,
                "lang_full": full, "size": os.path.getsize(m), "custom": True,
                "speakers": int(cfg.get("num_speakers") or 1)}
        _custom_cache[j] = (mt, meta)
        return meta
    except (OSError, ValueError):
        return None


def voice_lang(key: str) -> str | None:
    if is_custom(key):
        m = custom_meta(key)
        return m["lang"] if m else None
    meta = PIPER_VOICES.get(key)
    return meta["lang"] if meta else (key[:2] if KEY_RE.fullmatch(key or "") else None)


def list_custom(custom_root: str = CUSTOM_DIR) -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(custom_root))
    except OSError:
        return out
    for n in names:
        key = CUSTOM_PREFIX + n
        if KEY_RE.fullmatch(key) and installed(key, custom_root=custom_root):
            m = custom_meta(key, custom_root)
            if m:
                out.append(m)
    return out


def voices_payload() -> list[dict]:
    out = [{"key": k, "label": v["label"], "gender": v["gender"], "lang": v["lang"], "installed": installed(k),
            "license": v["license"], "mb": round(v["size"] / 1e6)} for k, v in PIPER_VOICES.items()]
    out += [dict(m, installed=True, gender="", license="свой", mb=round(m["size"] / 1e6)) for m in list_custom()]
    return out


def ru_gender(settings: dict) -> str:
    k = str(settings.get("piper_voice") or DEFAULT_VOICE)
    meta = PIPER_VOICES.get(k)
    if meta:
        return meta["gender"]
    return "ж" if any(x in str(settings.get("voice") or "") for x in ("Svetlana", "Dariya")) else "м"


def voice_for_lang(settings: dict, code: str) -> str:
    """The Piper voice for speaking `code`: the user's choice for that language if its language matches,
    else the default voice of that language (same gender as the Russian voice)."""
    if code == "ru":
        k = str(settings.get("piper_voice") or "")
    else:
        k = str((settings.get("piper_voices") or {}).get(code) or "")
    if k and voice_lang(k) == code and (not is_custom(k) or installed(k)):
        return k
    d = LANG_DEFAULTS.get(code)
    return d[ru_gender(settings)] if d else ""


def espeak_dir() -> str | None:
    """espeak-ng-data bundled into Jarvis.exe; None → the piper package's own copy."""
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
    """Download one catalogue voice (+json) with progress and md5 verification. Returns the .onnx path."""
    import requests
    if key not in PIPER_VOICES:
        raise ValueError(f"неизвестный голос {key}")
    meta = PIPER_VOICES[key]
    fk = file_key(key)
    with _dl_lock:
        mpath, jpath = voice_files(key, root)
        if installed(key, root):
            return mpath
        ensure_dir(root)
        size = int(meta.get("size") or VOICE_SIZE)
        for url_suffix, dest, md5, sz in ((f"{fk}.onnx.json", jpath, meta["md5_json"], 0),
                                          (f"{fk}.onnx", mpath, meta["md5"], size)):
            url = f"{HF_ROOT}/{meta['path']}/{url_suffix}"
            tmp = dest + ".part"
            h = hashlib.md5()
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0) or sz
                done, last = 0, 0.0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if on_progress and sz and time.monotonic() - last > 0.3:
                            last = time.monotonic()
                            on_progress(done, total)
            if h.hexdigest() != md5:
                os.remove(tmp)
                raise RuntimeError("файл голоса повреждён (md5 не совпал) — попробуй ещё раз")
            os.replace(tmp, dest)
        if on_progress:
            on_progress(size, size)
        log.info("piper voice installed: %s", mpath)
        return mpath


# ── custom voices (v1.4): «Загрузить свой голос» ──
class NeedJson(Exception):
    """The .onnx came without its .onnx.json — ask the user for it."""


MAX_CUSTOM_BYTES = 400_000_000


def _base(name: str) -> str:
    base = os.path.basename(name)
    for suf in (".onnx.json", ".onnx", ".zip", ".json"):
        if base.lower().endswith(suf):
            return base[: -len(suf)]
    return base


def _slug(name: str) -> str:
    from .lang import translit
    base = translit(_base(name))  # «Мой голос.onnx» → moy_golos
    tr = str.maketrans({c: "_" for c in " ()[]{}!@#$%^&*+=,;'\"`~<>?/\\|"})
    s = re.sub(r"[^a-z0-9_.-]", "", base.translate(tr).lower()).strip("._-")
    s = re.sub(r"_+", "_", s).strip("_")
    return (s or "voice")[:48]


def find_json_for(onnx_path: str) -> str | None:
    """Look for the matching config next to the model: X.onnx.json, X.json, or the only .json there."""
    d = os.path.dirname(os.path.abspath(onnx_path))
    base = os.path.basename(onnx_path)
    stem = base[:-5] if base.lower().endswith(".onnx") else base
    for cand in (onnx_path + ".json", os.path.join(d, stem + ".json"), os.path.join(d, stem + ".onnx.json")):
        if os.path.isfile(cand):
            return cand
    try:
        js = [f for f in os.listdir(d) if f.lower().endswith(".json") and "card" not in f.lower()]
    except OSError:
        return None
    return os.path.join(d, js[0]) if len(js) == 1 else None


def _from_zip(zpath: str, workdir: str) -> tuple[str, str | None]:
    import zipfile
    with zipfile.ZipFile(zpath) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        onnx = [i for i in infos if i.filename.lower().endswith(".onnx")]
        if not onnx:
            raise ValueError("в архиве нет файла .onnx")
        if len(onnx) > 1:
            raise ValueError("в архиве несколько .onnx — оставь один голос")
        m = onnx[0]
        if m.file_size > MAX_CUSTOM_BYTES:
            raise ValueError("файл голоса слишком большой")
        js = [i for i in infos if i.filename.lower().endswith(".json") and "card" not in i.filename.lower()]
        pick = None
        for i in js:
            if os.path.basename(i.filename).lower() in (os.path.basename(m.filename).lower() + ".json",
                                                         os.path.basename(m.filename)[:-5].lower() + ".json"):
                pick = i
        if pick is None and len(js) == 1:
            pick = js[0]
        mdst = os.path.join(workdir, os.path.basename(m.filename))
        with z.open(m) as src, open(mdst, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        jdst = None
        if pick is not None:
            jdst = mdst + ".json"
            with z.open(pick) as src, open(jdst, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return mdst, jdst


def import_voice(path: str, json_path: str | None = None, *, custom_root: str = CUSTOM_DIR,
                 threads: int = 2) -> dict:
    """Validate a user's Piper voice (.onnx [+ .onnx.json] or a .zip with both) and copy it into
    %LOCALAPPDATA%\\Jarvis\\voices\\<name>\\. Raises NeedJson if the config is missing,
    ValueError/RuntimeError with a Russian message if the voice does not work."""
    import tempfile
    if not path or not os.path.isfile(path):
        raise ValueError("файл не найден")
    work = tempfile.mkdtemp(prefix="jarvis_voice_")
    try:
        if path.lower().endswith(".zip"):
            onnx, js = _from_zip(path, work)
            js = json_path or js
            name_src = path
        elif path.lower().endswith(".onnx"):
            if os.path.getsize(path) > MAX_CUSTOM_BYTES:
                raise ValueError("файл голоса слишком большой")
            onnx, js, name_src = path, json_path or find_json_for(path), path
        else:
            raise ValueError("нужен файл голоса Piper: .onnx (и .onnx.json рядом) или .zip с ними")
        if not js or not os.path.isfile(js):
            raise NeedJson("не нашёл файл настроек голоса (.onnx.json) рядом с моделью")
        try:
            with open(js, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            raise ValueError(f"файл настроек голоса не читается: {e}")
        if not isinstance(cfg, dict) or "phoneme_id_map" not in cfg or not (cfg.get("audio") or {}).get("sample_rate"):
            raise ValueError("это не настройки голоса Piper (нет phoneme_id_map / sample_rate)")
        code, full = lang_from_config(cfg)
        # really load it and say something
        from .lang import LANGS, preview_text
        phrase = preview_text(code, "Jarvis") if code in LANGS else "Hello. Test."
        t0 = time.monotonic()
        eng = PiperTTS(threads=threads)
        try:
            v = eng._load_files(onnx, cfg)
            pcm, sr = eng._synth_with(v, phrase, None, 0)
        except Exception as e:
            raise RuntimeError(f"голос не запускается: {type(e).__name__}: {str(e)[:200]}")
        if pcm.size < sr * 0.3 or float(abs(pcm.astype("int32")).max()) < 200:
            raise RuntimeError("голос загрузился, но вместо речи тишина — файл не подходит")
        test_sec = time.monotonic() - t0
        slug = _slug(name_src)
        ensure_dir(custom_root)
        dest_dir, n = os.path.join(custom_root, slug), 2
        while os.path.exists(dest_dir):
            dest_dir = os.path.join(custom_root, f"{slug}-{n}")
            n += 1
        name = os.path.basename(dest_dir)
        tmp_dir = dest_dir + ".part"
        ensure_dir(tmp_dir)
        shutil.copyfile(onnx, os.path.join(tmp_dir, f"{name}.onnx"))
        if not cfg.get("jarvis_label"):
            cfg["jarvis_label"] = (_base(name_src).strip() or name)[:60]
        with open(os.path.join(tmp_dir, f"{name}.onnx.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        os.replace(tmp_dir, dest_dir)
        key = CUSTOM_PREFIX + name
        log.info("custom voice imported: %s (%s, %.1f MB, test %.2fs)", key, full, os.path.getsize(onnx) / 1e6, test_sec)
        meta = custom_meta(key, custom_root) or {"key": key, "lang": code}
        return dict(meta, test_sec=round(test_sec, 2), audio_sec=round(pcm.size / sr, 2))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def delete_custom(key: str, custom_root: str = CUSTOM_DIR) -> bool:
    if not is_custom(key) or not KEY_RE.fullmatch(key):
        return False
    d = os.path.join(custom_root, key[len(CUSTOM_PREFIX):])
    if not os.path.isdir(d) or os.path.dirname(os.path.abspath(d)) != os.path.abspath(custom_root):
        return False
    shutil.rmtree(d, ignore_errors=True)
    log.info("custom voice deleted: %s", key)
    return not os.path.exists(d)


class PiperTTS:
    """Keeps up to two loaded voices (e.g. Russian + English); synth(text, key) → (int16 mono, sample_rate)."""

    MAX_LOADED = 2

    def __init__(self, root: str = PIPER_DIR, threads: int | None = None, custom_root: str = CUSTOM_DIR) -> None:
        self.root = root
        self.custom_root = custom_root
        self.threads = threads or max(1, min(4, (os.cpu_count() or 4) // 2))  # leave cores for games
        self._voices: dict[str, object] = {}  # onnx path → PiperVoice (insertion order = LRU)
        self._lock = threading.Lock()

    def ready(self, key: str) -> bool:
        return installed(key, self.root, self.custom_root) and available()

    def loaded(self, key: str) -> bool:
        return voice_files(key, self.root, self.custom_root)[0] in self._voices

    def _load_files(self, mpath: str, cfg_dict: dict):
        import onnxruntime as ort
        from pathlib import Path
        from piper import PiperVoice
        from piper.config import PiperConfig
        cfg = PiperConfig.from_dict(cfg_dict)
        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        sess = ort.InferenceSession(mpath, sess_options=so, providers=["CPUExecutionProvider"])
        kw = {}
        ed = espeak_dir()
        if ed:
            kw["espeak_data_dir"] = Path(ed)
        return PiperVoice(session=sess, config=cfg, **kw)

    def load(self, key: str):
        mpath, jpath = voice_files(key, self.root, self.custom_root)
        with self._lock:
            v = self._voices.get(mpath)
            if v is not None:
                self._voices[mpath] = self._voices.pop(mpath)  # most recently used
                return v
            t0 = time.monotonic()
            with open(jpath, "r", encoding="utf-8") as f:
                v = self._load_files(mpath, json.load(f))
            warm = {"ru": "Да.", "uk": "Так.", "de": "Ja.", "pl": "Tak."}.get(voice_lang(key) or "", "Ok.")
            self._synth_raw(v, warm, speaker_of(key))  # warm-up: espeak init + first inference
            while len(self._voices) >= self.MAX_LOADED:
                self._voices.pop(next(iter(self._voices)))
            self._voices[mpath] = v
            log.info("piper voice %s loaded in %.2fs (%d threads)", key, time.monotonic() - t0, self.threads)
            return v

    @staticmethod
    def _synth_raw(v, text: str, speaker: int | None, length_scale: float | None = None):
        from piper import SynthesisConfig
        from piper.config import PhonemeType
        if v.config.phoneme_type == PhonemeType.TEXT:
            text = text.lower()  # text-phoneme voices (uk ukrainian_tts) only know lowercase letters
        if speaker is not None and (v.config.num_speakers or 1) <= speaker:
            speaker = None
        cfg = SynthesisConfig(speaker_id=speaker, length_scale=length_scale)
        return list(v.synthesize(text, cfg))

    def _synth_with(self, v, text: str, speaker: int | None, rate: int) -> tuple[np.ndarray, int]:
        ls = 1.0 / (1.0 + max(-50, min(50, int(rate))) / 100.0)
        chunks = self._synth_raw(v, text, speaker, ls * float(v.config.length_scale or 1.0))
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

    def synth(self, text: str, key: str, rate: int = 0) -> tuple[np.ndarray, int]:
        v = self.load(key)
        with self._lock:
            return self._synth_with(v, text, speaker_of(key), rate)
