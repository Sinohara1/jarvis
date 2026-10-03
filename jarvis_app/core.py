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
from .audio import MicHub, Recording, Speaker, beep
from .brain import Brain, context_line
from .config import load_settings, normalize_settings, register_secrets, save_settings
from .focus import FocusSession, Stats, fmt_clock, minutes_phrase
from .hotkey import Hotkey
from .watcher import (Foreground, NudgePolicy, capture_screen_jpeg, get_foreground,
                      match_distraction, nudge_text)

log = logging.getLogger("jarvis")


class JarvisCore:
    def __init__(self, emit) -> None:
        self.emit = emit
        self.settings = load_settings()
        register_secrets(self.settings)
        self.stats = Stats()
        self.session = FocusSession()
        self.mic = MicHub()
        self.speaker = Speaker(lambda: self.settings, on_state=self._on_speaking)
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

    # ── lifecycle ──
    def start(self) -> None:
        threading.Thread(target=self._worker, name="brain-worker", daemon=True).start()
        threading.Thread(target=self._ticker, name="ticker", daemon=True).start()
        self._apply_hotkey()
        self._apply_wake()
        log.info("core started; provider=%s chat=%s lite=%s", self.settings["provider"],
                 *self.brain.models())

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
        if (old["wake_word"] != self.settings["wake_word"]
                or old["wake_threshold"] != self.settings["wake_threshold"]):
            self._apply_wake()
        if old["provider"] != self.settings["provider"]:
            self.brain.reset()
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

    def _apply_wake(self) -> None:
        if self.wake:
            self.wake.stop()
            self.wake = None
        if not self.settings["wake_word"]:
            self.emit("wake", enabled=False, error=None)
            return
        try:
            from .wakeword import WakeWordListener
            self.wake = WakeWordListener(self.mic, self.wake_detected,
                                         threshold=self.settings["wake_threshold"])
            self.wake.start()
            self.emit("wake", enabled=True, error=None)
        except Exception as e:
            log.error("wake word unavailable: %s", e)
            self.wake = None
            self.emit("wake", enabled=False, error=str(e))

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
        elif self.state == "speaking":
            self.set_state("idle")

    # ── voice input ──
    def hotkey_down(self) -> None:
        with self._rec_lock:
            if self._rec is not None:
                self._rec.stop()  # second press ends a toggle-mode recording
                return
        self.speaker.stop()
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
        self.speaker.stop()
        self._start_recording("auto")

    def wake_detected(self) -> None:
        if self._rec is not None or self.state == "thinking":
            return
        self.speaker.stop()
        self._start_recording("auto")

    def _start_recording(self, mode: str) -> None:
        with self._rec_lock:
            if self._rec is not None:
                return
            rec = Recording(self.mic, auto_stop=(mode == "auto"),
                            on_level=lambda lv: self.emit("level", level=lv))
            self._rec = rec
            self._rec_mode = mode
        if self.wake:
            self.wake.suspended.set()
        self.set_state("listening")
        beep("listen")
        threading.Thread(target=self._record_thread, args=(rec,), name="recorder", daemon=True).start()

    def _record_thread(self, rec: Recording) -> None:
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
            self._jobs.put(("audio", wav))
        else:
            self.set_state("idle", "Не расслышал" if not rec.cancelled else "")

    def submit_text(self, text: str) -> None:
        t = (text or "").strip()
        if t:
            self.speaker.stop()
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
                if kind == "audio":
                    self.set_state("thinking", "Распознаю…")
                    text = self.brain.transcribe(payload)
                    if not text:
                        self.set_state("idle", "Не расслышал")
                        continue
                    self.emit("chat", role="user", text=text)
                    self._answer(text)
                elif kind == "text":
                    self.emit("chat", role="user", text=payload)
                    self._answer(payload)
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

    def _answer(self, text: str) -> None:
        self.set_state("thinking", "Думаю…")
        reply = self.brain.ask(text, on_tool=lambda n, a: self.emit("tool", name=n, args=a))
        self.emit("chat", role="jarvis", text=reply)
        if self.settings["speak_replies"]:
            self.set_state("speaking")
            self.say(reply)
        else:
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
                self._guard_step(time.monotonic())
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
        text = nudge_text(level, self.session.task, left=left)
        log.info("nudge level %d", level)
        beep("nudge")
        self.emit("chat", role="jarvis", text=text, kind="nudge")
        self.emit("notify", text=text)
        self.emit("focus")
        self.say(text)
