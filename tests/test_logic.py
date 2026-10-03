"""Offline unit tests for pure logic. Run: python -m pytest -q tests/test_logic.py (or python tests/test_logic.py)"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date
from jarvis_app.config import normalize_settings, DEFAULT_SETTINGS, save_settings, load_settings
from jarvis_app.focus import FocusSession, Stats, fmt_clock, minutes_phrase, format_duration
from jarvis_app.watcher import NudgePolicy, match_distraction, nudge_text
from jarvis_app.actions import score_name, translit, find_files, Actions, time_info
from jarvis_app.providers import clean_transcript, parse_retry_after
from jarvis_app.brain import parse_screen_verdict, context_line, _is_plain_user
from jarvis_app.updater import is_newer_version


def test_settings_normalize():
    s = normalize_settings({"provider": "nope", "screen_check_sec": 5, "distractions": "a\n\nB", "tts_rate": 999,
                            "models": {"gemini": {"chat": ""}}, "api_keys": {"gemini": " k "}})
    assert s["provider"] == "gemini"
    assert s["screen_check_sec"] == 20
    assert s["distractions"] == ["a", "b"]
    assert s["tts_rate"] == 50
    assert s["models"]["gemini"]["chat"] == DEFAULT_SETTINGS["models"]["gemini"]["chat"]
    assert s["api_keys"]["gemini"] == "k"
    assert normalize_settings({"screen_check_sec": 0})["screen_check_sec"] == 0


def test_settings_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.json")
        s = normalize_settings({"voice": "ru-RU-SvetlanaNeural", "custom": 1})
        save_settings(s, p)
        s2 = load_settings(p)
        assert s2["voice"] == "ru-RU-SvetlanaNeural" and s2["custom"] == 1


def test_focus_single_block():
    f = FocusSession()
    f.start("физика", 40, 1, 5, now=0)
    assert f.phase == "focus" and abs(f.remaining(now=60) - 2340) < 1e-6
    assert f.tick(now=100) is None
    ev = f.tick(now=2400)
    assert ev.old == "focus" and ev.new == "done" and ev.focus_seconds == 2400
    assert not f.active


def test_focus_rounds_pause_skip_stop():
    f = FocusSession()
    f.start("код", 25, 2, 5, now=0)
    f.pause(now=600)
    assert abs(f.remaining(now=5000) - 900) < 1e-6
    f.resume(now=5000)
    ev = f.tick(now=5900)
    assert ev.new == "break" and ev.focus_seconds == 1500
    ev = f.skip(now=5950)
    assert ev.new == "focus" and f.round_num == 2 and ev.focus_seconds == 0
    assert f.stop(now=5950 + 300) == 300
    assert f.phase == "idle"


def test_stats(tmp=None):
    with tempfile.TemporaryDirectory() as d:
        st = Stats(os.path.join(d, "a.json"), os.path.join(d, "b.json"))
        st.add_focus(600, date(2026, 10, 1)); st.add_focus(1200, date(2026, 10, 2)); st.add_focus(60, date(2026, 10, 3))
        st.add_distraction(date(2026, 10, 3))
        assert st.today(date(2026, 10, 3)) == 60
        assert st.streak(date(2026, 10, 3)) == 3
        assert st.week(date(2026, 10, 3)) == 1860
        assert st.distractions_today(date(2026, 10, 3)) == 1
        st2 = Stats(os.path.join(d, "a.json"), os.path.join(d, "b.json"))
        assert st2.total() == 1860


def test_nudge_policy():
    p = NudgePolicy(cooldown=60, grace=8, reset_after=90)
    assert p.off_task(0) is None
    assert p.off_task(5) is None
    assert p.off_task(9) == 1
    assert p.off_task(30) is None
    assert p.off_task(70) == 2
    assert p.off_task(131) == 3
    assert p.off_task(200) is None      # level 3 repeats at 2x cooldown
    assert p.off_task(252) == 3
    p.on_task(260); p.on_task(355)
    assert p.level == 0
    assert p.off_task(400, immediate=True) == 1


def test_match_distraction():
    pats = ["tiktok", "shorts", "proc:steam.exe"]
    assert match_distraction("TikTok - Make Your Day — Mozilla Firefox", "firefox.exe", pats) == "tiktok"
    assert match_distraction("funny #shorts - YouTube", "chrome.exe", pats) == "shorts"
    assert match_distraction("Steam", "steam.exe", pats) == "proc:steam.exe"
    assert match_distraction("Physik.docx - Word", "WINWORD.EXE", pats) is None
    assert match_distraction("TikTok", "x.exe", pats, own_pid=5, pid=5) is None
    assert "физика" in nudge_text(1, "физика")


def test_app_matching():
    assert translit("телеграм") == "telegram"
    assert score_name("telegram", "Telegram Desktop") >= 0.88
    assert score_name("Visual Studio Code", "Visual Studio Code") == 1.0
    assert score_name("дискорд", "Discord") > 0.72
    assert score_name("chrome", "Google Chrome") >= 0.85
    assert score_name("word", "WordPad") < 1.0


def test_find_files():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "Физика", "node_modules"))
        open(os.path.join(d, "Физика", "реферат по физике.docx"), "w").close()
        open(os.path.join(d, "Физика", "node_modules", "физика.js"), "w").close()
        r = find_files("физик", [d])
        assert any(p.endswith("реферат по физике.docx") for p in r)
        assert not any("node_modules" in p for p in r)


def test_actions_safety():
    class C: pass
    a = Actions.__new__(Actions)
    a.core = C()
    assert not a.do_open_url("javascript:alert(1)")["ok"]
    with tempfile.TemporaryDirectory() as d:
        bat = os.path.join(d, "x.bat"); open(bat, "w").close()
        assert not a.do_open_path(bat)["ok"]
    assert not a.do_open_path("/no/such/file")["ok"]
    assert set(time_info()) == {"time", "date", "weekday"}


def test_transcript_and_verdict():
    assert clean_transcript("<пусто>") == ""
    assert clean_transcript("00:00") == ""
    assert clean_transcript("«Привет»") == "Привет"
    v = parse_screen_verdict('{"on_task": false, "confidence": 0.93, "activity": "TikTok"}')
    assert v == {"on_task": False, "confidence": 0.93, "activity": "TikTok", "valid": True}
    v = parse_screen_verdict("garbage")
    assert v["on_task"] is True and v["valid"] is False
    assert parse_retry_after({}, {"error": {"details": [{"retryDelay": "17s"}]}}) == 17.0
    assert parse_retry_after({"Retry-After": "5"}, None) == 5.0


def test_misc():
    assert fmt_clock(3725) == "1:02:05" and fmt_clock(65) == "01:05"
    assert minutes_phrase(1) == "1 минута" and minutes_phrase(3) == "3 минуты" and minutes_phrase(11) == "11 минут"
    assert format_duration(5400) == "1 ч 30 мин"
    assert is_newer_version("v0.2.0", "0.1.0") and not is_newer_version("v0.1.0", "0.1.0")
    assert is_newer_version("v0.3.0", "0.2.0")


def test_webui_helpers():
    import tempfile
    from jarvis_app.webui import ChatLog, _deep_merge, hotkey_pretty
    assert hotkey_pretty("ctrl+alt+j") == "Ctrl+Alt+J"
    m = _deep_merge({"a": {"b": 1, "c": 2}, "x": 1}, {"a": {"b": 5}})
    assert m == {"a": {"b": 5, "c": 2}, "x": 1}
    p = os.path.join(tempfile.mkdtemp(), "chat.json")
    log = ChatLog(p, limit=3)
    for i in range(5):
        log.add({"role": "user", "text": str(i)})
    log.flush()
    assert [x["text"] for x in ChatLog(p).items] == ["2", "3", "4"]
    from jarvis_app.config import normalize_settings
    s = normalize_settings({"custom_commands": [{"name": "утро", "prompt": "открой почту"}, {"name": "", "prompt": "x"}],
                            "confirm_actions": 1})
    assert s["custom_commands"] == [{"name": "утро", "prompt": "открой почту"}] and s["confirm_actions"] is True
    assert "Фокус-сессия не запущена" in context_line({"phase": "idle"})
    assert _is_plain_user({"role": "user", "parts": [{"text": "hi"}]})
    assert not _is_plain_user({"role": "user", "parts": [{"functionResponse": {}}]})


def test_versions_parse(monkeypatch=None):
    from jarvis_app import updater as u
    assert u.compare("v1.1.0", "1.0.0") == 1 and u.compare("v1.0.0", "1.0.0") == 0 and u.compare("v0.9.9", "1.0.0") == -1
    raw = [
        {"tag_name": "v1.1.0", "published_at": "2026-10-03T20:00:00Z", "body": "## Что нового\n- **Имя** ассистента\n- Характер",
         "assets": [{"name": "Jarvis.exe", "size": 10, "browser_download_url": "https://x/1.1.0/Jarvis.exe"}]},
        {"tag_name": "v1.0.0", "published_at": "2026-10-03T19:00:00Z", "body": "- Версии",
         "assets": [{"name": "Jarvis.exe", "size": 9, "browser_download_url": "https://x/1.0.0/Jarvis.exe"}]},
        {"tag_name": "v0.5.0", "assets": []},                       # no exe → skipped
        {"tag_name": "v2.0.0", "draft": True, "assets": [{"name": "Jarvis.exe"}]},  # draft → skipped
    ]
    u._cache.update(t=__import__("time").time(), data=raw)
    rels = u.list_releases(current="1.0.0")
    assert [r["tag"] for r in rels] == ["v1.1.0", "v1.0.0"]
    assert rels[0]["status"] == "newer" and rels[1]["status"] == "current"
    assert rels[0]["notes"].splitlines()[0] == "Что нового" and "• Имя ассистента" in rels[0]["notes"]
    assert u.list_releases(current="1.1.0")[1]["status"] == "older"
    with tempfile.TemporaryDirectory() as d:
        bat = u.write_swap_bat(os.path.join(d, "Jarvis.exe"), r"C:\T\Jarvis.exe", [11, 22])
        txt = open(bat, encoding="utf-8").read()
        assert 'find " 11 "' in txt and "taskkill /F /PID 22" in txt and 'start "" "C:\\T\\Jarvis.exe"' in txt
        assert "taskkill /F /IM" not in txt
        assert "set PYINSTALLER_RESET_ENVIRONMENT=1" in txt and "set _PYI_APPLICATION_HOME_DIR=" in txt
        assert txt.index("copied OK") < txt.index('start ""')
    os.environ["_PYI_APPLICATION_HOME_DIR"] = "x"; os.environ["JARVIS_AUTOTEST"] = "y"
    env = u.clean_env()
    assert "_PYI_APPLICATION_HOME_DIR" not in env and "JARVIS_AUTOTEST" not in env and env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    del os.environ["_PYI_APPLICATION_HOME_DIR"], os.environ["JARVIS_AUTOTEST"]
    u._cache.update(t=0.0, data=None)


def test_old_version_tolerates_new_keys():
    s = normalize_settings({"assistant_name": "Пятница", "character_preset": "friend", "future_key": [1, 2]})
    assert s["future_key"] == [1, 2] and s["assistant_name"] == "Пятница"


def test_persona_and_prompt():
    from jarvis_app import persona
    from jarvis_app.brain import build_system_prompt
    s = normalize_settings({})
    assert s["assistant_name"] == "Джарвис" and s["character_preset"] == "butler" and s["user_name"] == ""
    p = build_system_prompt(s)
    assert "Ты — Джарвис" in p and persona.PRESETS["butler"]["prompt"] in p and "Вов" not in p
    s2 = normalize_settings({"assistant_name": "Пятница", "character_preset": "coach", "user_name": "Вова"})
    p2 = build_system_prompt(s2)
    assert "Ты — Пятница" in p2 and "Строгий" in p2 and "Вова" in p2
    s3 = normalize_settings({"character_preset": "custom", "character": "Пират {арр}"})
    assert "Пират {арр}" in build_system_prompt(s3)
    for k in persona.PRESETS:
        t = persona.nudge_text({"character_preset": k, "user_name": "Вова"}, 2, "физика", "10 минут")
        assert "{" not in t and t
    t = persona.nudge_text({"character_preset": "custom", "user_name": ""}, 2, "физика", "10 минут")
    assert t in [x.format(task="физика", left="10 минут", user="").replace(", ", ", ") for x in persona.NEUTRAL_NUDGES[2]] or "{" not in t
    assert persona.assistant_name({"assistant_name": "  "}) == "Джарвис"


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
