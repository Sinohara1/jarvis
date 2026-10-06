"""Offline tests for v1.5 local AI (Ollama): routing, game guard, fallback, stream parsing.
Run: python tests/test_local.py (or pytest)"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis_app import providers as P
from jarvis_app import local_ai as LA
from jarvis_app.config import normalize_settings
from jarvis_app.brain import Brain
from jarvis_app.actions import TOOLS


def test_classify_app():
    isaac = r"C:\Program Files (x86)\Steam\steamapps\common\The Binding of Isaac Rebirth\isaac-ng.exe"
    assert LA.classify_app("isaac-ng.exe", isaac, False) == "game"
    assert LA.classify_app("isaac-ng.exe", isaac.replace("\\", "/"), False) == "game"
    assert LA.classify_app("chrome.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe", True) is None
    assert LA.classify_app("wallpaper32.exe", r"D:\Steam\steamapps\common\wallpaper_engine\wallpaper32.exe", True) is None
    assert LA.classify_app("steam.exe", r"C:\Program Files (x86)\Steam\steam.exe", True) is None
    assert LA.classify_app("Adobe Premiere Pro.exe", r"C:\Program Files\Adobe\Adobe Premiere Pro 2026\Adobe Premiere Pro.exe") == "gpu"
    assert LA.classify_app("unknowngame.exe", r"C:\Stuff\unknowngame.exe", True) == "game"   # fullscreen exclusive
    assert LA.classify_app("notepad.exe", r"C:\Windows\notepad.exe", False) is None
    assert LA.classify_app("", "", True) is None


def test_game_guard_linger():
    g = LA.GameGuard(linger=120)
    isaac = r"D:\SteamLibrary\steamapps\common\Isaac\isaac-ng.exe"
    assert g.update("isaac-ng.exe", isaac, False, 42, 0.0) and g.active and g.kind == "game"
    alive = {42: True}
    # alt-tab to Telegram while the game still runs → still yielding (within linger)
    assert g.update("Telegram.exe", "", False, 7, 30.0, alive=lambda p: alive.get(p, False))
    # game closed → free right away
    alive[42] = False
    assert not g.update("Telegram.exe", "", False, 7, 31.0, alive=lambda p: alive.get(p, False)) and not g.active
    # game alive but out of focus for longer than linger → free
    g.update("isaac-ng.exe", isaac, False, 42, 100.0); alive[42] = True
    assert not g.update("code.exe", "", False, 9, 100.0 + 121, alive=lambda p: alive.get(p, False))


def test_parse_text_tool_call():
    known = {t["name"] for t in TOOLS}
    c = LA._parse_text_tool_call('{"name": "open_app", "arguments": {"name": "Telegram"}}', known)
    assert len(c) == 1 and c[0].name == "open_app" and c[0].args == {"name": "Telegram"}
    c = LA._parse_text_tool_call('```json\n[{"function": {"name": "open_url", "arguments": "{\\"url\\": \\"youtube.com\\"}"}}]\n```', known)
    assert c and c[0].name == "open_url" and c[0].args["url"] == "youtube.com"
    c = LA._parse_text_tool_call('<tool_call>{"name": "set_live_mode", "parameters": {"enabled": true}}</tool_call>', known)
    assert c and c[0].args == {"enabled": True}
    assert LA._parse_text_tool_call('{"name": "rm_rf", "arguments": {}}', known) == []
    assert LA._parse_text_tool_call("Привет! Чем помочь?", known) == []


class FakeMgr:
    def __init__(self, running=True, models=("gemma4:12b",), loaded=None):
        self._running, self._models, self._loaded = running, list(models), loaded
        self.loads, self.unloads, self.version = [], [], "0.35.1"
        self.pulling = None
    def set_url(self, u): pass
    def running(self): return self._running
    def installed(self): return True
    def ensure_running(self, wait=15.0): return self._running
    def models(self): return [{"name": m} for m in self._models]
    def has_model(self, m): return m in self._models
    def loaded(self): return [{"name": self._loaded, "gb": 9, "vram_gb": 9, "gpu": 1.0}] if self._loaded else []
    def is_loaded(self, m): return (self.loaded() or [None])[0] if self._loaded == m else None
    def load(self, m, keep=10, timeout=120): self.loads.append(m); self._loaded = m; return 0.1
    def unload(self, m): self.unloads.append(m); self._loaded = None; return True
    def unload_all(self):
        n = [self._loaded] if self._loaded else []; self._loaded = None; self.unloads += n; return n


def _ctl(settings, mgr, fg=None):
    c = LA.LocalController(lambda: settings, foreground=fg, manager=mgr)
    c.refresh()
    return c


def test_route_modes():
    s = normalize_settings({})
    assert s["ai_route"] == "local_first" and s["gpu_free_in_games"] is True and s["local_keep_alive_min"] == 10
    mgr = FakeMgr(loaded="gemma4:12b")
    c = _ctl(s, mgr)
    assert c.route("chat", "gemini", True) == ["ollama", "gemini"]
    assert c.route("chat", "gemini", False) == ["ollama"]
    s["ai_route"] = "cloud"; assert c.route("chat", "gemini", True) == ["gemini"]
    s["ai_route"] = "local"; assert c.route("chat", "gemini", True) == ["ollama"]


def test_route_cold_warms_and_uses_cloud():
    import time
    s = normalize_settings({})
    mgr = FakeMgr(loaded=None)
    c = _ctl(s, mgr)
    assert c.route("chat", "gemini", True) == ["gemini"]          # cold: don't make him wait for the load
    for _ in range(100):
        if mgr.loads: break
        time.sleep(0.02)
    assert mgr.loads == ["gemma4:12b"]                            # …but warm it for the next turn
    mgr._loaded = None; c.refresh()
    assert c.route("live", "gemini", True) == ["ollama", "gemini"]  # background checks can wait
    assert c.route("chat", "gemini", False)[0] == "ollama"         # no cloud key → local even if cold


def test_route_not_available():
    s = normalize_settings({})
    c = _ctl(s, FakeMgr(running=False))
    assert c.route("chat", "gemini", True) == ["gemini"]
    c = _ctl(s, FakeMgr(models=("qwen3.5:9b",)))                 # chosen model not downloaded
    assert c.route("chat", "gemini", True) == ["gemini"]


def test_game_unloads_and_reloads():
    import time
    s = normalize_settings({})
    mgr = FakeMgr(loaded="gemma4:12b")
    fg = {"v": ("isaac-ng.exe", r"C:\Steam\steamapps\common\Isaac\isaac-ng.exe", True, 999999)}
    c = _ctl(s, mgr, fg=lambda: fg["v"])
    c.tick(now=1000.0)
    for _ in range(100):
        if mgr.unloads: break
        time.sleep(0.02)
    assert mgr.unloads == ["gemma4:12b"] and c.blocked() == "isaac-ng.exe" and c.keep_alive() == "20s"
    assert c.route("chat", "gemini", True) == ["gemini"]
    c.warm_async(); time.sleep(0.05); assert mgr.loads == []        # no warm-up while gaming
    # game exits (pid gone) → reload
    fg["v"] = ("explorer.exe", r"C:\Windows\explorer.exe", False, 1)
    c.tick(now=1003.0)
    for _ in range(100):
        if mgr.loads: break
        time.sleep(0.02)
    assert mgr.loads == ["gemma4:12b"] and not c.blocked() and c.keep_alive() == "10m"
    # setting off → the game does not block
    s["gpu_free_in_games"] = False
    fg["v"] = ("isaac-ng.exe", r"C:\Steam\steamapps\common\Isaac\isaac-ng.exe", True, 999999)
    c.tick(now=2000.0)
    assert c.blocked() == "" and c.keep_alive() == "10m"


def test_settings_normalization():
    s = normalize_settings({"provider": "ollama", "ai_route": "weird", "local_keep_alive_min": 9999,
                            "gpu_free_in_games": 0})
    assert s["provider"] != "ollama" and s["ai_route"] == "local_first"
    assert s["local_keep_alive_min"] == 240 and s["gpu_free_in_games"] is False
    s = normalize_settings({"models": {"ollama": {"chat": "qwen3.5:9b"}}, "ai_route": "local"})
    assert s["models"]["ollama"]["chat"] == "qwen3.5:9b" and s["ai_route"] == "local"


class FakeProv(P.Provider):
    def __init__(self, name, behaviour):
        self.name, self.behaviour, self.seen = name, behaviour, []
    def user_message(self, text, image_jpeg=None): return {"role": "user", "content": text, "p": self.name}
    def assistant_message(self, text): return {"role": "assistant", "content": text, "p": self.name}
    def tool_result_messages(self, calls, results): return [{"role": "tool", "content": json.dumps(r)} for r in results]
    def chat_stream(self, model, system, messages, tools=None, *, on_text, timeout=40.0, temperature=0.7, max_tokens=2048):
        self.seen.append([dict(m) for m in messages])
        return self.behaviour(on_text)
    def chat(self, model, system, messages, tools=None, *, timeout=40.0, temperature=0.7):
        return self.chat_stream(model, system, messages, tools, on_text=lambda d: None)


class Route:
    def __init__(self, names): self.names = names
    def route(self, kind, cloud, ok): return list(self.names)
    def keep_alive(self): return "10m"
    def note_result(self, ok, err=""): pass


def _brain(local_beh, cloud_beh):
    s = normalize_settings({"user_name": "Вова"})
    b = Brain(lambda: s, None, lambda: "")
    provs = {"ollama": FakeProv("ollama", local_beh), "gemini": FakeProv("gemini", cloud_beh)}
    b.provider = lambda name=None: provs[name]
    b.models = lambda name=None: ("m", "m")
    b.local = Route(["ollama", "gemini"])
    return b, provs


def test_brain_falls_back_before_text():
    def boom(on_text): raise P.OverloadedError("Локальная модель не ответила вовремя")
    def ok(on_text): on_text("Канберра."); return P.LLMResult(text="Канберра.", tool_calls=[], raw_message={"role": "model", "content": "Канберра."})
    b, provs = _brain(boom, ok)
    out = []
    assert b.ask("Столица Австралии?", on_text=out.append) == "Канберра."
    assert out == ["Канберра."] and b.last_provider == "gemini" and b.last_fallback.startswith("ollama→gemini")
    assert b.transcript == [("user", "Столица Австралии?"), ("assistant", "Канберра.")]
    # next turn local works again: history rebuilt in local format from the transcript
    def ok2(on_text): on_text("Да."); return P.LLMResult(text="Да.", tool_calls=[], raw_message={"role": "assistant", "content": "Да."})
    provs["ollama"].behaviour = ok2
    assert b.ask("Уверен?", on_text=lambda d: None) == "Да." and b.last_provider == "ollama"
    msgs = provs["ollama"].seen[-1]
    assert [m["content"] for m in msgs] == ["Столица Австралии?", "Канберра.", "Уверен?"]
    assert all(m.get("p") == "ollama" for m in msgs[:2])


def test_brain_no_fallback_after_text():
    def half(on_text):
        on_text("Сейчас расскажу")
        raise P.ProviderError("Обрыв связи с локальной моделью")
    def ok(on_text): return P.LLMResult(text="x", tool_calls=[], raw_message={})
    b, provs = _brain(half, ok)
    try:
        b.ask("Расскажи анекдот", on_text=lambda d: None)
        raise AssertionError("must raise")
    except P.ProviderError:
        pass
    assert provs["gemini"].seen == [] and b.transcript == []


def test_brain_empty_local_reply_falls_back():
    def empty(on_text): return P.LLMResult(text="", tool_calls=[], raw_message={"role": "assistant", "content": ""})
    def ok(on_text): on_text("Привет!"); return P.LLMResult(text="Привет!", tool_calls=[], raw_message={})
    b, provs = _brain(empty, ok)
    assert b.ask("Привет", on_text=lambda d: None) == "Привет!" and b.last_provider == "gemini"


class FakeResp:
    def __init__(self, lines, status=200): self.lines, self.status_code = lines, status
    def iter_lines(self):
        for l in self.lines: yield json.dumps(l).encode()
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def close(self): pass


def _prov_with(lines):
    p = LA.OllamaProvider()
    sent = {}
    def post(url, json=None, timeout=None, stream=False):
        sent.update(json); return FakeResp(lines)
    p.session.post = post
    return p, sent


def test_ollama_stream_text_and_payload():
    p, sent = _prov_with([{"message": {"content": "При"}}, {"message": {"content": "вет!"}}, {"done": True, "message": {}}])
    out = []
    r = p.chat_stream("gemma4:12b", "sys", [p.user_message("hi", b"\xff\xd8jpeg")], TOOLS, on_text=out.append)
    assert "".join(out) == "Привет!" and r.text == "Привет!" and not r.tool_calls
    assert sent["think"] is False and sent["keep_alive"] == "10m" and sent["options"]["num_ctx"] == LA.NUM_CTX
    assert sent["messages"][0] == {"role": "system", "content": "sys"} and sent["messages"][1]["images"]
    assert sent["tools"][0]["type"] == "function"


def test_ollama_stream_native_tool_call():
    p, _ = _prov_with([{"message": {"content": "", "tool_calls": [{"function": {"name": "open_app", "arguments": {"name": "Telegram"}}}]}},
                       {"done": True, "message": {}}])
    out = []
    r = p.chat_stream("m", "", [], TOOLS, on_text=out.append)
    assert out == [] and r.tool_calls[0].name == "open_app" and r.raw_message["tool_calls"]
    tr = p.tool_result_messages(r.tool_calls, [{"ok": True}])
    assert tr[0]["role"] == "tool" and tr[0]["tool_name"] == "open_app"


def test_ollama_stream_text_tool_call_rescued():
    p, _ = _prov_with([{"message": {"content": '{"name": "set_live_mode", '}}, {"message": {"content": '"arguments": {"enabled": false}}'}},
                       {"done": True, "message": {}}])
    out = []
    r = p.chat_stream("m", "", [], TOOLS, on_text=out.append)
    assert out == [] and r.tool_calls and r.tool_calls[0].args == {"enabled": False} and r.text == ""
    # JSON-looking text that is not a tool call is still spoken (once, at the end)
    p, _ = _prov_with([{"message": {"content": "[1, 2, 3]"}}, {"done": True, "message": {}}])
    out = []
    r = p.chat_stream("m", "", [], TOOLS, on_text=out.append)
    assert out == ["[1, 2, 3]"]


def test_ollama_errors_map_to_fallback():
    import requests
    p = LA.OllamaProvider(base_url="http://127.0.0.1:9/v1")
    assert p.base_url == "http://127.0.0.1:9"
    def refuse(*a, **k): raise requests.ConnectionError("refused")
    p.session.post = refuse
    try:
        p.chat("m", "", [])
        raise AssertionError
    except LA.LocalUnavailable as e:
        assert isinstance(e, P.OverloadedError)


def test_model_matches():
    assert LA._model_matches("gemma4:12b", "gemma4:12b") and LA._model_matches("qwen3.5", "qwen3.5:latest")
    assert not LA._model_matches("gemma4:12b", "gemma4:26b")


def test_control_tokens_stripped_and_repeat_penalty():
    p, sent = _prov_with([{"message": {"content": "Поставил напоминание. "}}, {"message": {"content": "<channel|>"}}, {"done": True, "message": {}}])
    out = []
    r = p.chat_stream("m", "", [], TOOLS, on_text=out.append)
    assert r.text == "Поставил напоминание." and "".join(out).strip() == "Поставил напоминание."
    assert sent["options"]["repeat_penalty"] == LA.REPEAT_PENALTY
    assert LA._clean('a <|"|>b<turn|> c <b>x</b>') == "a b c <b>x</b>"


def test_empty_reply_retried_once():
    p = LA.OllamaProvider()
    calls = []
    replies = [[{"done": True, "done_reason": "length", "message": {"content": ""}}],
               [{"message": {"content": "", "tool_calls": [{"function": {"name": "start_focus", "arguments": {"task": "физика", "minutes": 40}}}]}}, {"done": True, "message": {}}]]
    def post(url, json=None, timeout=None, stream=False):
        calls.append(json); return FakeResp(replies[len(calls) - 1])
    p.session.post = post
    r = p.chat_stream("m", "", [], TOOLS, on_text=lambda d: None)
    assert r.tool_calls and r.tool_calls[0].args["minutes"] == 40 and len(calls) == 2
    assert calls[1]["options"]["repeat_penalty"] > calls[0]["options"]["repeat_penalty"]
    # still empty on the retry → empty result (Brain then falls back to the cloud)
    calls.clear(); replies[1] = replies[0]
    r = p.chat_stream("m", "", [], TOOLS, on_text=lambda d: None)
    assert not r.text and not r.tool_calls and len(calls) == 2


def test_local_skips_redundant_second_round():
    from jarvis_app import brain as B
    rounds = []
    def beh(on_text):
        rounds.append(1)
        on_text("Открываю Telegram.")
        return P.LLMResult(text="Открываю Telegram.", tool_calls=[P.ToolCall(id="1", name="open_app", args={"name": "Telegram"})],
                           raw_message={"role": "assistant", "content": "Открываю Telegram."})
    b, provs = _brain(beh, beh)
    class Act:
        def execute(self, n, a): return {"ok": True}
    b.actions = Act()
    b.local = Route(["ollama"])
    assert b.ask("открой телеграм", on_text=lambda d: None) == "Открываю Telegram." and len(rounds) == 1
    # info tools still get a second round
    assert not B._nothing_to_add([P.ToolCall(id="1", name="find_files", args={})], [{"ok": True, "files": []}])
    assert not B._nothing_to_add([P.ToolCall(id="1", name="open_app", args={})], [{"ok": False, "error": "нет"}])


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
