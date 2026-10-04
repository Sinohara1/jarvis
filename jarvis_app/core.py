"""JarvisCore: glue between voice I/O, the brain, focus session, focus guard,
reminders and settings. GUI-agnostic; reports to the UI via emit(event, **data)."""

from __future__ import annotations

import itertools
import logging
import os
import queue
import threading
import time
from datetime import datetime

from . import providers as P
from .actions import Actions
from .audio import MicHub, Recording, SentenceSplitter, Speaker, VoiceTurn, beep
from .brain import Brain, TurnCancelled, context_line
from . import stt_local, tts_local
from . import persona
from .config import load_settings, normalize_settings, register_secrets, save_settings
from .focus import FocusSession, Stats, fmt_clock, minutes_phrase
from .hotkey import Hotkey
from .live import LivePolicy, build_prompt, live_intent, talk_params
from .watcher import (Foreground, NudgePolicy, capture_screen_jpeg, get_foreground, is_call,
                      is_fullscreen, match_distraction, nudge_text)

log = logging.getLogger("jarvis")

# Spoken right away when the model calls a tool before saying anything (voice turns): the tool + the
# second model round take ~1 s more, the user should hear that we are on it.
TOOL_ACK = {"open_app": "Открываю.", "open_url": "Открываю.", "open_path": "Открываю.", "web_search": "Ищу.",
            "find_files": "Ищу файлы.", "start_focus": "Запускаю фокус.", "stop_focus": "Останавливаю.",
            "set_reminder": "Ставлю напоминание.", "cancel_reminders": "Отменяю.", "look_at_screen": "Смотрю на экран."}


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
        self.speaker = Speaker(lambda: self.settings, on_state=self._on_speaking, on_turn=self._on_turn,
                               piper=self.piper)
        self._turn: VoiceTurn | None = None
        self._answering = False
        self._piper_dl: str | None = None
        self._piper_err: str | None = None
        self.last_timing: dict | None = None
        self.actions = Actions(self)
        self.brain = Brain(lambda: self.settings, self.actions, self._context)
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

    # ── lifecycle ──
    def start(self) -> None:
        threading.Thread(target=self._worker, name="brain-worker", daemon=True).start()
        threading.Thread(target=self._ticker, name="ticker", daemon=True).start()
        self._apply_hotkey()
        self._apply_wake()
        threading.Thread(target=self._prepare_voice, name="voice-prepare", daemon=True).start()
        log.info("core started; provider=%s chat=%s lite=%s", self.settings["provider"],
                 *self.brain.models())

    # ── fast voice (v1.3): local TTS + local STT preparation ──
    def _prepare_voice(self) -> None:
        """Warm up what the next voice turn needs, so the first answer is not slower than the rest."""
        s = self.settings
        if s["tts_engine"] == "piper":
            self.ensure_piper_voice(s["piper_voice"])
        if s["stt_mode"] == "local":
            self._ensure_stt_model()

    def ensure_piper_voice(self, key: str) -> None:
        if not tts_local.available():
            self._piper_err = "движок Piper не найден в сборке"
            return
        if tts_local.installed(key):
            try:
                self.piper.load(key)
            except Exception as e:
                self._piper_err = f"не удалось загрузить голос: {e}"
                log.exception("piper load failed")
            self._emit_tts()
            return
        self.download_voice(key)

    def download_voice(self, key: str) -> bool:
        if key not in tts_local.PIPER_VOICES:
            return False
        if self._piper_dl:
            return self._piper_dl == key
        self._piper_dl, self._piper_err = key, None
        self._emit_tts()

        def work() -> None:
            try:
                log.info("downloading piper voice %s", key)
                tts_local.download_voice(key, lambda d, t: self.emit("tts_dl", key=key, done=d, total=t))
                self._piper_dl = None
                if self.settings["piper_voice"] == key and self.settings["tts_engine"] == "piper":
                    self.piper.load(key)
            except Exception as e:
                self._piper_dl = None
                self._piper_err = f"не удалось скачать голос: {e}"
                log.error("piper voice download failed: %s", e)
            self._emit_tts()
        threading.Thread(target=work, name="piper-download", daemon=True).start()
        return True

    def tts_info(self) -> dict:
        s = self.settings
        return {"engine": s["tts_engine"], "voice": s["piper_voice"], "voices": tts_local.voices_payload(),
                "downloading": self._piper_dl, "error": self._piper_err, "available": tts_local.available(),
                "active": self.speaker.engine_for(s), "stt": self.stt_info(), "timing": self.last_timing}

    def _emit_tts(self) -> None:
        self.emit("tts", **self.tts_info())

    def stt_info(self) -> dict:
        from . import namewake
        return {"mode": self.settings["stt_mode"], "model": bool(namewake.find_model()),
                "loaded": stt_local.is_loaded(), "downloading": self._wake_dl}

    def _ensure_stt_model(self) -> None:
        path = stt_local.model_path()
        if path:
            try:
                t0 = time.monotonic()
                stt_local.get_model(path)
                log.info("local stt model ready in %.2fs", time.monotonic() - t0)
            except Exception as e:
                log.error("local stt model load failed: %s", e)
            return
        if not self._wake_dl:
            self._wake_dl = True
            threading.Thread(target=self._download_vosk, name="vosk-download", daemon=True).start()
            self._emit_wake()

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
        if self._rec:
            self._rec.cancel()
        self.speaker.shutdown()
        self.mic.close()
        self._jobs.put(None)

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
        if old["provider"] != self.settings["provider"]:
            self.brain.reset()
        s = self.settings
        if s["tts_engine"] == "piper" and (old["piper_voice"] != s["piper_voice"] or old["tts_engine"] != "piper"):
            threading.Thread(target=self.ensure_piper_voice, args=(s["piper_voice"],), daemon=True).start()
        if s["stt_mode"] == "local" and old["stt_mode"] != "local":
            threading.Thread(target=self._ensure_stt_model, daemon=True).start()
        if old["vad_silence_ms"] != s["vad_silence_ms"] and self.wake is not None and hasattr(self.wake, "set_hang"):
            self.wake.set_hang(s["vad_silence_ms"] / 1000.0)
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
        if self.wake:
            if speaking:
                self.wake.suspended.set()
            elif not self._rec:
                self.wake.suspended.clear()
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
        det = det or {}
        if request_wav or det.get("request_text"):
            # «Пятница, открой телеграм» in one breath: the request is already recorded (and recognised)
            beep("listen")
            turn = VoiceTurn("voice")
            turn.mark("speech_end", det.get("t_speech_end"))
            turn.mark("rec_end")
            text = det.get("request_text") or ""
            if self.settings["stt_mode"] == "local" and stt_local.acceptable(text, det.get("request_conf", 0.0)):
                turn.stt = "wake-local"
                turn.mark("text")
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
            kw = {"no_speech_timeout": float(no_speech_timeout)} if no_speech_timeout else {}
            stt = None
            if self.settings["stt_mode"] == "local":
                path = stt_local.model_path()
                if path and stt_local.is_loaded():
                    try:
                        stt = stt_local.StreamingSTT(stt_local.get_model(path))
                    except Exception as e:
                        log.warning("local stt unavailable: %s", e)
                elif path:  # loads in ~0.7 s; this turn goes to the cloud, the next one is local
                    threading.Thread(target=self._ensure_stt_model, daemon=True).start()
            rec = Recording(self.mic, auto_stop=(mode in ("auto", "reply")),
                            on_level=lambda lv: self.emit("level", level=lv), stt=stt,
                            silence_sec=self.settings["vad_silence_ms"] / 1000.0, **kw)
            self._rec = rec
            self._rec_mode = mode
        if self.wake:
            self.wake.suspended.set()
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
            if self.wake and not self.speaker.speaking:
                self.wake.suspended.clear()
        beep("stop")
        if wav and rec.speech_detected:
            turn = VoiceTurn("voice")
            turn.mark("speech_end", rec.t_speech_end)
            turn.mark("rec_end", rec.t_end)
            text = ""
            if rec.stt is not None and stt_local.acceptable(rec.text, rec.conf):
                text = rec.text
                turn.stt = "local"
                turn.mark("text")
            elif rec.stt is not None:
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

    def say(self, text: str, *, interrupt: bool = False) -> None:
        self.speaker.say(text, interrupt=interrupt)

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
                    if not text:
                        self.set_state("thinking", "Распознаю…")
                        turn.stt = "cloud"
                        text = self.brain.transcribe(wav)
                        turn.mark("text")
                        if not text:
                            self.set_state("idle", "Не расслышал")
                            continue
                    self.emit("chat", role="user", text=text)
                    self._answer(text, voice=True, turn=turn)
                elif kind == "audio":  # legacy path (kept for tests/tools)
                    self.set_state("thinking", "Распознаю…")
                    text = self.brain.transcribe(payload)
                    if not text:
                        self.set_state("idle", "Не расслышал")
                        continue
                    self.emit("chat", role="user", text=text)
                    self._answer(text, voice=True)
                elif kind == "text":
                    self.emit("chat", role="user", text=payload)
                    self._answer(payload)
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

    def _answer(self, text: str, *, voice: bool = False, turn: VoiceTurn | None = None) -> None:
        """Stream the reply: each finished sentence goes to TTS while the model keeps writing."""
        self.set_state("thinking", "Думаю…")
        turn = turn or VoiceTurn("text")
        turn.mark("text")
        self._turn = turn
        self._answering = True
        tools_used: list[str] = []
        speak = bool(self.settings["speak_replies"])
        stream = self.speaker.open_stream(turn) if speak else None
        if stream is not None:
            self.speaker.prewarm()
        splitter = SentenceSplitter()
        parts: list[str] = []
        last_ui = [0.0]
        spoken = [0]
        acked: list[str] = []
        lock = threading.Lock()
        last_delta = [time.monotonic()]
        finished = threading.Event()

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
            self.emit("tool", name=n, args=a)
            if stream is not None:
                with lock:
                    for snt in splitter.flush():  # say «Открываю…» before the tool runs
                        speak_sentence(snt)
                    if voice and spoken[0] == 0 and n in TOOL_ACK:
                        acked.append(TOOL_ACK[n])
                        speak_sentence(TOOL_ACK[n])

        if stream is not None:
            threading.Thread(target=idle_flush, name="tts-idle-flush", daemon=True).start()
        try:
            reply = self.brain.ask(text, on_tool=on_tool, on_text=on_text,
                                   fast=voice and bool(self.settings["fast_replies"]), voice=voice)
            turn.model = turn.model or self.brain.last_model
            finished.set()
            if stream is not None and not turn.cancelled:
                with lock:
                    for snt in splitter.flush():
                        speak_sentence(snt)
                if not parts and not acked:  # nothing was streamed (tool-only answer → «Готово.»)
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
        self.emit("chat", role="jarvis", text=reply)
        if not speak or (not self.speaker.speaking and self.speaker.idle):
            if self.state == "thinking":
                self.set_state("idle")

    def _context(self) -> str:
        return context_line(self.session.snapshot(), self.last_fg.title)

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
        if ev.new == "break":
            text = f"Блок закончен, отличная работа! Перерыв {minutes_phrase(self.session.break_sec // 60)}. Встань и разомнись."
        elif ev.new == "focus":
            text = f"Перерыв окончен. Возвращаемся к задаче: {task}."
        else:
            text = f"Готово! Фокус-сессия «{task}» завершена. Сегодня в фокусе уже {self.stats.today() // 60} минут."
            self.guard_status = "Страж ждёт фокус-сессию"
        self.policy.reset()
        self.emit("chat", role="jarvis", text=text)
        self.emit("notify", text=text)
        self.emit("focus")
        self.say(text)

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
        for r in due:
            text = f"Напоминаю: {r['text']}."
            beep("phase")
            self.emit("chat", role="jarvis", text=text)
            self.emit("notify", text=text)
            self.emit("reminders")
            self.say(text)

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
        left = minutes_phrase(max(1, int(self.session.remaining() // 60)))
        text = persona.nudge_text(self.settings, level, self.session.task, left=left)
        log.info("nudge level %d", level)
        self.live.on_remark(now, "guard")
        beep("nudge")
        self.emit("chat", role="jarvis", text=text, kind="nudge")
        self.emit("notify", text=text)
        self.emit("focus")
        self.say(text)

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
                                          memory=self.live.memory_lines(now))
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
        self.say(text)
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
