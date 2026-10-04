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


def test_v12_settings_migration():
    s = normalize_settings({"wake_word": True})          # 1.1 settings with Hey Jarvis on
    assert s["wake_mode"] == "hey_jarvis" and s["wake_word"] is True
    s = normalize_settings({})
    assert s["wake_mode"] == "off" and s["wake_word"] is False and s["live_mode"] is False
    assert s["live_interval_sec"] == 45 and s["live_min_gap_sec"] == 180 and s["live_talk"] == "some" and s["live_reply_sec"] == 8
    s = normalize_settings({"wake_mode": "name", "wake_word": False, "live_interval_sec": 3, "live_talk": "x",
                            "live_min_gap_sec": 99999, "name_threshold": 7})
    assert s["wake_mode"] == "name" and s["wake_word"] is True       # legacy mirror for older versions
    assert s["live_interval_sec"] == 20 and s["live_talk"] == "some" and s["live_min_gap_sec"] == 1800
    assert s["name_threshold"] == 0.95
    assert normalize_settings(s)["wake_mode"] == "name"               # idempotent


def test_live_policy_timing():
    from jarvis_app.live import LivePolicy, talk_params, SETTLE_SEC
    st = normalize_settings({"live_mode": True})
    p = LivePolicy(); p.reset(now=0.0)
    p.note_foreground(("code.exe", "main.py"), 0.0)
    assert p.due(5.0, st) == (False, "settle")                 # first 20 s after enabling
    assert p.due(SETTLE_SEC + 1, st)[0]
    p.on_check(21.0)
    assert p.due(40.0, st) == (False, "interval")              # 45 s interval
    assert p.due(67.0, st)[0]
    assert p.note_foreground(("chrome.exe", "TikTok"), 70.0)    # window changed
    assert p.due(80.0, st) == (False, "change")
    assert p.due(91.0, st)[0]
    p.on_remark(100.0)
    assert p.due(200.0, st) == (False, "gap")                  # 3 min between remarks
    assert p.due(281.0, st)[0]
    rare, often = talk_params({**st, "live_talk": "rare"}), talk_params({**st, "live_talk": "often"})
    assert rare["gap"] > talk_params(st)["gap"] > often["gap"] and rare["conf"] > often["conf"]
    assert talk_params(st, in_focus=True)["interval"] == 2 * talk_params(st)["interval"]


def test_live_policy_accept_and_backoff():
    from jarvis_app.live import LivePolicy
    st = normalize_settings({"live_mode": True})
    p = LivePolicy(); p.reset(now=0.0)
    d = {"valid": True, "speak": True, "kind": "hint", "text": "В строке 12 опечатка в имени переменной.", "confidence": 0.9}
    assert p.accept(d, 100.0, st) == (True, "")
    assert p.accept({**d, "confidence": 0.5}, 100.0, st) == (False, "unsure")
    assert p.accept({**d, "speak": False}, 100.0, st) == (False, "silent")
    assert p.accept({**d, "kind": "nudge"}, 100.0, st, in_focus=True) == (False, "guard")   # no double nudges
    p.remember(100.0, "пишет код", "VS Code", said=d["text"], kind="hint")
    p.on_remark(100.0, "live")
    assert p.accept(d, 150.0, st) == (False, "gap")
    assert p.accept({**d, "text": "В строке 12 опечатка в имени переменной!"}, 400.0, st) == (False, "repeat")
    assert p.accept({**d, "text": "Тесты прошли — отличная работа."}, 400.0, st)[0]
    p.on_remark(500.0, "guard")                                 # a guard nudge also restarts the gap
    assert p.accept({**d, "text": "Новая мысль"}, 520.0, st) == (False, "gap")
    b1 = p.on_rate_limit(1000.0, 45.0, retry_after=None)
    b2 = p.on_rate_limit(1000.0, 45.0, retry_after=None)
    assert b2 > b1 and p.slow == 4.0 and p.due(1000.0 + b2 - 1, st) == (False, "backoff")
    assert p.on_rate_limit(0.0, 45.0, daily=True) == 3600.0
    p.on_success(); assert p.fail == 0 and p.slow < 8.0
    lines = p.memory_lines(400.0)
    assert lines and "Ты сказал" in lines[0]


def test_live_prompt_and_parse():
    from jarvis_app.live import build_prompt, parse_decision
    st = normalize_settings({"assistant_name": "Пятница", "character_preset": "friend", "user_name": "Вова"})
    sysp, up = build_prompt(st, task="физика", focus_phase="focus", title="TikTok", process="chrome.exe",
                            fullscreen=True, since_remark=None, memory=["- 2 мин назад: код. Ты промолчал."])
    assert "Ты — Пятница" in sysp and "Тёплый" in sysp and "Вова" in sysp and "МОЛЧИШЬ" in sysp
    assert "физика" in up and "не используй nudge" in up and "весь экран" in up and "Ты промолчал" in up
    _, up2 = build_prompt(st, since_remark=300)
    assert "не запущена" in up2 and "5 мин назад" in up2
    d = parse_decision('{"activity":"смотрит TikTok","reason":"залип","speak":true,"kind":"nudge","confidence":0.92,"text":"**Эй**, хватит листать!"}')
    assert d["valid"] and d["speak"] and d["kind"] == "nudge" and d["text"] == "Эй, хватит листать!" and d["confidence"] == 0.92
    d = parse_decision('мусор {"speak": "false", "kind": "weird", "confidence": 85} хвост')
    assert d["valid"] and not d["speak"] and d["kind"] == "hint" and d["confidence"] == 0.85
    assert not parse_decision("nope")["valid"]


def test_fullscreen_and_calls():
    from jarvis_app.watcher import covers_monitor, is_call
    assert covers_monitor((0, 0, 1920, 1080), (0, 0, 1920, 1080))
    assert covers_monitor((-8, -8, 1928, 1088), (0, 0, 1920, 1080))
    assert not covers_monitor((0, 0, 1920, 1040), (0, 0, 1920, 1080))          # maximized, taskbar visible
    assert covers_monitor((1920, 0, 4480, 1440), (1920, 0, 4480, 1440))        # second monitor
    assert is_call("Zoom Meeting", "Zoom.exe") and is_call("Weekly sync | Microsoft Teams", "ms-teams.exe")
    assert not is_call("main.py - Visual Studio Code", "Code.exe")


def test_name_wake_matching():
    from jarvis_app.namewake import name_variants, match_wake, lat_to_cyr, need_score
    assert name_variants("Пятница") == ["пятница"]
    assert "джарвис" in name_variants("Jarvis") and "джарвис" in name_variants("Джарвис")
    assert lat_to_cyr("Friday") == "фрайдей" and lat_to_cyr("Kira") == "кира" and lat_to_cyr("Kaito") == "кайто"
    assert name_variants("Airi")[0] == "айри"
    assert name_variants("Мистер Робот") == ["мистер робот"]
    W = lambda *ws: [{"word": w, "conf": c, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, (w, c) in enumerate(ws)]
    v = name_variants("Пятница")
    assert match_wake(W(("пятница", 1.0)), v)
    m = match_wake(W(("эй", 1.0), ("пятница", 1.0), ("открой", 1.0), ("телеграм", 1.0)), v)
    assert m and [w["word"] for w in m["rest"]] == ["открой", "телеграм"] and abs(m["end"] - 0.9) < 1e-6
    assert match_wake(W(("сегодня", 1.0), ("пятница", 1.0)), v) is None        # name not at the start
    assert match_wake(W(("алиса", 1.0), ("поставь", 1.0)), v) is None
    assert match_wake(W(("джервис", 0.78), ("сколько", 1.0)), name_variants("Джарвис"))   # close mishearing
    assert match_wake(W(("мистер", 1.0), ("робот", 0.9)), name_variants("Мистер Робот"))
    assert match_wake(W(("джарвис", 0.5)), name_variants("Джарвис"), threshold=0.95) is None   # 0.9 < 0.93
    assert need_score(0.1) < need_score(0.5) < need_score(0.95)


def test_live_tool_declared():
    from jarvis_app.actions import TOOLS
    t = [x for x in TOOLS if x["name"] == "set_live_mode"][0]
    assert t["parameters"]["required"] == ["enabled"] and "следи и подсказывай" in t["description"]
    class C:
        def set_live(self, on): return {"ok": True, "live_mode": on}
    a = Actions.__new__(Actions); a.core = C()
    assert a.execute("set_live_mode", {"enabled": False}) == {"ok": True, "live_mode": False}
    from jarvis_app.brain import build_system_prompt, SYSTEM_PROMPT
    assert "set_live_mode" in SYSTEM_PROMPT


def test_live_intent_fallback():
    from jarvis_app.live import live_intent
    on = ["Следи и подсказывай", "Включи живой режим", "включи, пожалуйста, живой режим", "живой режим вкл", "подсказывай мне"]
    off = ["Хватит, выключи живой режим", "тихо, не мешай со своими подсказками", "отключи живой режим",
           "хватит следить и подсказывать", "больше не подсказывай", "живой режим выкл"]
    neutral = ["открой телеграм", "что такое живой режим?", "тихо", "хватит", "включи музыку", "сколько времени"]
    for t in on: assert live_intent(t) is True, t
    for t in off: assert live_intent(t) is False, t
    for t in neutral: assert live_intent(t) is None, t


# ─── v1.3: fast voice ────────────────────────────────────────────────────────

def test_sentence_splitter():
    from jarvis_app.audio import SentenceSplitter, split_sentences
    sp = SentenceSplitter()
    out = []
    for d in ["Привет", "! Сейчас ", "открою ютуб. Это", " займёт секунду, т. е. совсем", " чуть-чуть."]:
        out += sp.push(d)
    out += sp.flush()
    assert out[0] == "Привет!", out
    assert "".join(out).replace(" ", "") == "Привет!Сейчасоткроюютуб.Этозаймётсекунду,т.е.совсемчуть-чуть.".replace(" ", ""), out
    assert not any(x.endswith("т.") for x in out), out  # no cut after an abbreviation
    # a long first sentence is cut at a clause so the first sound comes early
    sp = SentenceSplitter()
    first = sp.push("Солнечный свет рассеивается в атмосфере, и синяя часть спектра рассеивается сильнее")
    assert first and first[0].endswith(","), first
    # a newline (end of text before a tool call) cuts too
    sp = SentenceSplitter()
    assert sp.push("Открываю ютуб") == [] and sp.push("\n") == ["Открываю ютуб"]
    # hard cut at max_len
    sp = SentenceSplitter(max_len=50)
    got = sp.push("слово " * 30)
    assert got and all(len(x) <= 50 for x in got), got
    assert split_sentences("Раз. Два три четыре пять шесть. Семь!") and split_sentences("") == []


def test_needs_tools_router():
    from jarvis_app.brain import needs_tools
    yes = ["Айри, открой телеграм", "поставь таймер на пять минут", "Найди на компьютере файл с рефератом",
           "я делаю домашку по физике", "включи живой режим", "сколько сейчас часов?", "да", "давай"]
    no = ["Почему небо голубое?", "Какая столица Австралии?", "Расскажи короткий анекдот про программистов",
          "как дела у тебя сегодня", "сколько будет семь умножить на восемь"]
    for t in yes: assert needs_tools(t), t
    for t in no: assert not needs_tools(t), t


def test_v13_settings():
    s = normalize_settings({})
    assert s["tts_engine"] == "piper" and s["stt_mode"] == "local" and s["fast_replies"] is True
    assert s["piper_voice"] == "ru_RU-dmitri-medium" and s["vad_silence_ms"] == 600
    s = normalize_settings({"tts_engine": "x", "stt_mode": "y", "piper_voice": "../evil", "vad_silence_ms": 5})
    assert s["tts_engine"] == "piper" and s["stt_mode"] == "local" and s["piper_voice"] == "ru_RU-dmitri-medium"
    assert s["vad_silence_ms"] == 400
    s = normalize_settings({"tts_engine": "edge", "stt_mode": "cloud", "piper_voice": "ru_RU-irina-medium",
                            "fast_replies": False, "vad_silence_ms": 900})
    assert (s["tts_engine"], s["stt_mode"], s["piper_voice"], s["fast_replies"], s["vad_silence_ms"]) == \
        ("edge", "cloud", "ru_RU-irina-medium", False, 900)


def test_piper_catalog_and_rate():
    from jarvis_app import tts_local as T
    assert T.DEFAULT_VOICE in T.PIPER_VOICES and len(T.PIPER_VOICES) >= 4
    for k, v in T.PIPER_VOICES.items():
        assert k[:2] == v["lang"] and len(v["md5"]) == 32 and len(v["md5_json"]) == 32, k
        assert T.KEY_RE.fullmatch(k) and v["path"].startswith(v["lang"] + "/"), k
        on, js = T.voice_files(k, root="/nonexistent")
        assert on.endswith(T.file_key(k) + ".onnx") and js.endswith(".onnx.json")
        assert not T.installed(k, root="/nonexistent")
    for edge, piper in T.EDGE_TO_PIPER.items():
        assert piper in T.PIPER_VOICES, (edge, piper)


def test_stt_acceptable():
    from jarvis_app.stt_local import acceptable, words_conf, words_text
    assert acceptable("открой телеграм", 0.9) and not acceptable("открой телеграм", 0.3)
    assert not acceptable("", 1.0) and not acceptable("а", 1.0)
    w = [{"word": "открой", "conf": 1.0}, {"word": "ютуб", "conf": 0.5}]
    assert abs(words_conf(w) - 0.75) < 1e-6 and words_text(w) == "Открой ютуб"


class _FakeResp:
    def __init__(self, chunks): self.chunks = chunks
    def iter_content(self, chunk_size=None): return iter(self.chunks)


def test_sse_stream_accumulator():
    import json as _j
    from jarvis_app.providers import iter_sse, StreamAccumulator
    ev1 = {"candidates": [{"content": {"parts": [{"text": "Открываю\u2028 ютуб"}]}}]}
    ev2 = {"candidates": [{"content": {"parts": [{"text": ".", "thoughtSignature": "sig"}]}}]}
    ev3 = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "open_url", "args": {"url": "https://youtube.com"}}}]}}],
           "modelVersion": "gemini-x"}
    body = b"".join(b"data: " + _j.dumps(e, ensure_ascii=False).encode("utf-8") + b"\r\n\r\n" for e in (ev1, ev2, ev3))
    chunks = [body[i:i + 7] for i in range(0, len(body), 7)]  # split mid-UTF-8 and mid-line
    events = list(iter_sse(_FakeResp(chunks)))
    assert len(events) == 3, events
    acc = StreamAccumulator("m")
    deltas = []
    for e in events:
        deltas += acc.add(e)
    assert deltas == ["Открываю\u2028 ютуб", ".", "\n"], deltas
    r = acc.result()
    assert r.text == "Открываю\u2028 ютуб." and r.model == "gemini-x"
    assert [c.name for c in r.tool_calls] == ["open_url"] and r.tool_calls[0].args["url"].endswith("youtube.com")
    parts = r.raw_message["parts"]
    assert parts[0]["text"] == "Открываю\u2028 ютуб." and parts[0]["thoughtSignature"] == "sig"
    assert "functionCall" in parts[1]
    try:
        StreamAccumulator("m").result()
        raise AssertionError("empty stream must raise")
    except Exception as e:
        assert "Пустой" in str(e)


def test_tool_ack_dedup():
    from jarvis_app.core import _same_ack, TOOL_ACK
    assert _same_ack("Открываю ютуб.", TOOL_ACK["open_url"])
    assert not _same_ack("Сделано, музыка для концентрации уже играет на ютубе.", TOOL_ACK["open_url"])
    assert not _same_ack("Готово.", TOOL_ACK["open_url"])


def test_brain_plan_and_cooldown():
    import time as _t
    from jarvis_app.brain import Brain
    from jarvis_app.providers import RateLimitError
    b = Brain.__new__(Brain)
    b.cooldown_until = b.lite_cooldown_until = 0.0
    assert b._plan("flash", "lite", fast=True) == ["lite", "flash"]
    assert b._plan("flash", "lite", fast=False) == ["flash", "lite"]
    b._cool("flash", "flash", RateLimitError("q", retry_after=None, daily=True))
    assert b.cooldown_until - _t.monotonic() > 3000  # a daily quota is skipped for hours, not minutes
    assert b._plan("flash", "lite", fast=False) == ["lite", "flash"]
    b._cool("lite", "flash", RateLimitError("q", retry_after=20, daily=False))
    assert 10 < b.lite_cooldown_until - _t.monotonic() <= 20
    assert b._plan("flash", "flash", fast=True) == ["flash"]


def test_speaker_pipeline_and_interrupt():
    import time as _t
    import numpy as np
    from jarvis_app.audio import Speaker, VoiceTurn

    class FakePiper:
        def __init__(self): self.said = []
        def ready(self, key): return True
        def synth(self, text, key, rate):
            self.said.append(text)
            return np.zeros(2205, dtype=np.int16), 22050  # 0.1 s
    fp = FakePiper()
    turns = []
    sp = Speaker(lambda: {"tts_engine": "piper", "piper_voice": "ru_RU-dmitri-medium", "tts_rate": 0, "tts_volume": 0},
                 on_turn=turns.append, piper=fp)
    sp.null_output = True
    t = VoiceTurn("voice")
    t.mark("speech_end")
    st = sp.open_stream(t)
    st.feed("Раз.")
    st.feed("Два.")
    assert not sp.idle
    st.end()
    for _ in range(100):
        if sp.idle: break
        _t.sleep(0.02)
    assert sp.idle and fp.said == ["Раз.", "Два."] and turns == [t]
    # interrupt: a stale stream says nothing more
    st = sp.open_stream(None)
    for i in range(20):
        st.feed(f"Фраза номер {i}.")
    _t.sleep(0.05)
    sp.stop()
    st.feed("После стопа.")
    st.end()
    for _ in range(100):
        if sp.idle: break
        _t.sleep(0.02)
    assert sp.idle and "После стопа." not in fp.said and len(fp.said) < 22, fp.said
    sp.shutdown()


def test_vad_endpoint():
    import numpy as np
    from jarvis_app.vad import EndpointDetector
    rng = np.random.default_rng(0)
    quiet = (rng.standard_normal(1280) * 20).astype(np.int16)
    loud = (rng.standard_normal(1280) * 3000).astype(np.int16)
    lvl = lambda c: float(np.sqrt(np.mean(c.astype(np.float32) ** 2)))
    en = EndpointDetector(use_silero=False)
    assert en.kind == "energy"
    got = [en.voiced(quiet, lvl(quiet), k * 0.08) for k in range(6)] + [en.voiced(loud, lvl(loud), 0.5 + k * 0.08) for k in range(3)]
    assert not any(got[:6]) and all(got[6:]), got
    si = EndpointDetector(use_silero=True)
    if si.kind == "silero":  # model ships in models/; white noise / hum is not speech
        assert not any(si.voiced(c, lvl(c), k * 0.08) for k, c in enumerate([quiet] * 5 + [loud] * 5))


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
