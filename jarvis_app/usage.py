"""Day usage: how long the foreground looked like work vs a distraction (TikTok, Shorts…).

Samples are taken by the core ticker. Stored as seconds per day, same folder as focus stats.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta

from .config import DATA_DIR, ensure_dir

USAGE_PATH = os.path.join(DATA_DIR, "usage.json")


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save(data: dict, path: str) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


class Usage:
    def __init__(self, path: str = USAGE_PATH) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.days = _load(path)

    def add(self, kind: str, seconds: float, day: date | None = None) -> None:
        if seconds <= 0 or kind not in ("work", "distract", "other"):
            return
        key = (day or date.today()).isoformat()
        with self.lock:
            row = self.days.setdefault(key, {"work": 0, "distract": 0, "other": 0})
            row[kind] = int(row.get(kind, 0)) + int(seconds)
            cutoff = (date.today() - timedelta(days=45)).isoformat()
            for k in [k for k in self.days if k < cutoff]:
                self.days.pop(k, None)
            _save(self.days, self.path)

    def today(self, when: date | None = None) -> dict:
        key = (when or date.today()).isoformat()
        with self.lock:
            row = dict(self.days.get(key) or {})
        return {
            "work": int(row.get("work", 0)),
            "distract": int(row.get("distract", 0)),
            "other": int(row.get("other", 0)),
        }


def report_text(usage: dict, focus_sec: int, nudges: int) -> str:
    def fmt(sec: int) -> str:
        sec = max(0, int(sec))
        h, rem = divmod(sec, 3600)
        m = rem // 60
        if h:
            return f"{h} ч {m} мин" if m else f"{h} ч"
        return f"{m} мин"

    w, d, o = usage.get("work", 0), usage.get("distract", 0), usage.get("other", 0)
    lines = [
        f"За сегодня: работа {fmt(w)}, отвлечения (TikTok, шортсы и похожее) {fmt(d)}, остальное {fmt(o)}.",
    ]
    if focus_sec:
        lines.append(f"Фокус-сессии: {fmt(focus_sec)}.")
    if nudges:
        lines.append(f"Страж фокуса возвращал {nudges} раз.")
    if d > w and d >= 15 * 60:
        lines.append("Отвлечений больше, чем работы.")
    elif w >= 25 * 60 and d < w:
        lines.append("Работа сегодня перевешивает ленту.")
    return " ".join(lines)
