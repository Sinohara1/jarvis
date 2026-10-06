"""Unit tests: Spotify search-play, Telegram open chat / Saved Messages, generic app_search (no Windows needed).

Win32 calls are not executed here; we test parsing, routing, schemas and that the right code path is chosen.
Run: python -m pytest tests/test_app_control.py -q   (or: python tests/test_app_control.py)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_app import pc_power as pp
from jarvis_app import spotify as sp
from jarvis_app.actions import TOOLS, Actions, tools_for_settings
from jarvis_app.brain import build_system_prompt, local_model_hint, needs_tools
from jarvis_app.config import normalize_settings


def test_clean_query_ru_en():
    cases = {
        "включи Can't Fault Das в спотике": "Can't Fault Das",
        "Play Can't Fault Das on Spotify": "Can't Fault Das",
        "поставь песню Numb Linkin Park на спотифае пожалуйста": "Numb Linkin Park",
        "Джарвис, включи трек «Кино — Группа крови» в Spotify": "Кино — Группа крови",
        "в спотике включи Rammstein Sonne": "Rammstein Sonne",
        "Spotify play Daft Punk": "Daft Punk",
        "Can't Fault Das": "Can't Fault Das",
    }
    for raw, want in cases.items():
        assert sp.clean_query(raw) == want, (raw, sp.clean_query(raw))


def test_spotify_intent():
    assert sp.spotify_play_intent("включи Can't Fault Das в спотике") == "Can't Fault Das"
    assert sp.spotify_play_intent("play bohemian rhapsody in spotify") == "bohemian rhapsody"
    assert sp.spotify_play_intent("включи музыку в спотике") is None
    assert sp.spotify_play_intent("включи спотик") is None
    assert sp.spotify_play_intent("включи свет на кухне") is None


def test_search_uri():
    assert sp.search_uri("Can't Fault Das") == "spotify:search:Can%27t%20Fault%20Das"
    assert sp.search_uri("Кино") .startswith("spotify:search:%D0%9A")


def test_api_credentials_optional():
    for k in ("JARVIS_SPOTIFY_CLIENT_ID", "JARVIS_SPOTIFY_CLIENT_SECRET", "JARVIS_SPOTIFY_REFRESH_TOKEN"):
        os.environ.pop(k, None)
    assert sp.api_credentials({}) is None
    assert sp.api_credentials({"spotify_client_id": "a", "spotify_client_secret": "b",
                               "spotify_refresh_token": "c"}) == ("a", "b", "c")


def test_key_sequences():
    seq = pp.parse_key_sequence("esc; esc; ctrl+f")
    assert seq == [([], 0x1B), ([], 0x1B), ([pp.VK_CONTROL], ord("F"))]
    assert len(pp.parse_key_sequence("down*3; enter")) == 4
    assert pp.parse_key_sequence("tab then enter") == [([], 0x09), ([], 0x0D)]
    assert pp.parse_key_sequence("") == []
    assert pp.parse_key_sequence("ctrl+alt+del") is None
    assert pp.parse_key_sequence("bogus+key") is None


def test_presets_and_saved_messages():
    assert pp.resolve_app_preset("Spotify")[0] == "spotify"
    assert pp.resolve_app_preset("спотик")[0] == "spotify"
    assert pp.resolve_app_preset("телеграм")[0] == "telegram"
    assert pp.resolve_app_preset("Google Chrome")[0] == "chrome"
    assert pp.resolve_app_preset("Visual Studio Code")[0] == "vscode"
    assert pp.resolve_app_preset("SomeRandomApp") is None
    assert pp.APP_SEARCH_PRESETS["spotify"]["search"] == "ctrl+k"
    assert "ctrl+f" in pp.APP_SEARCH_PRESETS["telegram"]["search"]  # Ctrl+K in TDesktop = create link
    for t in ("Избранное", "saved messages", "Saved Messages", "сохранённые сообщения", "«избранное»"):
        assert pp.is_saved_messages(t), t
    assert not pp.is_saved_messages("Мама")
    assert pp.telegram_saved_intent("открой избранное в телеграме")
    assert pp.telegram_saved_intent("Open Saved Messages on Telegram")
    assert not pp.telegram_saved_intent("открой телеграм")


def test_tool_schemas_and_levels():
    by = {t["name"]: t for t in TOOLS}
    mc = by["media_control"]
    assert "query" in mc["parameters"]["properties"]
    assert "Can't Fault Das" in mc["description"]
    for n in ("telegram_open_chat", "app_search"):
        assert n in by and pp.TOOL_MIN_LEVEL[n] == "full"
    std = {t["name"] for t in tools_for_settings({"pc_control": "standard"})}
    full = {t["name"] for t in tools_for_settings({"pc_control": "full"})}
    assert "media_control" in std and "app_search" not in std and "telegram_open_chat" not in std
    assert {"app_search", "telegram_open_chat", "media_control"} <= full


def test_prompts_mention_search_play_and_no_refusal():
    pf = build_system_prompt(normalize_settings({"pc_control": "full"}))
    assert "query" in pf and "telegram_open_chat" in pf and "app_search" in pf
    assert "не могу" in pf  # explicit «never say I can't …»
    h = local_model_hint(normalize_settings({"pc_control": "full"}), "ru")
    assert "query" in h and "telegram_open_chat" in h and "app_search" in h
    hs = local_model_hint(normalize_settings({"pc_control": "standard"}), "ru")
    assert "query" in hs and "app_search" not in hs
    assert needs_tools("Play Can't Fault Das on Spotify please")
    assert needs_tools("Open Saved Messages on Telegram now")


class _Core:
    settings = {"pc_control": "full"}
    addons = None


def _actions():
    a = Actions.__new__(Actions)  # skip AppIndex prebuild thread
    a.core = _Core()
    a.apps = None
    return a


def test_media_control_routes_query_to_spotify_play():
    calls = {}
    orig = sp.play_query

    def fake(q, *, ensure_open=None, settings=None):
        calls["q"] = q
        return {"ok": True, "via": "fake"}
    sp.play_query = fake
    try:
        a = _actions()
        r = a.execute("media_control", {"action": "play", "app": "spotify", "query": "Can't Fault Das"})
        assert r["ok"] and calls["q"] == "Can't Fault Das"
        calls.clear()
        r = a.execute("media_control", {"action": "play", "query": "Numb"})  # app omitted → Spotify
        assert calls["q"] == "Numb"
        calls.clear()
        r = a.execute("app_search", {"app": "Spotify", "query": "Sonne"})  # app_search on Spotify → play
        assert calls["q"] == "Sonne"
    finally:
        sp.play_query = orig


def test_app_search_and_telegram_routing():
    import jarvis_app.actions as act
    seen = {}
    o1, o2 = act.telegram_open_chat, act.app_search
    act.telegram_open_chat = lambda chat, ensure_open=None: seen.setdefault("tg", chat) and {"ok": True}
    act.app_search = lambda app, q, **kw: seen.setdefault("app", (app, q, kw.get("search_keys"))) and {"ok": True}
    try:
        a = _actions()
        a.execute("telegram_open_chat", {"chat": "Избранное"})
        assert seen["tg"] == "Избранное"
        seen.clear()
        a.execute("app_search", {"app": "телеграм", "query": "Мама"})
        assert seen["tg"] == "Мама"
        seen.clear()
        a.execute("app_search", {"app": "Discord", "query": "general", "search_keys": ""})
        assert seen["app"][0] == "Discord" and seen["app"][1] == "general"
    finally:
        act.telegram_open_chat, act.app_search = o1, o2


def test_non_windows_safe_errors():
    if pp.IS_WIN:
        return
    assert not pp.app_search("Discord", "x")["ok"]
    assert not pp.telegram_open_chat("Избранное", ensure_open=lambda n: None)["ok"]
    assert not sp.ui_play("x", ensure_open=None)["ok"]
    assert not pp.app_search("Discord", "")["ok"]


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
