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


def test_looks_secret():
    for t in ("мой пароль от почты qwerty123", "API key sk-abcdefghijklmnop",
              "токен бота example-not-a-real-bot-token", "пин-код 1234"):
        assert M.looks_secret(t), t


if __name__ == "__main__":
    print("all ok")
