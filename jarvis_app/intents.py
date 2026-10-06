"""Local voice intents that should not wait for the model: reminders and the day report."""

from __future__ import annotations

import re

_NUM = {
    "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "полчаса": 30,
    "час": 60, "полтора": 90,
}


def _n(tok: str) -> float | None:
    tok = tok.replace(",", ".")
    if tok in _NUM:
        return float(_NUM[tok])
    try:
        return float(tok)
    except ValueError:
        return None


def parse_reminder(text: str) -> dict | None:
    """«напомни через 20 минут выключить духовку» / «таймер на 5 минут» / «напомни в 18:30 …»."""
    raw = (text or "").strip()
    t = raw.lower().replace("ё", "е")
    if not re.search(r"напомн|таймер|поставь таймер|засеки|разбуди", t):
        return None
    if re.search(r"отмени|удали|сбрось", t) and re.search(r"напомин|таймер", t):
        return {"cancel": True}
    if re.search(r"какие напомин|список напомин|что запланир", t):
        return {"list": True}
    m = re.search(r"в\s+(\d{1,2})[:.](\d{2})", t)
    if m:
        body = _body(raw, m.end())
        return {"at_time": f"{int(m.group(1)):02d}:{m.group(2)}", "text": body or "напоминание"}
    m = re.search(
        r"(?:через|на)\s+(\d+(?:[.,]\d+)?|один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|"
        r"пятнадцать|двадцать|тридцать|сорок|пятьдесят|полчаса|час|полтора)\s*"
        r"(секунд\w*|сек\b|минут\w*|мин\b|час\w*)",
        t,
    )
    if not m and re.search(r"полчаса", t):
        minutes, end = 30.0, t.find("полчаса") + 7
    elif m:
        n = _n(m.group(1))
        if n is None:
            return None
        unit = m.group(2)
        minutes = n / 60.0 if unit.startswith("сек") else n * 60.0 if unit.startswith("час") else n
        end = m.end()
    else:
        return None
    body = _body(raw, end)
    return {"minutes": round(minutes, 2), "text": body or "таймер"}


def _body(raw: str, end: int) -> str:
    rest = raw[end:].strip(" .,!:-—")
    rest = re.sub(r"^(чтобы|что|про|о|об)\s+", "", rest, flags=re.I)
    return rest[:180]


def wants_day_report(text: str) -> bool:
    t = (text or "").lower().replace("ё", "е")
    return bool(re.search(r"отч[её]т за день|фокус[- ]?отч|сколько.*(тикток|отвлек|работал)|как прошел день|итоги дня", t))
