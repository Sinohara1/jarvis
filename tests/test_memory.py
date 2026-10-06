"""Unit tests: configurable companion memory (jarvis_app/memory.py + brain/actions/config wiring).

No network, no Ollama: a capturing fake provider sees the system prompt and tool list.
Run: python tests/test_memory.py   (or python -m pytest -q tests/test_memory.py)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_app import memory as M
from jarvis_app import providers as P
from jarvis_app.actions import TOOLS, Actions
from jarvis_app.brain import Brain, needs_tools
from jarvis_app.config import DEFAULT_SETTINGS, normalize_settings


WORDS = ["кофе", "чай", "пиццу", "суши", "футбол", "шахматы", "джаз", "рок", "горы", "море", "кошек", "собак",
         "зиму", "лето", "книги", "аниме", "сериалы", "велосипед", "бег", "плавание"]


def _store(settings: dict | None = None, path: str | None = None) -> M.MemoryStore:
    s = normalize_settings(settings or {})
    path = path or os.path.join(tempfile.mkdtemp(prefix="jmem_"), "memory.json")
    return M.MemoryStore(path=path, get_settings=lambda: s)


# ─── parsing / guards ───────────────────────────────────────────────────────

def test_explicit_fact_positive():
    cases = {
        "Запомни, что я люблю кофе без сахара": "Я люблю кофе без сахара",
        "Джарвис, запомни: мою сестру зовут Аня": "Мою сестру зовут Аня",
        "запомни пожалуйста что у меня кот Барсик.": "У меня кот Барсик",
        "Пятница, запомни, что дедлайн проекта 20 октября": "Дедлайн проекта 20 октября",
        "запиши в память что я учусь в 10 классе": "Я учусь в 10 классе",
        "remember that I prefer dark mode": "I prefer dark mode",
    }
    for src, want in cases.items():
        assert M.explicit_fact(src) == want, (src, M.explicit_fact(src))


def test_explicit_fact_negative():
    for src in ("запомни это", "ты запомнил?", "Помоги запомнить формулу", "напомни через 5 минут выпить воды",
                "как мне запомнить таблицу умножения", "открой телеграм", "", "запомни"):
        assert M.explicit_fact(src) == "", src


def test_looks_secret():
    for t in ("мой пароль от почты qwerty123", "API key sk-abcdefghijklmnop", "карта 4111 1111 1111 1111",
              "IBAN DE89 3704 0044 0532 0130 00", "токен бота example-not-a-real-bot-token",
              "пин-код 1234", "AIzaSyA1234567890abcdefghijklmnopqrs"):
        assert M.looks_secret(t), t
    for t in ("я люблю кофе", "Сестру зовут Аня, ей 15 лет", "Учусь в 10 классе", "телефон мамы +49 151 23456789",
              "Дедлайн проекта 20 октября 2026"):
        assert not M.looks_secret(t), t


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("OK", name)
            except Exception as e:
                failed += 1
                print("FAIL", name, type(e).__name__, e)
    if failed:
        raise SystemExit(failed)
    print("all ok")
