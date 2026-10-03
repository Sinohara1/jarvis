"""Focus session (Pomodoro-style) logic and focus statistics. Pure / GUI-free."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import DISTRACT_PATH, STATS_PATH, ensure_dir

WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def fmt_clock(sec: float | int) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def minutes_phrase(n: int) -> str:
    return f"{n} {plural_ru(n, 'минута', 'минуты', 'минут')}"


def phase_label(phase: str) -> str:
    return {"idle": "Нет сессии", "focus": "Фокус", "break": "Перерыв",
            "done": "Готово"}.get(phase, phase)


@dataclass
class PhaseEvent:
    old: str
    new: str
    round_num: int
    focus_seconds: int  # focus seconds completed by this transition (for stats)


class FocusSession:
    """Rounds of focus→break. 1 round = focus + break; after the last focus the
    session ends (no trailing break). Time is injected for testability."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.task = ""
        self.phase = "idle"
        self.focus_sec = 25 * 60
        self.break_sec = 5 * 60
        self.rounds = 1
        self.round_num = 1
        self.running = False
        self._deadline: float | None = None
        self._remaining = 0.0
        self._phase_started_remaining = 0.0
        self.started_at: float | None = None

    # ── queries ──
    @property
    def active(self) -> bool:
        return self.phase in ("focus", "break")

    @property
    def in_focus(self) -> bool:
        return self.phase == "focus" and self.running

    def remaining(self, now: float | None = None) -> float:
        with self.lock:
            if self.running and self._deadline is not None:
                now = time.monotonic() if now is None else now
                return max(0.0, self._deadline - now)
            return max(0.0, self._remaining)

    def phase_total(self) -> int:
        return self.focus_sec if self.phase == "focus" else self.break_sec if self.phase == "break" else 0

    def progress(self, now: float | None = None) -> float:
        """Remaining fraction 1.0 → 0.0 within the current phase."""
        tot = self.phase_total()
        if tot <= 0:
            return 1.0 if self.phase == "done" else 0.0
        return max(0.0, min(1.0, self.remaining(now) / tot))

    def snapshot(self, now: float | None = None) -> dict:
        with self.lock:
            return {
                "task": self.task, "phase": self.phase, "running": self.running,
                "remaining": self.remaining(now), "round": self.round_num,
                "rounds": self.rounds, "focus_min": self.focus_sec // 60,
                "break_min": self.break_sec // 60, "progress": self.progress(now),
            }

    # ── control ──
    def start(self, task: str, focus_min: int, rounds: int = 1, break_min: int = 5,
              now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self.lock:
            self.task = (task or "").strip() or "работа"
            self.focus_sec = max(60, int(focus_min) * 60)
            self.break_sec = max(60, int(break_min) * 60)
            self.rounds = max(1, min(8, int(rounds)))
            self.round_num = 1
            self.phase = "focus"
            self.running = True
            self._remaining = float(self.focus_sec)
            self._deadline = now + self._remaining
            self.started_at = now

    def pause(self, now: float | None = None) -> None:
        with self.lock:
            if not self.running:
                return
            self._remaining = self.remaining(now)
            self._deadline = None
            self.running = False

    def resume(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.running or not self.active:
                return
            self._deadline = now + self._remaining
            self.running = True

    def _focus_elapsed(self, now: float | None) -> int:
        if self.phase != "focus":
            return 0
        return max(0, int(round(self.focus_sec - self.remaining(now))))

    def stop(self, now: float | None = None) -> int:
        """End the session. Returns focus seconds to record."""
        with self.lock:
            done = self._focus_elapsed(now)
            self.phase = "idle"
            self.running = False
            self._deadline = None
            self._remaining = 0.0
            return done

    def skip(self, now: float | None = None) -> PhaseEvent | None:
        with self.lock:
            if not self.active:
                return None
            return self._advance(now)

    def _advance(self, now: float | None) -> PhaseEvent:
        now = time.monotonic() if now is None else now
        old = self.phase
        focus_done = self._focus_elapsed(now)
        if old == "focus":
            if self.round_num >= self.rounds:
                self.phase = "done"
            else:
                self.phase = "break"
        elif old == "break":
            self.phase = "focus"
            self.round_num += 1
        if self.phase == "done":
            self.running = False
            self._deadline = None
            self._remaining = 0.0
        else:
            self._remaining = float(self.phase_total())
            self._deadline = now + self._remaining if self.running else None
            if not self.running:
                self.running = True
                self._deadline = now + self._remaining
        return PhaseEvent(old, self.phase, self.round_num, focus_done)

    def tick(self, now: float | None = None) -> PhaseEvent | None:
        with self.lock:
            if not self.running or not self.active:
                return None
            if self.remaining(now) > 0.0:
                return None
            return self._advance(now)

    def extend(self, minutes: int, now: float | None = None) -> None:
        with self.lock:
            if self.phase != "focus":
                return
            add = max(1, int(minutes)) * 60
            self.focus_sec += add
            if self.running and self._deadline is not None:
                self._deadline += add
            else:
                self._remaining += add


# ─── Stats (compatible with Pomodoro's {YYYY-MM-DD: seconds}) ────────────────

def _load_daymap(path: str) -> dict[str, int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                datetime.strptime(str(k), "%Y-%m-%d")
                n = int(v)
            except (ValueError, TypeError):
                continue
            if n > 0:
                out[str(k)] = n
    return out


def _save_daymap(data: dict[str, int], path: str) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: int(v) for k, v in data.items() if int(v) > 0}, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


class Stats:
    def __init__(self, stats_path: str = STATS_PATH, distract_path: str = DISTRACT_PATH) -> None:
        self.stats_path = stats_path
        self.distract_path = distract_path
        self.lock = threading.Lock()
        self.focus = _load_daymap(stats_path)
        self.distractions = _load_daymap(distract_path)

    def add_focus(self, seconds: int, day: date | None = None) -> None:
        if seconds <= 0:
            return
        key = (day or date.today()).isoformat()
        with self.lock:
            self.focus[key] = int(self.focus.get(key, 0)) + int(seconds)
            _save_daymap(self.focus, self.stats_path)

    def add_distraction(self, day: date | None = None) -> None:
        key = (day or date.today()).isoformat()
        with self.lock:
            self.distractions[key] = int(self.distractions.get(key, 0)) + 1
            _save_daymap(self.distractions, self.distract_path)

    def _sum(self, start: date, end: date) -> int:
        tot, cur = 0, start
        while cur <= end:
            tot += int(self.focus.get(cur.isoformat(), 0))
            cur += timedelta(days=1)
        return tot

    def today(self, when: date | None = None) -> int:
        return int(self.focus.get((when or date.today()).isoformat(), 0))

    def week(self, when: date | None = None) -> int:
        d = when or date.today()
        start = d - timedelta(days=d.weekday())
        return self._sum(start, start + timedelta(days=6))

    def month(self, when: date | None = None) -> int:
        d = when or date.today()
        start = d.replace(day=1)
        nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
        return self._sum(start, nxt - timedelta(days=1))

    def total(self) -> int:
        return sum(self.focus.values())

    def streak(self, when: date | None = None) -> int:
        d = when or date.today()
        if self.focus.get(d.isoformat(), 0) <= 0:
            d -= timedelta(days=1)
        n = 0
        while self.focus.get(d.isoformat(), 0) > 0:
            n += 1
            d -= timedelta(days=1)
        return n

    def last_days(self, n: int = 7, when: date | None = None) -> list[tuple[date, int]]:
        end = when or date.today()
        return [(end - timedelta(days=i), int(self.focus.get((end - timedelta(days=i)).isoformat(), 0)))
                for i in range(n - 1, -1, -1)]

    def distractions_today(self, when: date | None = None) -> int:
        return int(self.distractions.get((when or date.today()).isoformat(), 0))


def format_duration(seconds: int | float) -> str:
    total_min = max(0, int(round(float(seconds)))) // 60
    if total_min < 60:
        return f"{total_min} мин"
    h, m = divmod(total_min, 60)
    return f"{h} ч" if m == 0 else f"{h} ч {m} мин"


def format_days(n: int) -> str:
    return f"{n} {plural_ru(n, 'день', 'дня', 'дней')}"
