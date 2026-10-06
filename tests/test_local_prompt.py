"""Unit tests: local (Ollama) prompt wiring for name, answer language, and open_app tools.

No Ollama/GPU required — asserts the system prompt and tool schemas the local provider would see.
Run: python tests/test_local_prompt.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_app.actions import TOOLS
from jarvis_app.brain import Brain, build_system_prompt, local_model_hint
from jarvis_app.config import normalize_settings
from jarvis_app import lang as L
from jarvis_app import persona
from jarvis_app import providers as P


class CapturingProv(P.Provider):
    name = "ollama"

    def __init__(self):
        self.last_system = ""
        self.last_tools = None

    def user_message(self, text, image_jpeg=None):
        return {"role": "user", "content": text}

    def assistant_message(self, text):
        return {"role": "assistant", "content": text}

    def tool_result_messages(self, calls, results):
        return [{"role": "tool", "content": "{}"} for _ in results]

    def chat_stream(self, model, system, messages, tools=None, *, on_text, timeout=40.0, temperature=0.7,
                    max_tokens=2048):
        self.last_system = system or ""
        self.last_tools = tools
        on_text("Ок.")
        return P.LLMResult(text="Ок.", tool_calls=[], raw_message={"role": "assistant", "content": "Ок."})

    def chat(self, model, system, messages, tools=None, *, timeout=40.0, temperature=0.7):
        return self.chat_stream(model, system, messages, tools, on_text=lambda d: None)


class Route:
    def route(self, kind, cloud, ok):
        return ["ollama"]

    def keep_alive(self):
        return "10m"

    def note_result(self, ok, err=""):
        pass


def _brain(settings):
    s = normalize_settings(settings)
    b = Brain(lambda: s, None, lambda: "")
    cap = CapturingProv()
    b.provider = lambda name=None: cap
    b.models = lambda name=None: ("gemma4:12b", "gemma4:12b")
    b.local = Route()
    return b, cap, s


def test_build_system_prompt_uses_assistant_name_and_language():
    s = normalize_settings({"assistant_name": "Айри", "answer_lang": "ru"})
    p = build_system_prompt(s)
    assert "Айри" in p and "Тебя зовут Айри" in p
    assert "по-русски" in p
    assert "Jarvis" not in p or "Джарвис из" in p  # character may mention Jarvis as style only


def test_local_hint_name_language_tools():
    s = normalize_settings({"assistant_name": "Айри", "answer_lang": "ru"})
    h = local_model_hint(s, "ru")
    assert "Айри" in h
    assert "Не называй себя Jarvis" in h or "не Джарвис" in h.lower() or "Не называй себя Jarvis" in h
    assert "по-русски" in h or "русск" in h.lower()
    assert "open_app" in h and "Telegram" in h
    # default name: no anti-Jarvis line needed
    s2 = normalize_settings({"assistant_name": "Джарвис", "answer_lang": "en"})
    h2 = local_model_hint(s2, "en")
    assert "Джарвис" in h2 and "Never reply in English" not in h2
    assert "English" in h2 or "английск" in h2


def test_ask_local_injects_hint_and_open_app_schema():
    b, cap, s = _brain({"assistant_name": "Айри", "answer_lang": "ru", "ai_route": "local"})
    b.ask("открой телеграм", voice=True)
    assert "Айри" in cap.last_system
    assert "Локальная модель" in cap.last_system
    assert "по-русски" in cap.last_system
    names = {t["name"] for t in (cap.last_tools or [])}
    assert "open_app" in names
    oa = next(t for t in cap.last_tools if t["name"] == "open_app")
    assert "Telegram" in oa["description"]
    assert "открой" in oa["description"].lower()


def test_tools_list_includes_open_app():
    names = [t["name"] for t in TOOLS]
    assert names[0] == "open_app"
    assert "Telegram" in TOOLS[0]["description"]


def test_russian_native_rule_present():
    s = normalize_settings({"answer_lang": "ru"})
    rule = L.prompt_rule(s)
    assert "по-русски" in rule
    assert "Never reply in English" in rule or "только по-русски" in rule
    turn = L.turn_rule(s, "ru")
    assert "по-русски" in turn
    assert "английск" in turn.lower() or "English" in turn


def test_persona_name_helper():
    s = normalize_settings({"assistant_name": "Airi"})
    assert persona.assistant_name(s) == "Airi"
    assert not persona.is_default_name("Airi")
    assert persona.is_default_name("Jarvis")


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
