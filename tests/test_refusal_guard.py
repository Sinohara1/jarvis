"""Refusal → tool override at pc_control=full (no Windows, no network).

The model answering a clear PC command with «не могу / технические ограничения» must never reach the
user: Brain holds such a reply back and runs the matching tool itself (media_control, press_hotkey,
app_search, telegram_open_chat, type_text, window_control…), or retries the model once with a forcing
hint. Hard limits (disk wipe / format, mass delete, UAC bypass) keep the refusal.
Run: python -m pytest tests/test_refusal_guard.py -q   (or: python tests/test_refusal_guard.py)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_app import providers as P  # noqa: E402
from jarvis_app import refusal_guard as RG  # noqa: E402
from jarvis_app.actions import TOOLS  # noqa: E402
from jarvis_app.brain import Brain, _RefusalGate, build_system_prompt, local_model_hint, needs_tools  # noqa: E402
from jarvis_app.config import normalize_settings  # noqa: E402

FULL = {"pc_control": "full"}
STD = {"pc_control": "standard"}
REFUSAL_RU = "К сожалению, из-за технических ограничений я не могу нажать кнопку воспроизведения. Нажми её сам."
REFUSAL_EN = "Sorry, I can't click buttons on your screen due to a technical limitation."


# ─── detection ───────────────────────────────────────────────────────────────

def test_is_refusal_ru_en():
    for r in (REFUSAL_RU, REFUSAL_EN,
              "Я не могу открывать личные чаты.",
              "У меня нет доступа к твоему экрану, так что нажать не получится.",
              "Я всего лишь ИИ и не умею кликать.",
              "Тебе придётся сделать это вручную.",
              "I'm unable to press keys for you.",
              "You'll need to click play yourself."):
        assert RG.is_refusal(r), r
    for ok in ("Нажимаю плей.", "Открываю Telegram.", "Готово, включил.", "Включаю следующий трек.",
               "Opening Spotify.", "Пауза.", ""):
        assert not RG.is_refusal(ok), ok


def test_action_requests_and_hard_limits():
    for t in ("нажми плей", "Джарвис, нажми на кнопку play", "press play", "можешь нажать плей?",
              "кликни по кнопке", "открой избранное в телеграме", "введи привет", "закрой хром",
              "следующий трек", "пауза"):
        assert RG.is_action_request(t), t
    for t in ("отформатируй диск C", "удали все файлы", "обойди UAC и запусти скрипт", "format c:",
              "сотри всё с диска", "bypass UAC please", "как дела?", "расскажи анекдот"):
        assert not RG.is_action_request(t), t
        assert RG.override_call(t, FULL) is None, t


def test_override_mapping_full():
    cases = {
        "нажми плей": ("media_control", {"action": "play"}),
        "Джарвис, нажми на кнопку play": ("media_control", {"action": "play"}),
        "press play": ("media_control", {"action": "play"}),
        "нажми плей в спотифае": ("media_control", {"action": "play", "app": "spotify"}),
        "продолжи видео": ("media_control", {"action": "play"}),
        "поставь на паузу": ("media_control", {"action": "pause"}),
        "следующий трек": ("media_control", {"action": "next"}),
        "включи Can't Fault Das в спотике": ("media_control", {"action": "play", "app": "spotify",
                                                              "query": "Can't Fault Das"}),
        "нажми пробел": ("press_hotkey", {"keys": "space"}),
        "нажми энтер": ("press_hotkey", {"keys": "enter"}),
        "нажми ctrl+c": ("press_hotkey", {"keys": "ctrl+c"}),
        "нажми альт ф4": ("press_hotkey", {"keys": "alt+f4"}),
        "нажми пробел в хроме": ("press_hotkey", {"keys": "space", "window": "хром"}),
        "закрой вкладку": ("press_hotkey", {"keys": "ctrl+w"}),
        "сверни все окна": ("press_hotkey", {"keys": "win+d"}),
        "введи привет мир": ("type_text", {"text": "привет мир"}),
        "напечатай hello и нажми энтер": ("type_text", {"text": "hello", "press_enter": True}),
        "напиши в блокноте: список покупок": ("type_text", {"text": "список покупок", "window": "notepad"}),
        "закрой хром": ("window_control", {"action": "close", "name": "хром"}),
        "закрой это окно": ("window_control", {"action": "close", "name": ""}),
        "сверни телеграм": ("window_control", {"action": "minimize", "name": "телеграм"}),
        "переключись на дискорд": ("window_control", {"action": "focus", "name": "дискорд"}),
        "открой избранное в телеграме": ("telegram_open_chat", {"chat": "Saved Messages"}),
        "открой чат с Мамой в телеграме": ("telegram_open_chat", {"chat": "Мама"}),
        "напиши в тг Саше: буду в 8": ("send_telegram", {"contact": "Саше", "message": "буду в 8"}),
        "Write Mom on Telegram, I'm home": ("send_telegram", {"contact": "Mom", "message": "I'm home"}),
        "найди general в дискорде": ("app_search", {"app": "дискорде", "query": "general"}),
        "открой телеграм": ("open_app", {"name": "телеграм"}),
        "открой youtube.com": ("open_url", {"url": "youtube.com"}),
    }
    for text, want in cases.items():
        assert RG.override_call(text, FULL) == want, (text, RG.override_call(text, FULL))


def test_override_respects_level():
    # media keys exist at «standard» → «нажми плей» still forced; desktop control does not
    assert RG.override_call("нажми плей", STD) == ("media_control", {"action": "play"})
    assert RG.override_call("нажми плей", {"pc_control": "safe"}) is None
    for t in ("нажми пробел", "закрой хром", "введи привет", "открой избранное в телеграме",
              "открой чат с Мамой в телеграме", "найди general в дискорде"):
        assert RG.override_call(t, STD) is None, t
    assert RG.guard_active(FULL, "кликни по кнопке")          # unmapped, but full → retry with forcing hint
    assert not RG.guard_active(STD, "кликни по кнопке")       # standard: the «нужен полный» answer is honest
    assert not RG.guard_active(FULL, "отформатируй диск C")


def test_needs_tools_catches_press_click():
    for t in ("нажми на пробел пожалуйста", "кликни по кнопке плей", "press the play button now"):
        assert needs_tools(t), t


def test_gate_holds_refusal_and_passes_normal_text():
    out = []
    g = _RefusalGate(out.append)
    for d in ("Хорошо. ", "К сожалению, ", "из-за технических ограничений ", "я не могу нажать."):
        g.feed(d)
    g.close()
    assert g.dropped and out == []
    out2 = []
    g2 = _RefusalGate(out2.append)
    for d in ("Нажимаю плей, ", "сейчас всё будет.", " Ещё что-то?"):
        g2.feed(d)
    g2.close()
    assert not g2.dropped and "".join(out2) == "Нажимаю плей, сейчас всё будет. Ещё что-то?"


# ─── prompts / tool descriptions ─────────────────────────────────────────────

def test_full_prompt_forbids_soft_refusals_keeps_hard_limits():
    pf = build_system_prompt(normalize_settings(FULL))
    assert "технические ограничения" in pf and "НИКОГДА" in pf
    assert "не могу нажать плей" in pf and "press_hotkey" in pf and "media_control" in pf
    assert "правда невозможно" not in pf            # old loophole («then say honestly you can't») is gone
    assert "UAC" in pf and "форматирования дисков" in pf  # real hard limits stay
    h = local_model_hint(normalize_settings(FULL), "ru")
    assert "НИКОГДА" in h and "технические" in h and "нажми плей" in h and "press_hotkey" in h
    ps = build_system_prompt(normalize_settings(STD))
    assert "НИКОГДА не говори «не могу нажать" not in ps  # standard keeps its honest «нужен полный» line


def test_tool_descriptions_play_buttons():
    by = {t["name"]: t["description"] for t in TOOLS}
    assert "нажми плей" in by["media_control"] and "ограничения" in by["media_control"]
    assert "space" in by["press_hotkey"] and "не говори «не могу нажать" in by["press_hotkey"]
    assert "мягкое закрытие разрешено" in by["window_control"]
    assert "UAC" in by["run_elevated"] and "Массовое удаление / format" in by["run_command"]  # hard limits kept


# ─── Brain integration ───────────────────────────────────────────────────────

class ScriptProv(P.Provider):
    """Replays scripted replies: each item is (text, [ToolCall...])."""
    name = "gemini"

    def __init__(self, script):
        self.script = list(script)
        self.systems: list[str] = []
        self.seen: list[list] = []

    def user_message(self, text, image_jpeg=None):
        return {"role": "user", "content": text}

    def assistant_message(self, text):
        return {"role": "assistant", "content": text}

    def tool_result_messages(self, calls, results):
        return [{"role": "tool", "content": json.dumps(r, ensure_ascii=False)} for r in results]

    def chat_stream(self, model, system, messages, tools=None, *, on_text, timeout=40.0, temperature=0.7,
                    max_tokens=2048):
        self.systems.append(system or "")
        self.seen.append([dict(m) for m in messages])
        text, calls = self.script.pop(0) if self.script else ("", [])
        # stream in small chunks like a real provider
        for i in range(0, len(text), 12):
            on_text(text[i:i + 12])
        return P.LLMResult(text=text, tool_calls=list(calls), raw_message={"role": "assistant", "content": text})

    def chat(self, model, system, messages, tools=None, *, timeout=40.0, temperature=0.7):
        return self.chat_stream(model, system, messages, tools, on_text=lambda d: None)


class Acts:
    def __init__(self, result=None):
        self.calls: list[tuple[str, dict]] = []
        self.result = result or {"ok": True}

    def execute(self, name, args):
        self.calls.append((name, dict(args)))
        return dict(self.result)


def _brain(settings, script, result=None):
    s = normalize_settings(settings)
    acts = Acts(result)
    b = Brain(lambda: s, acts, lambda: "")
    prov = ScriptProv(script)
    b.provider = lambda name=None: prov
    b.models = lambda name=None: ("m", "m")
    return b, prov, acts


def test_brain_overrides_play_refusal_with_media_control():
    b, prov, acts = _brain(FULL, [(REFUSAL_RU, [])])
    streamed, tools = [], []
    reply = b.ask("Джарвис, нажми плей", on_text=streamed.append, on_tool=lambda n, a: tools.append(n), voice=True)
    assert acts.calls == [("media_control", {"action": "play"})]
    assert tools == ["media_control"]
    assert "".join(streamed) == ""                       # the refusal was never spoken / shown
    assert reply == "Включил." and "огранич" not in reply
    assert all("огранич" not in str(m.get("content")) for m in b.history)  # not kept to prime later turns
    assert b.transcript[-1] == ("assistant", "Включил.")


def test_brain_overrides_english_refusal_and_telegram():
    b, prov, acts = _brain(FULL, [(REFUSAL_EN, [])])
    b.ask("press play", on_text=lambda d: None, lang="en")
    assert acts.calls == [("media_control", {"action": "play"})]
    b, prov, acts = _brain(FULL, [("Извини, я не могу открывать личные чаты пользователя.", [])])
    reply = b.ask("открой чат с Мамой в телеграме", on_text=lambda d: None)
    assert acts.calls == [("telegram_open_chat", {"chat": "Мама"})] and reply == "Открыл чат."


def test_brain_override_window_type_hotkey_app_search():
    for text, want in (("закрой хром", "window_control"), ("введи привет мир", "type_text"),
                       ("нажми пробел", "press_hotkey"), ("найди general в дискорде", "app_search")):
        b, prov, acts = _brain(FULL, [("Не могу этого сделать из-за технических ограничений.", [])])
        b.ask(text, on_text=lambda d: None)
        assert acts.calls and acts.calls[0][0] == want, (text, acts.calls)


def test_brain_override_tool_error_is_spoken():
    b, prov, acts = _brain(FULL, [(REFUSAL_RU, [])], result={"ok": False, "error": "не вижу окно «хром»"})
    streamed = []
    reply = b.ask("закрой хром", on_text=streamed.append)
    assert "не вышло" in reply and "хром" in reply and "".join(streamed) == reply


def test_brain_unmapped_refusal_retries_with_forcing_hint():
    call = P.ToolCall(id="1", name="press_hotkey", args={"keys": "enter"})
    b, prov, acts = _brain(FULL, [("Я не могу кликать по кнопкам, это техническое ограничение.", []),
                                  ("Жму.", [call]), ("", [])])
    streamed = []
    reply = b.ask("кликни по кнопке", on_text=streamed.append)
    assert acts.calls == [("press_hotkey", {"keys": "enter"})]
    assert RG.FORCE_TOOL_HINT.strip() in prov.systems[1] and RG.FORCE_TOOL_HINT.strip() not in prov.systems[0]
    assert "огранич" not in "".join(streamed) and "огранич" not in reply
    # the retried request did not contain the refusal
    assert [m["content"] for m in prov.seen[1]] == ["кликни по кнопке"]


def test_brain_still_refusing_after_retry_asks_to_clarify():
    b, prov, acts = _brain(FULL, [("Не могу, ограничения.", []), ("Всё равно не могу.", [])])
    streamed = []
    reply = b.ask("кликни по кнопке", on_text=streamed.append)
    assert acts.calls == [] and reply == RG.CLARIFY["ru"] and "".join(streamed) == reply


def test_brain_normal_tool_reply_untouched():
    call = P.ToolCall(id="1", name="media_control", args={"action": "play"})
    b, prov, acts = _brain(FULL, [("Нажимаю плей.", [call]), ("", [])])
    streamed = []
    reply = b.ask("нажми плей", on_text=streamed.append)
    assert "".join(streamed) == "Нажимаю плей." and reply == "Нажимаю плей."
    assert acts.calls == [("media_control", {"action": "play"})] and len(prov.systems) == 2


def test_brain_keeps_hard_limit_refusal():
    msg = "Форматировать диск не буду — это жёсткий запрет, даже по просьбе. Не могу."
    b, prov, acts = _brain(FULL, [(msg, [])])
    streamed = []
    reply = b.ask("отформатируй диск C", on_text=streamed.append)
    assert acts.calls == [] and reply == msg and "".join(streamed) == msg


def test_brain_standard_level_honest_refusal_kept_for_desktop_control():
    msg = "Не могу закрыть окно на уровне «стандарт» — включи «полный» в настройках."
    b, prov, acts = _brain(STD, [(msg, [])])
    reply = b.ask("закрой хром", on_text=lambda d: None)
    assert acts.calls == [] and reply == msg
    # …but media keys are available at standard → «нажми плей» refusal is still overridden
    b, prov, acts = _brain(STD, [(REFUSAL_RU, [])])
    b.ask("нажми плей", on_text=lambda d: None)
    assert acts.calls == [("media_control", {"action": "play"})]


def test_brain_override_without_streaming():
    b, prov, acts = _brain(FULL, [(REFUSAL_RU, [])])
    assert b.ask("нажми плей") == "Включил." and acts.calls == [("media_control", {"action": "play"})]


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # pragma: no cover
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
