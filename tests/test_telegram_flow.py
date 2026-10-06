"""Telegram Desktop flow: target classification, step plan (Esc → Ctrl+F → paste → Enter), executor with a
fake driver (foreground loss, tg:// fallback), app_search routing, contact aliases. No Windows needed."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jarvis_app.pc_power as pp  # noqa: E402
from jarvis_app.actions import Actions  # noqa: E402
from jarvis_app.stt_commands import contact_aliases, resolve_contact, voice_names  # noqa: E402


class FakeDriver:
    def __init__(self, *, uri_ok=True, uri_works=True, fg_script=None, focus_ok=True, paste_ok=True):
        self.uri_ok = uri_ok
        self.uri_works = uri_works
        self.fg_script = list(fg_script or [])   # successive is_foreground() answers (default True)
        self.focus_ok = focus_ok
        self.paste_ok = paste_ok
        self.log: list[tuple] = []

    def focus(self):
        self.log.append(("focus",))
        return self.focus_ok

    def is_foreground(self):
        return self.fg_script.pop(0) if self.fg_script else True

    def keys(self, spec):
        self.log.append(("keys", spec))

    def paste(self, text, clear=True):
        self.log.append(("paste", text, clear))
        return self.paste_ok

    def sleep(self, sec):
        pass

    def open_uri(self, uri):
        self.log.append(("uri", uri))
        return self.uri_works

    def ops(self):
        return [x for x in self.log if x[0] != "focus"]


# ─── classification ──────────────────────────────────────────────────────────

def test_target_kinds():
    T = pp.telegram_target
    assert T("Избранное") == ("saved", "")
    assert T("Saved Messages") == ("saved", "")
    assert T("«избранное»") == ("saved", "")
    assert T("@durov") == ("username", "durov")
    assert T(" @Nehto_tg ") == ("username", "Nehto_tg")
    assert T("t.me/durov") == ("username", "durov")
    assert T("https://t.me/durov") == ("username", "durov")
    assert T("tg://resolve?domain=durov") == ("username", "durov")
    assert T("собака durov") == ("username", "durov")
    assert T("Мама") == ("name", "Мама")
    assert T("чат с Мамой") == ("name", "Мамой")
    assert T("Nehto") == ("name", "Nehto")
    assert T("Вася Пупкин") == ("name", "Вася Пупкин")
    assert T("@ab") == ("name", "ab")            # too short for a username → plain search
    assert T("") == ("", "")
    assert T("   ") == ("", "")


# ─── plan ────────────────────────────────────────────────────────────────────

def _keys(plan):
    return [s[1] for s in plan if s[0] == "keys"]


def test_plan_name_search_order_and_no_ctrl_k():
    plan = pp.telegram_plan("Мама")
    assert plan[0] == ("focus",)
    ops = [s[0] for s in plan if s[0] != "sleep"]
    assert ops == ["focus", "keys", "keys", "keys", "paste", "keys"]
    assert _keys(plan) == ["esc", "esc", "ctrl+f", "enter"]
    assert ("paste", "Мама") in plan
    assert all("ctrl+k" not in k for k in _keys(plan))
    # a wait for search results sits between paste and Enter
    i_paste = plan.index(("paste", "Мама"))
    i_enter = plan.index(("keys", "enter"))
    waits = [s[1] for s in plan[i_paste:i_enter] if s[0] == "sleep"]
    assert waits and sum(waits) >= 1.0


def test_plan_saved_messages():
    plan = pp.telegram_plan("избранное")
    assert _keys(plan) == ["ctrl+0"]
    assert not any(s[0] == "paste" for s in plan)


def test_plan_username_uri_and_fallback():
    plan = pp.telegram_plan("@durov", uri_ok=True)
    assert ("uri", "tg://resolve?domain=durov") in plan and not any(s[0] == "paste" for s in plan)
    assert plan[-1] == ("focus",)
    plan = pp.telegram_plan("@durov", uri_ok=False)
    assert ("paste", "@durov") in plan and _keys(plan) == ["esc", "esc", "ctrl+f", "enter"]
    i = plan.index(("paste", "@durov"))
    assert plan[i + 1] == ("sleep", pp.TG_TIMING["results_username"])  # server search needs longer


def test_plan_fresh_window_waits():
    plan = pp.telegram_plan("Мама", fresh=True)
    assert plan[1] == ("sleep", pp.TG_TIMING["fresh_window"])


def test_plan_latin_nick_without_at_waits_longer():
    plan = pp.telegram_plan("nehto_42")
    i = plan.index(("paste", "nehto_42"))
    assert plan[i + 1] == ("sleep", pp.TG_TIMING["results_username"])


# ─── executor ────────────────────────────────────────────────────────────────

def test_open_chat_by_name_with_fake_driver():
    d = FakeDriver()
    r = pp.telegram_open_chat("Мама", ensure_open=None, driver=d)
    assert r["ok"] and r["via"] == "search" and r["kind"] == "name" and r["chat"] == "Мама"
    assert d.ops() == [("keys", "esc"), ("keys", "esc"), ("keys", "ctrl+f"), ("paste", "Мама", True),
                       ("keys", "enter")]


def test_open_chat_username_uses_uri():
    d = FakeDriver(uri_ok=True)
    r = pp.telegram_open_chat("@durov", ensure_open=None, driver=d)
    assert r["ok"] and r["via"] == "tg_protocol" and r["chat"] == "@durov"
    assert d.ops() == [("uri", "tg://resolve?domain=durov")]


def test_open_chat_uri_failure_falls_back_to_search():
    d = FakeDriver(uri_ok=True, uri_works=False)
    r = pp.telegram_open_chat("@durov", ensure_open=None, driver=d)
    assert r["ok"] and r["via"] == "search"
    assert ("paste", "@durov", True) in d.log and ("keys", "ctrl+f") in d.log


def test_open_saved_messages():
    d = FakeDriver()
    r = pp.telegram_open_chat("Saved Messages", ensure_open=None, driver=d)
    assert r["ok"] and r["via"] == "ctrl+0" and d.ops() == [("keys", "ctrl+0")]


def test_refocus_when_foreground_lost():
    d = FakeDriver(fg_script=[True, False])  # lost focus before the 2nd Esc
    r = pp.telegram_open_chat("Мама", ensure_open=None, driver=d)
    assert r["ok"]
    assert d.log.count(("focus",)) == 2


def test_focus_failure_is_reported():
    d = FakeDriver(focus_ok=False)
    r = pp.telegram_open_chat("Мама", ensure_open=None, driver=d)
    assert not r["ok"] and "передний план" in r["error"]
    assert d.ops() == []  # nothing typed into some other window


def test_clipboard_failure_is_reported():
    d = FakeDriver(paste_ok=False)
    r = pp.telegram_open_chat("Мама", ensure_open=None, driver=d)
    assert not r["ok"] and "буфер" in r["error"]
    assert ("keys", "enter") not in d.log


def test_send_message_keeps_draft_and_presses_enter():
    d = FakeDriver()
    r = pp.send_telegram_desktop("Мама", "я дома", ensure_open=None, driver=d)
    assert r["ok"] and r["message_len"] == len("я дома")
    ops = d.ops()
    assert ops[-2:] == [("paste", "я дома", False), ("keys", "enter")]
    assert ops.index(("paste", "Мама", True)) < ops.index(("paste", "я дома", False))


def test_send_empty_rejected():
    assert not pp.send_telegram_desktop("Мама", "  ", ensure_open=None, driver=FakeDriver())["ok"]
    assert not pp.send_telegram_desktop("", "hi", ensure_open=None, driver=FakeDriver())["ok"]


def test_non_windows_without_driver():
    if pp.IS_WIN:
        return
    assert not pp.telegram_open_chat("Мама", ensure_open=None)["ok"]
    assert not pp.send_telegram_desktop("Мама", "hi", ensure_open=None)["ok"]


# ─── routing ─────────────────────────────────────────────────────────────────

def test_effective_search_keys_never_ctrl_k_for_telegram():
    assert pp.effective_search_keys("Telegram", "ctrl+k") == "esc; esc; ctrl+f"
    assert pp.effective_search_keys("телеграм", "") == "esc; esc; ctrl+f"
    assert pp.effective_search_keys("тг", "esc; ctrl+f") == "esc; ctrl+f"
    assert pp.effective_search_keys("Discord", "") == "ctrl+k"
    assert pp.effective_search_keys("Notepad++", "") == "ctrl+f"


class _Core:
    settings = {"pc_control": "full", "voice_contacts": ["Мама=@mama_tg", "Nehto", "Саша"]}
    addons = None


def _actions():
    a = Actions.__new__(Actions)
    a.core = _Core()
    a.apps = None
    return a


def test_actions_route_telegram_everywhere():
    import jarvis_app.actions as act
    seen = []
    o1, o2, o3 = act.telegram_open_chat, act.app_search, act.send_telegram_desktop
    act.telegram_open_chat = lambda chat, ensure_open=None: seen.append(("tg", chat)) or {"ok": True}
    act.app_search = lambda app, q, **kw: seen.append(("app", app, q)) or {"ok": True}
    act.send_telegram_desktop = lambda c, m, ensure_open=None: seen.append(("send", c, m)) or {"ok": True}
    try:
        a = _actions()
        a.execute("app_search", {"app": "Telegram", "query": "Nehto", "search_keys": "ctrl+k"})
        a.execute("app_search", {"app": "тг", "query": "Мама"})
        a.execute("telegram_open_chat", {"chat": "Сашей"})
        a.execute("telegram_open_chat", {"contact": "@durov"})
        a.execute("send_telegram", {"contact": "мама", "message": "hi"})
        a.execute("app_search", {"app": "Discord", "query": "general"})
        assert seen == [("tg", "Nehto"), ("tg", "@mama_tg"), ("tg", "Саша"), ("tg", "@durov"),
                        ("send", "@mama_tg", "hi"), ("app", "Discord", "general")]
    finally:
        act.telegram_open_chat, act.app_search, act.send_telegram_desktop = o1, o2, o3


def test_contact_aliases_and_declension():
    s = {"voice_contacts": ["Мама=@mama_tg", "Nehto", "Саша", "Вася Пупкин = Василий П."]}
    assert voice_names(s) == ["Мама", "Nehto", "Саша", "Вася Пупкин"]
    assert contact_aliases(s) == {"мама": "@mama_tg", "вася пупкин": "Василий П."}
    assert resolve_contact("Мамой", s) == "@mama_tg"
    assert resolve_contact("Саше", s) == "Саша"
    assert resolve_contact("nehto", s) == "Nehto"
    assert resolve_contact("Петя", s) == "Петя"
    assert resolve_contact("вася пупкин", s) == "Василий П."


def test_instrumental_to_nominative():
    f = pp.ru_instrumental_to_nominative
    assert [f(w) for w in ("Мамой", "Сашей", "Олей", "Сергеем", "Олегом", "Игорем", "Nehto", "Ян")] == \
        ["Мама", "Саша", "Оля", "Сергей", "Олег", "Игор", "Nehto", "Ян"]


def test_chat_intent():
    I = pp.telegram_chat_intent
    assert I("открой чат с Nehto в телеграме") == "Nehto"
    assert I("Open chat with @durov on Telegram") == "@durov"
    assert I("зайди в чат с Мамой в тг") == "Мама"
    assert I("открой переписку с Сергеем в телеграме") == "Сергей"
    assert I("open chat with Nehto on telegram") == "Nehto"
    assert I("открой чат с Nehto") == ""                       # no app mentioned, no context
    assert I("открой чат с Nehto", telegram_context=True) == "Nehto"
    assert I("перейди к Саше в телеграме") == "Саше"
    assert I("открой телеграм") == ""
    assert I("какая погода") == ""


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
