"""JarvisCore: glue between voice I/O, the brain, focus session, focus guard,
reminders and settings. GUI-agnostic; reports to the UI via emit(event, **data)."""

from __future__ import annotations

import itertools
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime

from . import providers as P
from .actions import Actions
from .audio import MicHub, Recording, SentenceSplitter, Speaker, VoiceTurn, beep
from .brain import Brain, TurnCancelled, context_line
from . import lang as L
from . import stt_local, stt_whisper, tts_local
from .stt_normalize import normalize_transcript
from .stt_commands import english_hint, pick_bilingual, voice_names
from . import persona
from .config import load_settings, normalize_settings, register_secrets, save_settings
from .converse import FollowListener, FollowSession, echo_match, fmt_left, is_stop_phrase, junk_reason
from .pc_audio import PCHearing, backend_available as pc_backend_available
from .focus import FocusSession, Stats, fmt_clock
from .hotkey import Hotkey
from .live import LivePolicy, build_prompt, live_intent, talk_params
from .local_ai import LocalController
from .watcher import (Foreground, NudgePolicy, capture_screen_jpeg, get_foreground, is_call,
                      is_fullscreen, match_distraction, nudge_text)

log = logging.getLogger("jarvis")

# Spoken right away when the model calls a tool before saying anything (voice turns): the tool + the
# second model round take ~1 s more, the user should hear that we are on it.
TOOL_ACK = {"open_app": "Открываю.", "open_url": "Открываю.", "open_path": "Открываю.", "web_search": "Ищу.",
            "find_files": "Ищу файлы.", "start_focus": "Запускаю фокус.", "stop_focus": "Останавливаю.",
            "set_reminder": "Ставлю напоминание.", "cancel_reminders": "Отменяю.", "look_at_screen": "Смотрю на экран."}


# «хватит» in conversation mode: short confirmation in the reply language
FOLLOW_BYE = {"ru": "Хорошо, не слушаю. Позови по имени.", "uk": "Добре, не слухаю. Поклич на ім'я.",
              "en": "Okay, I'll stop listening. Call me by name.", "de": "Okay, ich höre nicht mehr zu.",
              "pl": "Dobrze, nie słucham. Zawołaj mnie po imieniu."}
TTS_TAIL_SEC = 0.5   # the room echo of her last word: the mic is ignored a bit after TTS ends


def _same_ack(sentence: str, ack: str) -> bool:
    """«Открываю ютуб.» right after our own «Открываю.» would sound like a stutter."""
    w = sentence.lower().split()
    a = ack.lower().split()
    return bool(w and a) and len(w) <= 5 and w[0][:5] == a[0][:5]


class JarvisCore:
    def __init__(self, emit) -> None:
        self.emit = emit
        self.settings = load_settings()
        register_secrets(self.settings)
        self.stats = Stats()
        self.session = FocusSession()
        self.mic = MicHub()
        self.piper = tts_local.PiperTTS()
        self.user_lang: str | None = None  # language of his last message (for «Как я спросил»)
        self.speaker = Speaker(lambda: self.settings, on_state=self._on_speaking, on_turn=self._on_turn,
                               piper=self.piper, lang_pref=self._reply_lang, on_voice_missing=self._voice_missing)
        self._turn: VoiceTurn | None = None
        self._answering = False
        self._piper_dl: list[str] = []
        self._piper_err: str | None = None
        self._piper_failed: set[str] = set()
        self._stt_dl: str | None = None
        self._stt_err: str | None = None
        self._whisper_dl: str | None = None    # model being downloaded ("" while only cuBLAS is)
        self._whisper_err: str | None = None
        self.last_timing: dict | None = None
        self.actions = Actions(self)
        self.brain = Brain(lambda: self.settings, self.actions, self._context)
        # companion memory: %LOCALAPPDATA%\Jarvis\memory.json (facts, preferences, people, projects)
        try:
            from .memory import MemoryStore
            self.memory = MemoryStore(get_settings=lambda: self.settings,
                                      on_change=lambda what, n: self.emit("memory", what=what, count=n))
        except Exception:
            log.exception("memory failed to start")
            self.memory = None
        self.brain.memory = self.memory
        # v1.5: local model (Ollama) — routing, warm-up, «free the GPU in games»
        self.local = LocalController(lambda: self.settings, emit=self.emit, foreground=self._fg_for_games)
        self.brain.local = self.local
        self.policy = NudgePolicy(cooldown=self.settings["nudge_cooldown_sec"],
                                  grace=self.settings["distraction_grace_sec"])
        self.state = "idle"
        self._jobs: queue.Queue = queue.Queue()
        self._rec: Recording | None = None
        self._rec_mode: str | None = None
        self._rec_lock = threading.Lock()
        self.reminders: list[dict] = []
        self._rem_ids = itertools.count(1)
        self._rem_lock = threading.Lock()
        self.hotkey: Hotkey | None = None
        self.wake = None
        self.last_fg = Foreground()
        self._stop = threading.Event()
        # guard bookkeeping
        self._last_title_check = 0.0
        self._last_screen_check = 0.0
        self._screen_inflight = False
        self._screen_backoff_until = 0.0
        self._screen_fail = 0
        self._screen_offtask_until = 0.0
        self._title_hit_until = 0.0
        self._episode_counted = False
        self.guard_status = "Страж ждёт фокус-сессию"
        # live mode bookkeeping
        self.live = LivePolicy()
        self._live_inflight = False
        self._live_fg_at = 0.0
        self._live_fg = Foreground()
        self._live_fullscreen = False
        self.live_status = "Выключен"
        self._wake_dl = False
        if self.settings["live_mode"]:
            self.live.reset(time.monotonic())
            self.live_status = "Включён — присматриваюсь"
        self._init_listen_state()

    def _init_listen_state(self) -> None:
        """v1.6 conversation mode («Слушать после ответа») + «Слышать звук ПК» (no threads started here)."""
        self._answering = getattr(self, "_answering", False)
        self.follow = FollowSession(self.settings.get("follow_mode", "2m"))
        self.follow_listener: FollowListener | None = None
        self._follow_busy = False          # a follow-up phrase is being recognised / answered
        self._tts_tail_until = 0.0
        self._last_reply = ""              # her last reply (echo filter for the «всегда» mode)
        self._follow_emitted: tuple | None = None
        self._listen_lock = threading.Lock()
        self.pc = PCHearing(lambda: self.settings, transcribe=self._pc_transcribe, foreground=self._fg_title,
                            on_status=lambda _t: self.emit("pc_hearing", **self.pc_hearing_info()))

    # ── lifecycle ──
    def start(self) -> None:
        threading.Thread(target=self._worker, name="brain-worker", daemon=True).start()
        threading.Thread(target=self._ticker, name="ticker", daemon=True).start()
        self._apply_hotkey()
        self._apply_wake()
        self._apply_follow()
        ok, err = pc_backend_available()
        log.info("pc hearing: loopback backend %s%s", "import OK" if ok else "unavailable", "" if ok else f" ({err})")
        if self.settings.get("pc_hearing"):
            self.pc.start()
        threading.Thread(target=self._prepare_voice, name="voice-prepare", daemon=True).start()
        self.local.start()
        log.info("core started; provider=%s chat=%s lite=%s; ai_route=%s local=%s", self.settings["provider"],
                 *self.brain.models(), self.settings["ai_route"], self.local.model())

    def _fg_for_games(self) -> tuple[str, str, bool, int]:
        fg = get_foreground()
        if not fg.process or fg.pid == os.getpid():
            return "", "", False, 0
        return fg.process, fg.path, is_fullscreen(fg), fg.pid

    def local_info(self) -> dict:
        return self.local.state()

    # ── fast voice (v1.3) + languages (v1.4): local TTS + local STT preparation ──
    def _reply_lang(self) -> str:
        return L.reply_lang(self.settings, self.user_lang)

    def _prepare_voice(self) -> None:
        """Warm up what the next voice turn needs, so the first answer is not slower than the rest."""
        s = self.settings
        if s["tts_engine"] == "piper":
            self.ensure_piper_voice(tts_local.voice_for_lang(s, self._reply_lang()))
        if s["stt_mode"] == "local":
            self._ensure_stt_model()
            self._ensure_whisper()

    def ensure_piper_voice(self, key: str) -> None:
        if not key:
            return
        if not tts_local.available():
            self._piper_err = "движок Piper не найден в сборке"
            return
        if tts_local.installed(key):
            try:
                self.piper.load(key)
                self._piper_err = None
            except Exception as e:
                self._piper_err = f"не удалось загрузить голос: {e}"
                log.exception("piper load failed")
            self._emit_tts()
            return
        self.download_voice(key)

    def _voice_missing(self, key: str) -> None:
        """Speaker needed a voice that is not on disk (reply in a new language): fetch it once."""
        if key in tts_local.PIPER_VOICES and key not in self._piper_dl and key not in self._piper_failed:
            log.info("voice %s needed for %s — downloading", key, tts_local.voice_lang(key))
            self.download_voice(key)

    def download_voice(self, key: str) -> bool:
        if key not in tts_local.PIPER_VOICES:
            return False
        if key in self._piper_dl:
            return True
        if any(tts_local.file_key(k) == tts_local.file_key(key) for k in self._piper_dl):
            return True  # same file (multi-speaker voice) is already on its way
        self._piper_dl.append(key)
        self._piper_err = None
        self._emit_tts()

        def work() -> None:
            try:
                log.info("downloading piper voice %s", key)
                tts_local.download_voice(key, lambda d, t: self.emit("tts_dl", key=key, done=d, total=t))
                self._piper_failed.discard(key)
                s = self.settings
                if s["tts_engine"] == "piper" and key == tts_local.voice_for_lang(s, self._reply_lang()):
                    self.piper.load(key)
            except Exception as e:
                self._piper_failed.add(key)
                self._piper_err = f"не удалось скачать голос: {e}"
                log.error("piper voice download failed: %s", e)
            finally:
                if key in self._piper_dl:
                    self._piper_dl.remove(key)
            self._emit_tts()
        threading.Thread(target=work, name="piper-download", daemon=True).start()
        return True

    def import_voice(self, path: str, json_path: str | None = None) -> dict:
        """«Загрузить свой голос»: validate + copy, then use it for its language."""
        try:
            meta = tts_local.import_voice(path, json_path)
        except tts_local.NeedJson as e:
            return {"ok": False, "need_json": True, "error": str(e)}
        except Exception as e:
            log.warning("custom voice import failed: %s", e)
            return {"ok": False, "error": str(e)}
        code = meta.get("lang")
        new = dict(self.settings)
        if code == "ru":
            new["piper_voice"] = meta["key"]
        elif code in L.LANGS:
            new["piper_voices"] = dict(new.get("piper_voices") or {}, **{code: meta["key"]})
        if code in L.LANGS:
            self.apply_settings(new)
        self._emit_tts()
        return {"ok": True, "voice": meta, "supported": code in L.LANGS,
                "lang_label": L.LANGS[code]["label"] if code in L.LANGS else meta.get("lang_full", code)}

    def delete_voice(self, key: str) -> dict:
        if not tts_local.is_custom(key):
            return {"ok": False, "error": "удалять можно только свои голоса"}
        mpath = tts_local.voice_files(key)[0]
        with self.piper._lock:
            self.piper._voices.pop(mpath, None)  # release the file (Windows keeps it locked otherwise)
        ok = tts_local.delete_custom(key)
        new = dict(self.settings)
        if new.get("piper_voice") == key:
            new["piper_voice"] = tts_local.DEFAULT_VOICE
        new["piper_voices"] = {k: v for k, v in (new.get("piper_voices") or {}).items() if v != key}
        self.apply_settings(new)
        self._emit_tts()
        return {"ok": ok}

    def tts_info(self) -> dict:
        s = self.settings
        rl = self._reply_lang()
        return {"engine": s["tts_engine"], "voice": s["piper_voice"], "voices": tts_local.voices_payload(),
                "voice_for": {c: tts_local.voice_for_lang(s, c) for c in L.CODES}, "reply_lang": rl,
                "downloading": self._piper_dl[0] if self._piper_dl else None, "downloads": list(self._piper_dl),
                "error": self._piper_err, "available": tts_local.available(),
                "active": self.speaker.engine_for(s, rl), "stt": self.stt_info(), "timing": self.last_timing,
                "whisper": self.whisper_info(),
                "langs": L.payload(), "links": list(tts_local.LINKS)}

    def _emit_tts(self) -> None:
        self.emit("tts", **self.tts_info())

    def stt_info(self) -> dict:
        code = L.speech_lang(self.settings)
        return {"mode": self.settings["stt_mode"], "lang": code, "model": bool(stt_local.model_path(code)),
                "loaded": stt_local.is_loaded(code),
                "downloading": self._wake_dl if code == "ru" else self._stt_dl == code,
                "error": self._stt_err, "mb": round(L.LANGS[code]["vosk"][1] / 1e6)}

    def _ensure_stt_model(self) -> None:
        code = L.speech_lang(self.settings)
        path = stt_local.model_path(code)
        if path:
            try:
                t0 = time.monotonic()
                stt_local.get_model(path)
                log.info("local stt model (%s) ready in %.2fs", code, time.monotonic() - t0)
            except Exception as e:
                log.error("local stt model load failed: %s", e)
            return
        if code == "ru":
            if not self._wake_dl:
                self._wake_dl = True
                threading.Thread(target=self._download_vosk, name="vosk-download", daemon=True).start()
                self._emit_wake()
            return
        if self._stt_dl == code:
            return
        self._stt_dl, self._stt_err = code, None
        self._emit_tts()

        def work() -> None:
            try:
                log.info("downloading vosk model for %s", code)
                stt_local.download(code, lambda d, t: self.emit("stt_dl", lang=code, done=d, total=t))
                self._stt_dl = None
                if L.speech_lang(self.settings) == code and self.settings["stt_mode"] == "local":
                    self._ensure_stt_model()
            except Exception as e:
                self._stt_dl = None
                self._stt_err = f"не удалось скачать модель распознавания: {e}"
                log.error("vosk %s download failed: %s", code, e)
            self._emit_tts()
        threading.Thread(target=work, name=f"vosk-download-{code}", daemon=True).start()

    # ── v1.5.x optional Whisper (faster-whisper) STT ──
    def _whisper_wanted(self) -> bool:
        s = self.settings
        return s["stt_mode"] == "local" and s.get("stt_engine") == "whisper"

    def _whisper_ready(self) -> bool:
        return self._whisper_wanted() and stt_whisper.engine().ready(self.settings.get("whisper_model"))

    def whisper_info(self) -> dict:
        s = self.settings
        try:
            eng = stt_whisper.engine().info()
            # cheap on the UI thread: the real import / nvidia-smi run in _ensure_whisper (background)
            available = stt_whisper.package_present()
            gpu = stt_whisper.nvidia_gpu(probe=False)
            cuda = stt_whisper.cuda_installed()
            catalog = stt_whisper.catalog()
        except Exception as e:  # never break the settings page because of the optional engine
            log.warning("whisper info failed: %s", e)
            eng, available, gpu, cuda, catalog = {}, False, "", False, []
        return {"engine": s.get("stt_engine", "vosk"), "model": s.get("whisper_model"),
                "device_pref": s.get("whisper_device"), "lang_mode": s.get("whisper_lang"),
                "available": available, "import_error": "" if available else stt_whisper.import_error(),
                "gpu": gpu, "cuda": cuda, "catalog": catalog, "downloading": self._whisper_dl,
                "error": self._whisper_err or eng.get("error") or None, **{k: eng.get(k) for k in (
                    "loaded", "device", "compute", "loading", "gpu_error", "load_sec")},
                "loaded_model": eng.get("model", "")}

    def _emit_whisper(self) -> None:
        self.emit("whisper", **self.whisper_info())

    def _ensure_whisper(self) -> None:
        """engine = whisper and the model is on disk → load it (GPU if possible). Never downloads by itself:
        1.6 GB (+0.55 GB cuBLAS) only after «Скачать» in the settings."""
        if not self._whisper_wanted():
            return
        s = self.settings
        name = s.get("whisper_model")
        eng = stt_whisper.engine()
        stt_whisper.nvidia_gpu()  # probe once in the background so the settings page can show the GPU
        if eng.loading or not stt_whisper.available() or not stt_whisper.installed(name):
            self._emit_whisper()
            return
        if eng.ready(name) and s.get("whisper_device") in ("auto", eng.device):
            return
        self._whisper_err = None
        self._emit_whisper()
        eng.load(name, s.get("whisper_device", "auto"))
        self._emit_whisper()

    def _whisper_settings_changed(self, old: dict) -> None:
        s = self.settings
        keys = ("stt_mode", "stt_engine", "whisper_model", "whisper_device")
        if all(old.get(k) == s.get(k) for k in keys):
            return
        if not self._whisper_wanted():
            if stt_whisper.engine().model is not None:
                stt_whisper.engine().unload()  # frees 1.5–3 GB of VRAM/RAM for games and Ollama
                log.info("whisper unloaded (engine=%s, stt_mode=%s)", s.get("stt_engine"), s["stt_mode"])
            self._emit_whisper()
            return
        threading.Thread(target=self._ensure_whisper, name="whisper-load", daemon=True).start()

    def download_whisper(self, model: str | None = None) -> bool:
        """«Скачать» in the settings: the model (+ cuBLAS for an NVIDIA GPU) into %LOCALAPPDATA%\\Jarvis\\whisper."""
        name = stt_whisper.norm_model(model or self.settings.get("whisper_model"))
        if self._whisper_dl is not None:
            return True
        if not stt_whisper.available():
            self._whisper_err = "faster-whisper не входит в эту сборку — пересоберите exe с ним"
            self._emit_whisper()
            return False
        self._whisper_dl, self._whisper_err = name, None
        self._emit_whisper()

        def work() -> None:
            try:
                need_cuda = (self.settings.get("whisper_device") != "cpu" and stt_whisper.nvidia_gpu()
                             and not stt_whisper.cuda_installed() and not stt_whisper._cublas_on_path())
                if need_cuda:
                    stt_whisper.download_cuda(lambda d, t: self.emit("whisper_dl", stage="cuda", model=name,
                                                                     done=d, total=t))
                stt_whisper.download_model(name, lambda d, t: self.emit("whisper_dl", stage="model", model=name,
                                                                        done=d, total=t))
                self._whisper_dl = None
                if name != self.settings.get("whisper_model"):
                    new = dict(self.settings, whisper_model=name)
                    self.apply_settings(new)  # loads it if engine = whisper
                else:
                    self._ensure_whisper()
            except stt_whisper.Cancelled:
                self._whisper_err = "загрузка отменена"
            except Exception as e:
                self._whisper_err = f"не удалось скачать: {e}"[:220]
                log.error("whisper download failed: %s", e)
            finally:
                self._whisper_dl = None
            self._emit_whisper()
        threading.Thread(target=work, name="whisper-download", daemon=True).start()
        return True

    def cancel_whisper_download(self) -> None:
        stt_whisper.cancel_download()

    def delete_whisper(self, model: str) -> dict:
        name = stt_whisper.norm_model(model)
        eng = stt_whisper.engine()
        if eng.name == name:
            eng.unload()  # Windows keeps model.bin locked while loaded
        ok = stt_whisper.delete_model(name)
        self._emit_whisper()
        return {"ok": ok}

    def _whisper_transcribe(self, wav: bytes | None, turn: VoiceTurn) -> str:
        """Whisper pass for a finished phrase. '' = not ready / nothing / not confident → caller falls back."""
        if not wav or not self._whisper_ready():
            return ""
        s = self.settings
        code = L.speech_lang(s)
        t0 = time.monotonic()
        try:
            # no initial prompt: it made Whisper worse in tests; names are fixed by normalize_transcript later
            text, conf, lang = stt_whisper.engine().transcribe(wav, lang_mode=s.get("whisper_lang", "pair"),
                                                               speech_lang=code, prompt="")
        except Exception as e:
            log.warning("whisper transcribe failed: %s", e)
            return ""
        ms = 1000 * (time.monotonic() - t0)
        if not text or conf < 0.35:
            log.info("whisper not confident (%r, conf %.2f, %s) in %.0f ms → fallback", text[:80], conf, lang, ms)
            return ""
        log.info("whisper [%s/%s]: %r (conf %.2f, lang %s) in %.0f ms", stt_whisper.engine().name,
                 stt_whisper.engine().device, text[:120], conf, lang, ms)
        turn.stt, turn.lang = "whisper", lang or ""
        return text

    def shutdown(self) -> None:
        self._stop.set()
        try:
            if self.session.active:
                self.stats.add_focus(self.session.stop())
        except Exception:
            pass
        if self.hotkey:
            self.hotkey.stop()
        if self.wake:
            self.wake.stop()
        if self.follow_listener:
            self.follow_listener.stop()
        try:
            self.pc.stop()
        except Exception:
            pass
        if self._rec:
            self._rec.cancel()
        self.speaker.shutdown()
        self.mic.close()
        self._jobs.put(None)
        try:
            self.local.stop()
            self.local.shutdown()
        except Exception:
            pass
        phone = getattr(self, "phone", None)
        if phone is not None:
            phone.stop()

    # ── settings ──
    def apply_settings(self, new: dict) -> None:
        old = self.settings
        self.settings = normalize_settings(new)
        save_settings(self.settings)
        register_secrets(self.settings)
        self.policy.cooldown = self.settings["nudge_cooldown_sec"]
        self.policy.grace = self.settings["distraction_grace_sec"]
        if old["hotkey"] != self.settings["hotkey"]:
            self._apply_hotkey()
        if old["wake_mode"] != self.settings["wake_mode"]:
            self._apply_wake()
        elif self.wake is not None and self.settings["wake_mode"] == "name":
            if old["assistant_name"] != self.settings["assistant_name"] and hasattr(self.wake, "set_name"):
                self.wake.set_name(self.settings["assistant_name"])  # live, no restart
                self._emit_wake()
            if old["name_threshold"] != self.settings["name_threshold"] and hasattr(self.wake, "set_threshold"):
                self.wake.set_threshold(self.settings["name_threshold"])
        elif old["wake_threshold"] != self.settings["wake_threshold"] and self.settings["wake_mode"] == "hey_jarvis":
            self._apply_wake()
        if old["live_mode"] != self.settings["live_mode"]:
            self._live_switched(self.settings["live_mode"])
        s = self.settings
        if (old["ai_route"], old["models"]["ollama"], old["base_urls"]["ollama"]) != (
                s["ai_route"], s["models"]["ollama"], s["base_urls"]["ollama"]):
            threading.Thread(target=self.local.settings_changed, args=(old,), daemon=True).start()
        if old["answer_lang"] != s["answer_lang"]:
            self.user_lang = None
        voice_now = tts_local.voice_for_lang(s, self._reply_lang())
        if s["tts_engine"] == "piper" and (voice_now != tts_local.voice_for_lang(old, L.reply_lang(old, self.user_lang))
                                           or old["tts_engine"] != "piper" or old["piper_voice"] != s["piper_voice"]
                                           or old.get("piper_voices") != s.get("piper_voices")):
            threading.Thread(target=self.ensure_piper_voice, args=(voice_now,), daemon=True).start()
        if s["stt_mode"] == "local" and (old["stt_mode"] != "local" or old["speech_lang"] != s["speech_lang"]):
            threading.Thread(target=self._ensure_stt_model, daemon=True).start()
        self._whisper_settings_changed(old)
        if s.get("stt_en_model") == "lgraph" and old.get("stt_en_model") != "lgraph" and s.get("stt_en_pass", True):
            threading.Thread(target=stt_local.english_model, kwargs={"variant": "lgraph"},
                             name="vosk-en-lgraph", daemon=True).start()  # 128 MB, once, in the background
        if old["vad_silence_ms"] != s["vad_silence_ms"] and self.wake is not None and hasattr(self.wake, "set_hang"):
            self.wake.set_hang(s["vad_silence_ms"] / 1000.0)
        if old["vad_silence_ms"] != s["vad_silence_ms"] and self.follow_listener is not None:
            self.follow_listener.set_hang(s["vad_silence_ms"] / 1000.0)
        if old.get("follow_mode") != s.get("follow_mode"):
            self.follow.configure(s["follow_mode"])
            log.info("conversation mode: %s", s["follow_mode"])
            self._apply_follow()
        if bool(old.get("pc_hearing")) != bool(s.get("pc_hearing")):
            if s.get("pc_hearing"):
                self.pc.start()
            else:
                self.pc.stop()
            log.info("pc hearing %s", "on" if s.get("pc_hearing") else "off")
        if self.memory is not None and s.get("memory_max_facts", 100) < old.get("memory_max_facts", 100):
            try:
                self.memory.enforce_limit()
            except Exception:
                log.exception("memory limit failed")
        self._screen_backoff_until = 0.0
        self._screen_fail = 0
        log.info("settings applied; provider=%s chat=%s lite=%s", self.settings["provider"],
                 *self.brain.models())

    def _apply_hotkey(self) -> None:
        if self.hotkey:
            self.hotkey.stop()
            self.hotkey = None
        hk = Hotkey(self.settings["hotkey"], self.hotkey_down, self.hotkey_up)
        if hk.start():
            self.hotkey = hk
        else:
            self.emit("error", text=f"Не удалось назначить горячую клавишу {self.settings['hotkey']}")

    def wake_info(self, error: str | None = None) -> dict:
        mode = self.settings["wake_mode"]
        return {"enabled": self.wake is not None, "mode": mode, "error": error,
                "downloading": self._wake_dl, "phrase": (persona.assistant_name(self.settings) if mode == "name"
                                                         else "Hey Jarvis" if mode == "hey_jarvis" else "")}

    def _emit_wake(self, error: str | None = None, **extra) -> None:
        self.emit("wake", **self.wake_info(error), **extra)

    def _apply_wake(self) -> None:
        if self.wake:
            self.wake.stop()
            self.wake = None
        mode = self.settings["wake_mode"]
        if mode == "off":
            self._emit_wake()
            return
        if mode == "name":
            from . import namewake
            path = namewake.find_model()
            if not path:
                if not self._wake_dl:
                    self._wake_dl = True
                    threading.Thread(target=self._download_vosk, name="vosk-download", daemon=True).start()
                self._emit_wake()
                return
            try:
                self.wake = namewake.NameWakeListener(self.mic, self.wake_detected, self.settings["assistant_name"],
                                                      threshold=self.settings["name_threshold"], model_path=path,
                                                      hang_sec=self.settings["vad_silence_ms"] / 1000.0)
                self.wake.start()
                threading.Thread(target=self._check_wake_started, args=(self.wake,), daemon=True).start()
                self._emit_wake()
            except Exception as e:
                log.error("name wake unavailable: %s", e)
                self.wake = None
                self._emit_wake(str(e))
            return
        try:
            from .wakeword import WakeWordListener
            self.wake = WakeWordListener(self.mic, self.wake_detected,
                                         threshold=self.settings["wake_threshold"])
            self.wake.start()
            self._emit_wake()
        except Exception as e:
            log.error("wake word unavailable: %s", e)
            self.wake = None
            self._emit_wake(str(e))

    def _check_wake_started(self, listener) -> None:
        self._sync_listen()
        listener.ready.wait(30)
        if listener.error and self.wake is listener:
            self.wake = None
            self._emit_wake(listener.error)

    def _download_vosk(self) -> None:
        from . import namewake
        try:
            log.info("downloading vosk model %s", namewake.VOSK_MODEL_URL)
            namewake.download_model(lambda d, t: self.emit("wake_dl", done=d, total=t))
            self._wake_dl = False
            if self.settings["wake_mode"] == "name" and not self._stop.is_set():
                self._apply_wake()
            else:
                self._emit_wake()
            if self.settings["stt_mode"] == "local":
                self._ensure_stt_model()
            self._emit_tts()
        except Exception as e:
            self._wake_dl = False
            log.error("vosk model download failed: %s", e)
            self._emit_wake(f"не удалось скачать модель распознавания: {e}")

    # ── state ──
    def set_state(self, st: str, detail: str = "") -> None:
        self.state = st
        self.emit("state", state=st, detail=detail)

    def _on_speaking(self, speaking: bool) -> None:
        if not speaking:
            self._tts_tail_until = time.monotonic() + TTS_TAIL_SEC
            t = threading.Timer(TTS_TAIL_SEC + 0.05, self._sync_listen)  # resume listening right after the tail
            t.daemon = True
            t.start()
        self._sync_listen()
        if speaking:
            if self.state != "listening":
                self.set_state("speaking")
        elif self.state == "speaking" or (self.state == "thinking" and not self._answering):
            self.set_state("idle")

    def _on_turn(self, turn: VoiceTurn) -> None:
        """First audio of a reply started: log the latency breakdown."""
        bd = turn.breakdown()
        bd["source"] = turn.source
        bd["engine"] = self.speaker.last_engine
        if turn.source == "voice":
            self.last_timing = bd
        log.info("voice timing [%s, stt=%s, tts=%s, %s]: endpoint %s + stt %s + llm first token %s "
                 "(first sentence %s) + tts %s + play %s = %s s from end of speech to first sound "
                 "(text→sound %s s)", turn.source, bd["stt_mode"] or "-", bd["engine"], bd["model"] or "-",
                 bd["endpoint"], bd["stt"], bd["llm_first"], bd["first_sentence"], bd["tts"], bd["play"],
                 bd["first_audio"], bd["text_to_audio"])
        if turn.source == "voice":
            self.emit("voice_timing", **bd)

    def _interrupt(self) -> None:
        """New request / stop: silence now and abort a reply that is still streaming."""
        self.speaker.stop()
        t = self._turn
        if t is not None:
            t.cancelled = True

    # ── voice input ──
    def hotkey_down(self) -> None:
        with self._rec_lock:
            if self._rec is not None:
                self._rec.stop()  # second press ends a toggle-mode recording
                return
        self._interrupt()
        self._start_recording("hold")

    def hotkey_up(self, held: float) -> None:
        with self._rec_lock:
            rec = self._rec
            if rec is None or self._rec_mode != "hold":
                return
            if held < 0.4:
                rec.auto_stop = True  # quick tap → listen until a pause
                self._rec_mode = "auto"
            else:
                rec.stop()

    def toggle_listen(self) -> None:
        with self._rec_lock:
            if self._rec is not None:
                self._rec.stop()
                return
        self._interrupt()
        self._start_recording("auto")

    def wake_detected(self, request_wav: bytes | None = None, det: dict | None = None) -> None:
        if self._rec is not None or self.state == "thinking":
            return
        self._interrupt()
        self.local.warm_async()
        det = det or {}
        if request_wav or det.get("request_text"):
            # «Пятница, открой телеграм» in one breath: the request is already recorded (and recognised)
            beep("listen")
            turn = VoiceTurn("voice")
            turn.mark("speech_end", det.get("t_speech_end"))
            turn.mark("rec_end")
            text = det.get("request_text") or ""
            code = L.speech_lang(self.settings)
            local = self.settings["stt_mode"] == "local"
            rescued = ""
            if request_wav and self._whisper_ready():
                # Whisper decodes the request in the worker; the name-spotter's Vosk text is the fallback
                turn.fallback = text if code == "ru" and stt_local.acceptable(text, det.get("request_conf", 0.0)) else ""
                self.set_state("thinking", "Распознаю…")
                self._jobs.put(("voice", (turn, request_wav, "")))
                return
            if local and code == "ru" and not stt_local.acceptable(text, det.get("request_conf", 0.0)):
                rescued = self._english_rescue(text, request_wav)  # «Джарвис, райт … он телеграм …»
            if rescued:
                text = rescued
                turn.stt = "wake-local"
                turn.mark("text")
            elif local and code == "ru" and stt_local.acceptable(text, det.get("request_conf", 0.0)):
                turn.stt = "wake-local"
                turn.mark("text")
            elif local and code != "ru" and request_wav and stt_local.is_loaded(code):
                # the name is spotted by the Russian model; the request itself is in his language
                try:
                    text, conf = stt_local.transcribe_wav(stt_local.get_model(stt_local.model_path(code)), request_wav)
                except Exception as e:
                    log.warning("wake request local stt (%s) failed: %s", code, e)
                    text, conf = "", 0.0
                if stt_local.acceptable(text, conf):
                    turn.stt = "wake-local"
                    turn.mark("text")
                else:
                    text = ""
            else:
                text = ""
            self.set_state("thinking", "Думаю…" if text else "Распознаю…")
            self._jobs.put(("voice", (turn, request_wav, text)))
            return
        self._start_recording("auto")

    def _start_recording(self, mode: str, no_speech_timeout: float | None = None) -> None:
        with self._rec_lock:
            if self._rec is not None:
                return
            self.local.warm_async()  # he is talking: load the local model now (no-op if loaded / gaming)
            kw = {"no_speech_timeout": float(no_speech_timeout)} if no_speech_timeout else {}
            stt = None
            if self.settings["stt_mode"] == "local":
                code = L.speech_lang(self.settings)
                path = stt_local.model_path(code)
                if path and stt_local.is_loaded(code):
                    try:
                        stt = stt_local.StreamingSTT(stt_local.get_model(path))
                    except Exception as e:
                        log.warning("local stt unavailable: %s", e)
                else:  # loads in ~0.5 s (or downloads once); this turn goes to the cloud, the next one is local
                    threading.Thread(target=self._ensure_stt_model, daemon=True).start()
            rec = Recording(self.mic, auto_stop=(mode in ("auto", "reply")),
                            on_level=lambda lv: self.emit("level", level=lv), stt=stt,
                            silence_sec=self.settings["vad_silence_ms"] / 1000.0, **kw)
            self._rec = rec
            self._rec_mode = mode
        self._sync_listen()
        self.set_state("listening")
        beep("listen")
        threading.Thread(target=self._record_thread, args=(rec, mode), name="recorder", daemon=True).start()

    def _record_thread(self, rec: Recording, mode: str = "") -> None:
        wav = None
        try:
            wav = rec.run()
        except Exception as e:
            self.emit("chat", role="system", text="Микрофон недоступен. Проверь, что он подключён и разрешён в Windows.")
            log.error("recording failed: %s", e)
        finally:
            with self._rec_lock:
                self._rec = None
                self._rec_mode = None
            self._sync_listen()
        beep("stop")
        if wav and rec.speech_detected:
            turn = VoiceTurn("voice")
            turn.mark("speech_end", rec.t_speech_end)
            turn.mark("rec_end", rec.t_end)
            text = ""
            if self._whisper_ready():
                # Whisper decodes the whole phrase in the worker; Vosk's streaming text is only the fallback
                turn.fallback = rec.text if rec.stt is not None and stt_local.acceptable(rec.text, rec.conf) else ""
            elif rec.stt is not None and stt_local.acceptable(rec.text, rec.conf):
                text = rec.text
                turn.stt = "local"
                turn.mark("text")
            elif rec.stt is not None:
                text = self._english_rescue(rec.text, wav)
                if text:
                    turn.stt = "local"
                    turn.mark("text")
                else:
                    log.info("local stt not confident (%r, conf %.2f) → cloud", rec.text[:80], rec.conf)
            self.set_state("thinking", "Думаю…" if text else "Распознаю…")
            self._jobs.put(("voice", (turn, wav, text)))
        else:
            self.set_state("idle", "Не расслышал" if not rec.cancelled and mode != "reply" else "")

    def submit_text(self, text: str) -> None:
        t = (text or "").strip()
        if t:
            self._interrupt()
            self._jobs.put(("text", t))

    def say(self, text: str, *, interrupt: bool = False, lang: str | None = None) -> None:
        self.speaker.say(text, interrupt=interrupt, lang=lang)

    # ── worker ──
    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            kind, payload = job
            try:
                if kind == "voice":
                    turn, wav, text = payload
                    code = L.speech_lang(self.settings)
                    if not text and wav and self._whisper_ready():
                        self.set_state("thinking", "Распознаю…")
                        text = self._whisper_transcribe(wav, turn)
                        if not text and turn.fallback:
                            text, turn.stt = turn.fallback, "local"
                        if text:
                            turn.mark("text")
                    if not text:
                        self.set_state("thinking", "Распознаю…")
                        turn.stt = "cloud"
                        text = self.brain.transcribe(wav, lang=code)
                        turn.mark("text")
                        if not text:
                            self.set_state("idle", "Не расслышал")
                            continue
                        self.user_lang = L.detect(text, prefer=code)
                    elif turn.stt == "whisper":
                        self.user_lang = turn.lang if turn.lang in L.LANGS else L.detect(text, prefer=code)
                    else:
                        self.user_lang = code  # the local model only knows its own language
                    # Fix Vosk mangling of English brand names / spoken English commands before brain/tools
                    # (Whisper: only the dictionary fixes — its English is fine, no second Vosk pass)
                    text = self._clean_voice_text(text, wav, code, local=turn.stt not in ("cloud", "whisper"))
                    if self.follow.active() and is_stop_phrase(text, persona.assistant_name(self.settings)):
                        self.emit("chat", role="user", text=text)
                        self._follow_end("voice", say=True)
                        continue
                    self.emit("chat", role="user", text=text)
                    self._answer(text, voice=True, turn=turn, follow_ok=True)
                elif kind == "follow":
                    try:
                        self._follow_job(*payload)
                    finally:
                        self._follow_busy = False
                        self._sync_listen()
                elif kind == "audio":  # legacy path (kept for tests/tools)
                    self.set_state("thinking", "Распознаю…")
                    text = normalize_transcript(self.brain.transcribe(payload), names=voice_names(self.settings),
                                                assistant_name=persona.assistant_name(self.settings))
                    if not text:
                        self.set_state("idle", "Не расслышал")
                        continue
                    self.emit("chat", role="user", text=text)
                    self._answer(text, voice=True)
                elif kind == "text":
                    self.user_lang = L.detect(payload, prefer=self.user_lang or L.speech_lang(self.settings))
                    self.emit("chat", role="user", text=payload)
                    self._answer(payload)
                elif kind == "phone":
                    text, speak, box = payload
                    try:
                        self.user_lang = L.detect(text, prefer=self.user_lang or L.speech_lang(self.settings))
                        self.emit("chat", role="user", text=text)
                        box["reply"] = self._answer(text, voice=bool(speak)) or ""
                    except Exception as e:
                        box["error"] = P.friendly_error(e)
                        self.emit("chat", role="system", text=box["error"])
                    finally:
                        box["event"].set()
            except TurnCancelled:
                log.info("reply cancelled by the user")
                self.emit("chat_partial", text="", done=True)
            except P.ProviderError as e:
                msg = P.friendly_error(e)
                log.warning("provider error: %s (%s)", e, getattr(e, "detail", "")[:200])
                self.emit("chat", role="system", text=msg)
                self.set_state("idle", "Ошибка")
                if self.settings["speak_replies"]:
                    self.say(msg)
            except Exception as e:
                log.exception("job failed")
                self.emit("chat", role="system", text=P.friendly_error(e))
                self.set_state("idle", "Ошибка")

    def _clean_voice_text(self, text: str, wav: bytes | None, code: str, *, local: bool) -> str:
        """Post-STT repair: brand names, spoken English command templates and — when the Russian
        (or Ukrainian) model obviously heard English — a second decode with the English model."""
        names = voice_names(self.settings)
        aname = persona.assistant_name(self.settings)
        raw = text
        hint = english_hint(text) if local and code in ("ru", "uk") and wav \
            and self.settings.get("stt_en_pass", True) and re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", text or "") else 0
        if hint:
            t0 = time.monotonic()
            # weak hint: only if already downloaded
            en = stt_local.english_pass(wav, allow_download=hint >= 2, variant=self.settings.get("stt_en_model", "small"))
            if en and en[0]:
                raw = pick_bilingual(text, en[0], en[1], names=names, assistant_name=aname)
                log.info("bilingual stt: ru %r / en %r (conf %.2f) → %r in %.0f ms", text[:80], en[0][:80], en[1],
                         raw[:80], 1000 * (time.monotonic() - t0))
            else:
                log.info("bilingual stt: English model not ready yet (ru %r)", text[:80])
        out = normalize_transcript(raw, names=names, assistant_name=aname)
        if out != text:
            log.info("stt fix: %r → %r", text[:120], out[:120])
        if local and code in ("ru", "uk") and re.search(r"[A-Za-z]", out) and not re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", out):
            self.user_lang = L.detect(out, prefer=code)  # the local model heard English this time (bilingual pass)
        return out

    def _english_rescue(self, ru_text: str, wav: bytes | None) -> str:
        """The Russian model was not confident, but what it heard looks like English («райт … он
        телеграм»): try the English model before paying for a cloud transcription. '' = no luck."""
        code = L.speech_lang(self.settings)
        if code not in ("ru", "uk") or not wav or not self.settings.get("stt_en_pass", True):
            return ""
        hint = english_hint(ru_text)
        if not hint:
            return ""
        en = stt_local.english_pass(wav, allow_download=hint >= 2, variant=self.settings.get("stt_en_model", "small"))
        if not en or not stt_local.acceptable(en[0], en[1], 0.45):
            return ""
        out = pick_bilingual(ru_text, en[0], en[1], names=voice_names(self.settings),
                             assistant_name=persona.assistant_name(self.settings))
        log.info("bilingual stt rescue: ru %r → en %r (conf %.2f) → %r", ru_text[:80], en[0][:80], en[1], out[:80])
        return out if not re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", out) else ""

    def _answer(self, text: str, *, voice: bool = False, turn: VoiceTurn | None = None, follow_ok: bool = False,
                overheard: str = "", before_output=None) -> str:
        """Stream the reply: each finished sentence goes to TTS while the model keeps writing.
        v1.6: follow_ok → a spoken turn opens/extends the conversation window; overheard → the phrase came
        without the name (the model may stay silent); before_output() runs once before anything is shown."""
        self.set_state("thinking", "Думаю…")
        turn = turn or VoiceTurn("text")
        turn.mark("text")
        self._turn = turn
        self._answering = True
        tools_used: list[str] = []
        speak = bool(self.settings["speak_replies"])
        turn_lang = self._reply_lang()
        stream = self.speaker.open_stream(turn) if speak else None
        if stream is not None:
            stream.lang = turn_lang
            self.speaker.prewarm()
        splitter = SentenceSplitter()
        parts: list[str] = []
        last_ui = [0.0]
        spoken = [0]
        acked: list[str] = []
        lock = threading.Lock()
        last_delta = [time.monotonic()]
        finished = threading.Event()
        shown = [before_output is None]

        def first_output() -> None:
            if not shown[0]:
                shown[0] = True
                try:
                    before_output()
                except Exception:
                    log.exception("before_output failed")

        def idle_flush() -> None:
            # the model went quiet mid-sentence (usually: now writing a tool call) → say what we have
            while not finished.wait(0.05):
                with lock:
                    if (splitter.buf.strip() and time.monotonic() - last_delta[0] > (0.3 if spoken[0] == 0 else 0.45)
                            and len(splitter.buf.split()) >= (1 if spoken[0] == 0 else 3) and not turn.cancelled):
                        for snt in splitter.flush():
                            speak_sentence(snt)

        def speak_sentence(snt: str) -> None:
            if acked and spoken[0] == 1 and _same_ack(snt, acked[0]):
                spoken[0] += 1
                return
            spoken[0] += 1
            stream.feed(snt)

        def on_text(delta: str) -> None:
            if turn.cancelled:
                raise TurnCancelled()
            if "llm_first" not in turn.t:
                turn.mark("llm_first")
                turn.model = self.brain.current_model
            first_output()
            parts.append(delta)
            now = time.monotonic()
            if now - last_ui[0] > 0.12:
                last_ui[0] = now
                self.emit("chat_partial", text="".join(parts))
            if stream is not None:
                with lock:
                    last_delta[0] = now
                    for snt in splitter.push(delta):
                        speak_sentence(snt)

        def on_tool(n, a):
            if turn.cancelled:
                raise TurnCancelled()
            tools_used.append(n)
            turn.model = turn.model or self.brain.current_model
            first_output()
            self.emit("tool", name=n, args=a)
            if stream is not None:
                with lock:
                    for snt in splitter.flush():  # say «Открываю…» before the tool runs
                        speak_sentence(snt)
                    a = L.ack(n, turn_lang)
                    if voice and spoken[0] == 0 and a:
                        acked.append(a)
                        speak_sentence(a)

        if stream is not None:
            threading.Thread(target=idle_flush, name="tts-idle-flush", daemon=True).start()
        try:
            reply = self.brain.ask(text, on_tool=on_tool, on_text=on_text, lang=turn_lang,
                                   fast=voice and bool(self.settings["fast_replies"]), voice=voice,
                                   overheard=overheard)
            turn.model = turn.model or self.brain.last_model
            finished.set()
            if stream is not None and not turn.cancelled:
                with lock:
                    for snt in splitter.flush():
                        speak_sentence(snt)
                if not parts and not acked and reply:  # nothing was streamed (tool-only answer → «Готово.»)
                    stream.feed(reply)
        finally:
            finished.set()
            if stream is not None:
                stream.end()
            self._answering = False
        if "set_live_mode" not in tools_used:
            want = live_intent(text)
            if want is not None and bool(self.settings.get("live_mode")) != want:
                log.info("live intent fallback: model did not call set_live_mode -> %s", want)
                self.set_live(want)
        silent = bool(overheard) and not reply and not tools_used
        if silent:
            if self.state == "thinking":
                self.set_state("idle")
            return ""
        first_output()
        self._pc_intent_fallback(text, tools_used)
        self.emit("chat", role="jarvis", text=reply)
        if reply:
            self._last_reply = reply
        if follow_ok and self.follow.start():
            self._sync_listen()
        if not speak or (not self.speaker.speaking and self.speaker.idle):
            if self.state == "thinking":
                self.set_state("idle")
        return reply

    def _pc_intent_fallback(self, text: str, tools_used: list) -> None:
        """The model answered without the right tool (e.g. «не могу выбрать песню») for a clear command:
        «включи <трек> в спотике» or «открой избранное в телеграме» → run the tool ourselves."""
        try:
            from .pc_power import tool_allowed, telegram_saved_intent, telegram_chat_intent
            from .spotify import spotify_play_intent
            used = set(tools_used or ())
            call = None
            if not used & {"media_control", "app_search", "type_text", "press_hotkey"} and tool_allowed("media_control", settings=self.settings):
                q = spotify_play_intent(text)
                if q:
                    call = ("media_control", {"action": "play", "app": "spotify", "query": q})
            tg_free = not used & {"telegram_open_chat", "send_telegram", "app_search", "type_text", "press_hotkey"} \
                and tool_allowed("telegram_open_chat", settings=self.settings)
            if call is None and tg_free and telegram_saved_intent(text):
                call = ("telegram_open_chat", {"chat": "Saved Messages"})
            if call is None and tg_free:
                # «открой чат с Nehto (в телеграме)» — also right after «открой телеграм» (Telegram in front)
                tg_ctx = "telegram" in (getattr(self.last_fg, "process", "") or "").lower() or "open_app" in used
                who = telegram_chat_intent(text, telegram_context=tg_ctx)
                if who:
                    call = ("telegram_open_chat", {"chat": who})
            if call is None:
                return
            log.info("pc intent fallback: model did not call a tool -> %s", call[0])
            self.emit("tool", name=call[0], args=call[1])
            threading.Thread(target=self.actions.execute, args=call, name="pc-intent-fallback", daemon=True).start()
        except Exception:
            log.exception("pc intent fallback failed")

    def _context(self) -> str:
        base = context_line(self.session.snapshot(), self.last_fg.title)
        pc = self.pc.context_block() if self.settings.get("pc_hearing") else ""
        return base + ("\n" + pc if pc else "")

    # ── v1.6 conversation mode («Слушать после ответа») ──
    def _apply_follow(self) -> None:
        mode = self.settings.get("follow_mode", "2m")
        if mode == "off":
            if self.follow_listener is not None:
                self.follow_listener.stop()
                self.follow_listener = None
            self.follow.end("off")
        else:
            if self.follow_listener is None:
                self.follow_listener = FollowListener(self.mic, self._on_follow_utterance,
                                                      stt_factory=self._follow_stt)
                self.follow_listener.set_hang(self.settings["vad_silence_ms"] / 1000.0)
                self.follow_listener.start()
            if mode == "always":
                self.follow.start()
        self._sync_listen()

    def _follow_stt(self):
        """Streaming Vosk for a follow-up phrase — only when Whisper is not there to decode it afterwards."""
        if self._whisper_ready():
            return None
        code = L.speech_lang(self.settings)
        path = stt_local.model_path(code)
        if not path:
            return None  # no local model for his language: the phrase goes to the cloud (timed modes only)
        return stt_local.StreamingSTT(stt_local.get_model(path))  # cached after the first load

    def _sync_listen(self) -> None:
        """Who listens to the mic right now: the name-spotter, the conversation listener, or nobody (she speaks).
        Called on every state change and by the ticker (session timeout)."""
        with self._listen_lock:
            now = time.monotonic()
            speaking = bool(self.speaker.speaking)
            tail = now < self._tts_tail_until
            rec = self._rec is not None
            self.follow.hold(speaking, now)
            active = self.follow.active(now)
            busy = speaking or tail or rec or self._answering or self._follow_busy or self.state == "thinking"
            listening = active and not busy
            fl = self.follow_listener
            if fl is not None:
                if listening:
                    fl.enabled.set()
                else:
                    fl.enabled.clear()
            if self.wake is not None:
                if speaking or rec or active:
                    self.wake.suspended.set()
                else:
                    self.wake.suspended.clear()
            if speaking or tail:
                self.pc.paused.set()
            else:
                self.pc.paused.clear()
            snap = self.follow.snapshot(now)
            rem = snap.get("remaining")
            key = (snap["active"], snap["mode"], listening, None if rem is None else int(rem))
            if key != self._follow_emitted:
                was = self._follow_emitted
                self._follow_emitted = key
                if was is not None and was[0] and not snap["active"]:
                    log.info("conversation mode: window closed (%s)", self.follow.ended_reason or "-")
                elif snap["active"] and (was is None or not was[0]):
                    log.info("conversation mode: listening without the name (%s)", snap["mode"])
                self.emit("follow", **self.follow_info(snap, listening))

    def follow_info(self, snap: dict | None = None, listening: bool | None = None) -> dict:
        snap = snap or self.follow.snapshot()
        rem = snap.get("remaining")
        if listening is None:
            fl = self.follow_listener
            listening = bool(fl is not None and fl.enabled.is_set())
        return {"active": bool(snap["active"]), "mode": snap["mode"], "always": bool(snap.get("always")),
                "remaining": None if rem is None else int(rem), "left": fmt_left(rem) if snap["active"] else "",
                "listening": bool(listening)}

    def follow_stop(self) -> dict:
        self._follow_end("ui", say=False)
        return {"ok": True}

    def _follow_end(self, reason: str, *, say: bool) -> None:
        was = self.follow.end(reason)
        self._sync_listen()
        live_off = reason == "voice" and bool(self.settings.get("live_mode"))
        if live_off:
            self.set_live(False)  # «хватит» also meant «stop commenting» before 1.6 (the model switched live mode off)
        if not was and not live_off:
            return
        log.info("conversation mode: ended (%s)%s", reason, "; live mode off" if live_off else "")
        beep("stop")
        if say:
            code = self._reply_lang()
            text = FOLLOW_BYE.get(code, FOLLOW_BYE["en"])
            self.emit("chat", role="jarvis", text=text)
            if self.settings["speak_replies"]:
                self.say(text, lang=code)
        if self.state == "thinking":
            self.set_state("idle")

    def _on_follow_utterance(self, utt: dict) -> None:
        """Conversation listener (its own thread): a finished phrase heard without the name."""
        if not self.follow.active() or self._answering or self._rec is not None or self.speaker.speaking \
                or self._follow_busy or self.state == "thinking":
            return
        self._follow_busy = True
        self._sync_listen()
        turn = VoiceTurn("voice")
        turn.mark("speech_end", utt["t_end"] - max(0.3, self.settings["vad_silence_ms"] / 1000.0))
        turn.mark("rec_end", utt["t_end"])
        self._jobs.put(("follow", (turn, utt)))

    def _follow_job(self, turn: VoiceTurn, utt: dict) -> None:
        s = self.settings
        code = L.speech_lang(s)
        from .audio import pcm_to_wav
        wav = pcm_to_wav(utt["pcm"].tobytes())
        text, conf, engine = "", 0.0, "vosk"
        if self._whisper_ready():
            try:
                text, conf, lang = stt_whisper.engine().transcribe(wav, lang_mode=s.get("whisper_lang", "pair"),
                                                                   speech_lang=code, prompt="")
                engine = "whisper"
                turn.stt, turn.lang = "whisper", lang or ""
            except Exception as e:
                log.warning("follow: whisper failed: %s", e)
                text = ""
        if not text and utt.get("text"):
            text, conf, engine = utt["text"], float(utt.get("conf") or 0.0), "vosk"
            turn.stt = "local"
        always = self.follow.always
        if (not text and engine != "whisper" and not utt.get("text") and not always
                and float(utt.get("speech_sec") or 0.0) >= 0.6 and not stt_local.model_path(code)):
            try:  # neither Whisper nor a local model for his language: cloud transcription (timed window only)
                text = self.brain.transcribe(wav, lang=code)
                conf, engine, turn.stt = 0.9, "cloud", "cloud"
            except Exception as e:
                log.info("follow: cloud transcription failed: %s", type(e).__name__)
                text = ""
        text = (text or "").strip()
        name = persona.assistant_name(s)
        if text and is_stop_phrase(text, name):
            self.emit("chat", role="user", text=text)
            self._follow_end("voice", say=True)
            return
        why = junk_reason(text, conf, float(utt.get("speech_sec") or 0.0), engine=engine, always=always)
        pc_recent = self.pc.recent_texts(25.0) if self.settings.get("pc_hearing") else []
        if len(pc_recent) > 1:
            pc_recent.append(" ".join(pc_recent[-3:]))  # a phrase may span two loopback segments
        if not why and pc_recent and echo_match(text, pc_recent):
            why = "это звук с ПК"
        if not why and always and self._last_reply and echo_match(text, [self._last_reply]):
            why = "эхо моего ответа"
        if why:
            # never log what was said in the room: only why it was skipped
            log.info("follow: skip phrase (%s; %s conf %.2f, %.1f s speech, %d chars)", why, engine, conf,
                     float(utt.get("speech_sec") or 0.0), len(text))
            return
        turn.mark("text")
        if turn.stt == "whisper":
            self.user_lang = turn.lang if turn.lang in L.LANGS else L.detect(text, prefer=code)
        else:
            self.user_lang = code
        text = self._clean_voice_text(text, wav, code, local=turn.stt != "whisper")
        addressed = bool(re.match(rf"^\W*{re.escape(name.lower())}\b", text.lower())) if name else False
        log.info("follow: phrase without the name (%s, conf %.2f) → model", engine, conf)
        self._answer(text, voice=True, turn=turn, follow_ok=True,
                     overheard="" if addressed else ("always" if always else "follow"),
                     before_output=lambda: self.emit("chat", role="user", text=text))

    # ── v1.6 «Слышать звук ПК» ──
    def _fg_title(self) -> str:
        try:
            fg = get_foreground()
            if fg.pid == os.getpid():
                return ""
            return fg.title or fg.process
        except Exception:
            return ""

    def _pc_transcribe(self, wav: bytes) -> tuple[str, float, str]:
        """Speech from the speakers → text. Whisper only on an NVIDIA GPU that no game needs and only when
        it is not busy with his own voice; otherwise the small Vosk model on the CPU. Never blocks his turns."""
        s = self.settings
        code = L.speech_lang(s)
        eng = stt_whisper.engine()
        if (self._whisper_ready() and eng.device == "cuda" and not self.local.blocked()
                and self._rec is None and not self._follow_busy and not eng._lock.locked()):
            text, conf, _lang = eng.transcribe(wav, lang_mode="auto", speech_lang=code, prompt="")
            return (text if conf >= 0.4 else ""), conf, "whisper"
        for c in dict.fromkeys([code, "ru"]):
            path = stt_local.model_path(c)
            if path and stt_local.is_loaded(c):
                text, conf = stt_local.transcribe_wav(stt_local.get_model(path), wav)
                return (text if conf >= 0.5 else ""), conf, "vosk"
        return "", 0.0, "-"

    def pc_hearing_info(self) -> dict:
        d = self.pc.info()
        d["enabled"] = bool(self.settings.get("pc_hearing"))
        return d

    # ── focus session ──
    def focus_start(self, task: str, minutes=None, rounds=None) -> dict:
        s = self.settings
        if self.session.active:
            self.stats.add_focus(self.session.stop())
        mins = int(minutes) if minutes else s["focus_minutes"]
        mins = max(1, min(240, mins))
        rnds = max(1, min(8, int(rounds))) if rounds else s["rounds"]
        self.session.start(task, mins, rnds, s["break_minutes"])
        self.policy.reset()
        self._episode_counted = False
        self._last_screen_check = time.monotonic()  # first screen check after one interval
        self.guard_status = "Страж включён"
        self.emit("focus")
        ends = datetime.fromtimestamp(time.time() + mins * 60).strftime("%H:%M")
        log.info("focus start: %r %d min x%d", task, mins, rnds)
        return {"ok": True, "task": self.session.task, "minutes": mins, "rounds": rnds,
                "break_minutes": s["break_minutes"], "first_block_ends_at": ends}

    def focus_stop(self) -> dict:
        if not self.session.active:
            return {"ok": False, "error": "фокус-сессия не запущена"}
        sec = self.session.stop()
        self.stats.add_focus(sec)
        self.policy.reset()
        self.guard_status = "Страж ждёт фокус-сессию"
        self.emit("focus")
        return {"ok": True, "focused_minutes": sec // 60,
                "today_minutes": self.stats.today() // 60}

    def focus_pause(self, resume: bool = False) -> dict:
        if not self.session.active:
            return {"ok": False, "error": "фокус-сессия не запущена"}
        if resume:
            self.session.resume()
        else:
            self.session.pause()
        self.emit("focus")
        return {"ok": True, "paused": not self.session.running}

    def focus_toggle_pause(self) -> None:
        self.focus_pause(resume=not self.session.running)

    def focus_skip(self) -> None:
        ev = self.session.skip()
        if ev:
            self._on_phase(ev)

    def focus_status(self) -> dict:
        snap = self.session.snapshot()
        return {"active": self.session.active, "task": snap["task"], "phase": snap["phase"],
                "paused": not snap["running"], "remaining": fmt_clock(snap["remaining"]),
                "round": snap["round"], "rounds": snap["rounds"],
                "today_focus_minutes": self.stats.today() // 60,
                "distractions_today": self.stats.distractions_today()}

    def _on_phase(self, ev) -> None:
        self.stats.add_focus(ev.focus_seconds)
        beep("phase")
        task = self.session.task
        code = self._reply_lang()
        if ev.new == "break":
            text = L.text("break", code, mins=L.minutes(self.session.break_sec // 60, code))
        elif ev.new == "focus":
            text = L.text("back", code, task=task)
        else:
            text = L.text("done", code, task=task, today=L.minutes(self.stats.today() // 60, code))
            self.guard_status = "Страж ждёт фокус-сессию"
        self.policy.reset()
        self.emit("chat", role="jarvis", text=text)
        self.emit("notify", text=text)
        self.emit("focus")
        self.say(text, lang=code)

    # ── reminders ──
    def add_reminder(self, text: str, due: datetime) -> dict:
        with self._rem_lock:
            rid = next(self._rem_ids)
            self.reminders.append({"id": rid, "text": text, "due": due})
        self.emit("reminders")
        return {"ok": True, "text": text, "at": due.strftime("%H:%M"),
                "in_minutes": round((due - datetime.now()).total_seconds() / 60, 1)}

    def list_reminders(self) -> dict:
        with self._rem_lock:
            return {"reminders": [{"text": r["text"], "at": r["due"].strftime("%H:%M")}
                                  for r in sorted(self.reminders, key=lambda r: r["due"])]}

    def cancel_reminders(self) -> dict:
        with self._rem_lock:
            n = len(self.reminders)
            self.reminders.clear()
        self.emit("reminders")
        return {"ok": True, "cancelled": n}

    def _check_reminders(self) -> None:
        now = datetime.now()
        due = []
        with self._rem_lock:
            for r in list(self.reminders):
                if r["due"] <= now:
                    due.append(r)
                    self.reminders.remove(r)
        code = self._reply_lang()
        for r in due:
            text = L.text("remind", code, text=str(r["text"]).rstrip(". "))
            beep("phase")
            self.emit("chat", role="jarvis", text=text)
            self.emit("notify", text=text)
            self.emit("reminders")
            self.say(text, lang=code)

    # ── screen ──
    def look_at_screen(self, question: str) -> dict:
        jpeg = capture_screen_jpeg(max_side=1280, quality=70)
        if not jpeg:
            return {"ok": False, "error": "не удалось сделать снимок экрана"}
        return {"ok": True, "description": self.brain.describe_screen(jpeg, question)}

    # ── ticker: focus timer, reminders, guard ──
    def _ticker(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self.session.tick()
                if ev:
                    self._on_phase(ev)
                self._check_reminders()
                now = time.monotonic()
                self._sync_listen()
                self._guard_step(now)
                self._live_step(now)
            except Exception:
                log.exception("ticker step failed")
            self._stop.wait(0.5)

    def _guard_step(self, now: float) -> None:
        s = self.settings
        if not self.session.in_focus:
            return
        if now - self._last_title_check >= s["title_check_sec"]:
            self._last_title_check = now
            fg = get_foreground()
            self.last_fg = fg
            hit = match_distraction(fg.title, fg.process, s["distractions"], own_pid=os.getpid(), pid=fg.pid)
            if hit:
                self._title_hit_until = now + s["title_check_sec"] * 2
                self.guard_status = f"Отвлечение: «{hit}»"
                self.emit("guard", text=self.guard_status)
                self._offtask(now, immediate=False)
            elif now >= self._screen_offtask_until:
                self.policy.on_task(now)
                if self.policy.level == 0:
                    self._episode_counted = False
        interval = s["screen_check_sec"]
        if (interval > 0 and not self._screen_inflight and now >= self._screen_backoff_until
                and now >= self._title_hit_until and now - self._last_screen_check >= interval
                and self.state not in ("listening", "thinking")):
            self._last_screen_check = now
            self._screen_inflight = True
            threading.Thread(target=self._screen_check, name="screen-check", daemon=True).start()

    def _screen_check(self) -> None:
        try:
            jpeg = capture_screen_jpeg(max_side=1024, quality=60)
            if not jpeg or not self.session.in_focus:
                return
            fg = self.last_fg
            v = self.brain.classify_screen(jpeg, self.session.task, fg.title, fg.process)
            del jpeg  # memory only, never saved
            self._screen_fail = 0
            now = time.monotonic()
            stamp = datetime.now().strftime("%H:%M")
            if v["valid"] and not v["on_task"] and v["confidence"] >= 0.8:
                self._screen_offtask_until = now + self.settings["screen_check_sec"] + 5
                self.guard_status = f"{stamp} экран: не по задаче ({v['activity']})"
                self.emit("guard", text=self.guard_status)
                self._offtask(now, immediate=True)
            else:
                self._screen_offtask_until = 0.0
                self.guard_status = f"{stamp} экран: по задаче" + (f" ({v['activity']})" if v["activity"] else "")
                self.emit("guard", text=self.guard_status)
            log.info("screen check: %s", v)
        except P.RateLimitError as e:
            self._screen_fail += 1
            back = min(900.0, max(e.retry_after or 0, self.settings["screen_check_sec"] * (2 ** self._screen_fail)))
            self._screen_backoff_until = time.monotonic() + back
            self.guard_status = f"Лимит API: проверка экрана на паузе {int(back // 60) or 1} мин"
            self.emit("guard", text=self.guard_status)
        except P.NoKeyError:
            self._screen_backoff_until = time.monotonic() + 600
            self.guard_status = "Нет API-ключа: проверка экрана выключена"
            self.emit("guard", text=self.guard_status)
        except Exception as e:
            self._screen_fail += 1
            self._screen_backoff_until = time.monotonic() + min(600, 30 * self._screen_fail)
            log.warning("screen check failed: %s", e)
        finally:
            self._screen_inflight = False

    def _offtask(self, now: float, *, immediate: bool) -> None:
        level = self.policy.off_task(now, immediate=immediate)
        if not level:
            return
        if not self._episode_counted:
            self.stats.add_distraction()
            self._episode_counted = True
        if self.state in ("listening", "thinking"):
            return
        code = self._reply_lang()
        left = L.minutes(max(1, int(self.session.remaining() // 60)), code)
        text = None
        if code != "ru":
            text = L.nudge(code, level, self.session.task, left, str(self.settings.get("user_name") or "").strip())
        if not text:
            text = persona.nudge_text(self.settings, level, self.session.task, left=left)
        log.info("nudge level %d", level)
        self.live.on_remark(now, "guard")
        beep("nudge")
        self.emit("chat", role="jarvis", text=text, kind="nudge")
        self.emit("notify", text=text)
        self.emit("focus")
        self.say(text, lang=code)

    # ── live mode (v1.2) ──
    def set_live(self, on: bool) -> dict:
        """Voice/text intent or UI toggle."""
        if bool(self.settings["live_mode"]) != bool(on):
            new = dict(self.settings)
            new["live_mode"] = bool(on)
            self.apply_settings(new)  # → _live_switched
        p = talk_params(self.settings)
        return {"ok": True, "live_mode": bool(on), "check_every_sec": int(p["interval"]),
                "min_gap_min": round(p["gap"] / 60, 1),
                "note": "буду иногда смотреть на экран и говорить, только когда это уместно" if on
                        else "больше не буду сам комментировать экран"}

    def _live_switched(self, on: bool) -> None:
        self.live.reset(time.monotonic() if on else None)
        self.live_status = "Включён — присматриваюсь" if on else "Выключен"
        log.info("live mode %s", "on" if on else "off")
        self.emit("live", status=self.live_status, enabled=on)

    def _set_live_status(self, text: str) -> None:
        if text != self.live_status:
            self.live_status = text
            self.emit("live", status=text, enabled=self.settings["live_mode"])

    def _live_step(self, now: float) -> None:
        s = self.settings
        if not s["live_mode"] or self._live_inflight:
            return
        if now - self._live_fg_at >= 2.0:  # cheap local checks every 2 s
            self._live_fg_at = now
            fg = get_foreground()
            self._live_fg = fg
            self._live_fullscreen = is_fullscreen(fg)
            if fg.pid != os.getpid():
                self.live.note_foreground((fg.process.lower(), fg.title), now)
        fg = self._live_fg
        if fg.pid and fg.pid == os.getpid():
            return  # he is looking at Jarvis itself
        if self.state != "idle" or self._rec is not None or self.speaker.speaking:
            return
        if is_call(fg.title, fg.process):
            self._set_live_status("Молчу: идёт звонок")
            return
        in_focus = self.session.in_focus
        distraction = match_distraction(fg.title, fg.process, s["distractions"])
        if self._live_fullscreen and not distraction:
            self._set_live_status("Молчу: полноэкранное приложение")
            return
        if self._live_fullscreen and in_focus:
            return  # a fullscreen distraction during focus is the guard's job
        ok, why = self.live.due(now, s, in_focus=in_focus)
        if not ok:
            if why == "gap":
                left = self.live.gap_left(now, s, in_focus=in_focus)
                self._set_live_status(f"Пауза после реплики: {max(1, int(left // 60) + (1 if left % 60 else 0))} мин")
            elif why in ("settle", "change") and not self.live_status.startswith("Лимит"):
                self._set_live_status("Присматриваюсь…")
            return
        self.live.on_check(now)
        self._live_inflight = True
        threading.Thread(target=self._live_check, args=(fg, self._live_fullscreen, in_focus),
                         name="live-check", daemon=True).start()

    def _live_check(self, fg: Foreground, fullscreen: bool, in_focus: bool) -> None:
        s = self.settings
        try:
            jpeg = capture_screen_jpeg(max_side=1024, quality=60)
            if not jpeg:
                self._set_live_status("Не удалось сделать снимок экрана")
                return
            now = time.monotonic()
            snap = self.session.snapshot()
            since = None if self.live.last_remark is None else now - self.live.last_remark
            system, prompt = build_prompt(s, task=snap["task"], focus_phase=snap["phase"], title=fg.title,
                                          process=fg.process, fullscreen=fullscreen, since_remark=since,
                                          memory=self.live.memory_lines(now), user_lang=self.user_lang)
            pc = self.pc.context_block() if s.get("pc_hearing") else ""
            if pc:
                prompt += "\n\n" + pc
            d = self.brain.live_decide(jpeg, system, prompt)
            del jpeg  # memory only, never saved
            self.live.on_success()
            now = time.monotonic()
            ok, why = self.live.accept(d, now, self.settings, in_focus=self.session.in_focus)
            stamp = datetime.now().strftime("%H:%M")
            # the world may have moved on while the model was thinking
            if ok and (not self.settings["live_mode"] or self.state != "idle" or self._rec is not None
                       or self.speaker.speaking):
                ok, why = False, "busy"
            cur = get_foreground()
            if ok and cur.pid != os.getpid() and (cur.process.lower(), cur.title) != (fg.process.lower(), fg.title):
                ok, why = False, "change"
            log.info("live: speak=%s kind=%s conf=%.2f -> %s (%s) | %s", d["speak"], d["kind"], d["confidence"],
                     "SAY" if ok else "skip", why or "ok", d["reason"][:120])
            if not ok:
                self.live.remember(now, d["activity"], fg.title, silent_reason=why or d["reason"])
                self._set_live_status(f"{stamp} посмотрел — молчу" + (f" ({d['activity']})" if d["activity"] else ""))
                return
            self.live.remember(now, d["activity"], fg.title, said=d["text"], kind=d["kind"])
            self.live.on_remark(now, "live")
            self._set_live_status(f"{stamp} сказал: {d['text'][:60]}")
            self._live_say(d)
        except P.RateLimitError as e:
            back = self.live.on_rate_limit(time.monotonic(), talk_params(s)["interval"], e.retry_after, e.daily)
            self._set_live_status(f"Лимит API: живой режим на паузе {max(1, int(back // 60))} мин и смотрит реже")
            log.info("live: rate limited, pause %.0fs, slow x%.1f", back, self.live.slow)
        except P.NoKeyError:
            self.live.backoff_until = time.monotonic() + 600
            self._set_live_status("Нет API-ключа — живому режиму нечем думать")
        except Exception as e:
            back = self.live.on_error(time.monotonic())
            log.warning("live check failed: %s", e)
            self._set_live_status(f"Ошибка, повторю через {int(back)} с")
        finally:
            self._live_inflight = False

    def _live_say(self, d: dict) -> None:
        text = d["text"]
        kind = d["kind"]
        self.emit("chat", role="jarvis", text=text, kind="live", live_kind=kind)
        self.emit("notify", text=text)
        try:
            self.brain.note_remark(text)
        except Exception:
            log.exception("note_remark failed")
        if not self.settings["speak_replies"]:
            return
        beep("phase" if kind != "nudge" else "nudge")
        self.say(text, lang=self._reply_lang())
        reply = self.settings["live_reply_sec"]
        if kind == "question" and reply > 0:
            threading.Thread(target=self._await_reply, args=(reply,), name="live-reply", daemon=True).start()

    def _await_reply(self, seconds: int) -> None:
        """After a spoken question: once the voice finishes, listen briefly so he can just answer."""
        t0 = time.monotonic()
        while not self.speaker.speaking and time.monotonic() - t0 < 5:
            time.sleep(0.1)
        while self.speaker.speaking and time.monotonic() - t0 < 60:
            time.sleep(0.2)
        time.sleep(0.3)
        if self.state in ("idle", "speaking") and self._rec is None and not self.speaker.speaking:
            log.info("live: listening %ds for a reply", seconds)
            self._start_recording("reply", no_speech_timeout=seconds)
