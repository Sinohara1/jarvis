"""v1.5: compare local Ollama models (and Gemini lite as the baseline) on the user's PC.

  python tests/local_compare.py --models gemma4:12b,qwen3.5:9b,ministral-3:14b [--cloud] [--out FILE]

Per model: cold load time, VRAM (ollama ps + nvidia-smi), first-token latency (voice path, warm),
Russian/Ukrainian/English answers (printed for review + script checks), tool-call accuracy on the app's
test phrases (actions are recorded, never executed), screenshot judgment on the live-mode test screens
(drawn in memory). Uses the real settings for the cloud key (never printed). Aborts if a game is in front."""
import argparse, json, os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis_app import config, providers as P
from jarvis_app.config import load_settings, normalize_settings, PROVIDERS
from jarvis_app.brain import Brain
from jarvis_app.local_ai import OllamaManager, classify_app
from jarvis_app.live import LivePolicy, build_prompt
from jarvis_app.watcher import image_to_jpeg, get_foreground, is_fullscreen
import tests.live_mode_test as LM

if os.name == "nt":  # live_mode_test draws with DejaVu (Linux); use Windows fonts here
    LM.FONT, LM.MONO = r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\consola.ttf"

ap = argparse.ArgumentParser()
ap.add_argument("--models", default="gemma4:12b,qwen3.5:9b,ministral-3:14b")
ap.add_argument("--cloud", action="store_true", help="also run Gemini lite (fewer calls: latency + tools)")
ap.add_argument("--only", default="", help="comma list of sections: load,lat,lang,tools,screen")
ap.add_argument("--out", default="local_compare.json")
ap.add_argument("--reps", type=int, default=3)
a = ap.parse_args()
SECTIONS = set((a.only or "load,lat,lang,tools,screen").split(","))
mgr = OllamaManager()


def gaming() -> str:
    fg = get_foreground()
    if fg.process and classify_app(fg.process, fg.path, is_fullscreen(fg)) == "game":
        return fg.process
    return ""


def wait_not_gaming():
    g = gaming()
    while g:
        print(f"  [game in front: {g}] waiting…", flush=True)
        mgr.unload_all()
        time.sleep(30)
        g = gaming()


def smi() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=0x08000000 if os.name == "nt" else 0).stdout
        return int(out.split()[0])
    except Exception:
        return -1


class Rec:
    def __init__(self): self.calls = []
    def execute(self, name, args):
        self.calls.append((name, dict(args or {})))
        if name == "find_files":
            return {"ok": True, "files": [r"C:\Users\vova\Documents\Реферат по истории.docx"]}
        if name == "get_time":
            return {"ok": True, "time": time.strftime("%H:%M"), "date": time.strftime("%d.%m.%Y")}
        if name == "look_at_screen":
            return {"ok": True, "description": "на экране редактор кода VS Code с файлом bot.py"}
        if name == "focus_status":
            return {"active": False}
        return {"ok": True}


class LocalOnly:
    def __init__(self, on): self.on = on
    def route(self, kind, cloud, ok): return ["ollama"] if self.on else [cloud]
    def keep_alive(self): return "10m"
    def note_result(self, ok, err=""): pass


base = load_settings()


def make_brain(model: str | None):
    s = dict(base)
    s["models"] = {k: dict(v) for k, v in base["models"].items()}
    if model:
        s["ai_route"] = "local"
        s["models"]["ollama"] = {"chat": model, "lite": model}
    else:
        s["ai_route"] = "cloud"; s["provider"] = "gemini"
    s["confirm_actions"] = False; s["user_name"] = "Вова"
    s = normalize_settings(s)
    rec = Rec()
    b = Brain(lambda: s, rec, lambda: "Фокус-сессия не запущена.")
    b.local = LocalOnly(bool(model))
    return b, rec, s


def has(calls, name, pred=lambda a: True):
    return any(n == name and pred(ar) for n, ar in calls)


TOOL_CASES = [
    ("Айри, открой телеграм.", lambda c, r: has(c, "open_app", lambda x: "telegram" in str(x).lower() or "телеграм" in str(x).lower())),
    ("Поставь напоминание через десять минут выпить воды.", lambda c, r: has(c, "set_reminder", lambda x: float(x.get("minutes") or 0) == 10 or x.get("at_time"))),
    ("Включи живой режим.", lambda c, r: has(c, "set_live_mode", lambda x: x.get("enabled") is True)),
    ("Найди на компьютере файл с рефератом по истории.", lambda c, r: has(c, "find_files")),
    ("Я делаю домашку по физике сорок минут.", lambda c, r: has(c, "start_focus", lambda x: int(x.get("minutes") or 0) == 40)),
    ("Открой ютуб и найди музыку для концентрации.", lambda c, r: has(c, "web_search", lambda x: "youtube" in str(x).lower()) or has(c, "open_url", lambda x: "youtube" in str(x).lower())),
    ("я сейчас делаю домашку по физике 40 минут. И открой сайт youtube.com, мне нужен урок",
     lambda c, r: has(c, "start_focus", lambda x: int(x.get("minutes") or 0) == 40) and has(c, "open_url", lambda x: "youtube" in str(x).lower())),
    ("напомни через 10 минут выпить воды", lambda c, r: has(c, "set_reminder")),
    ("Следи и подсказывай", lambda c, r: has(c, "set_live_mode", lambda x: x.get("enabled") is True)),
    ("Хватит, выключи живой режим", lambda c, r: has(c, "set_live_mode", lambda x: x.get("enabled") is False)),
    ("тихо, не мешай со своими подсказками", lambda c, r: has(c, "set_live_mode", lambda x: x.get("enabled") is False)),
    ("Заверши фокус-сессию", lambda c, r: has(c, "stop_focus")),
    ("Что у меня на экране?", lambda c, r: has(c, "look_at_screen")),
    ("Какая столица Австралии?", lambda c, r: not c and "канберр" in r.lower()),
    ("Почему небо голубое?", lambda c, r: not c and len(r) > 20),
    ("Сколько сейчас времени?", lambda c, r: bool(re.search(r"\d{1,2}[:.]\d{2}|час", r.lower()))),
    ("Відкрий калькулятор", lambda c, r: has(c, "open_app", lambda x: "calc" in str(x).lower() or "кальк" in str(x).lower())),
    ("Open Spotify please", lambda c, r: has(c, "open_app", lambda x: "spotify" in str(x).lower())),
]

LANG_CASES = [
    ("ru", "Объясни простыми словами, что такое фотосинтез. Два предложения."),
    ("ru", "Придумай короткую мотивирующую фразу для школьника, который ленится делать уроки."),
    ("ru", "Чем отличается вирус от бактерии? Коротко."),
    ("uk", "Поясни, чому влітку тепліше, ніж узимку. Два речення."),
    ("uk", "Порадь, як не відволікатися на телефон під час навчання."),
    ("en", "Give me two quick tips to stay focused while studying."),
]
LAT_CASES = ["Какая столица Австралии?", "Почему небо голубое?", "Расскажи короткий анекдот про программистов."]

def script_ok(lang, text):
    cyr = len(re.findall(r"[а-яёіїєґ]", text.lower())); lat = len(re.findall(r"[a-z]", text.lower()))
    cjk = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]", text))
    if cjk: return False
    if lang == "uk": return cyr > 20 and bool(re.search(r"[іїєґ]", text.lower())) and not re.search(r"[ыэъё]", text.lower())
    if lang == "ru": return cyr > 20 and lat < cyr * 0.1
    return lat > 20 and cyr == 0


def run_model(model: str | None) -> dict:
    label = model or "gemini:" + PROVIDERS["gemini"]["lite"]
    print(f"\n===== {label} =====", flush=True)
    res: dict = {"model": label}
    brain, rec, s = make_brain(model)
    wait_not_gaming()
    if model and "load" in SECTIONS:
        mgr.unload_all(); time.sleep(3)
        v0 = smi(); t = time.monotonic()
        mgr.load(model, keep_alive_min=10, timeout=300)
        res["cold_load_s"] = round(time.monotonic() - t, 1)
        time.sleep(1)
        info = mgr.is_loaded(model) or {}
        res["ps"] = info; res["vram_before_mb"] = v0; res["vram_after_mb"] = smi()
        res["ollama_ps"] = subprocess.run([os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"), "ps"],
                                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip() if os.name == "nt" else ""
        print(f"  load {res['cold_load_s']}s, ps={info}, nvidia-smi {v0} -> {res['vram_after_mb']} MB", flush=True)
        print("  " + res["ollama_ps"].replace("\n", "\n  "), flush=True)
        # first real prompt after a load (system prompt + tools ≈ 2k tokens); the very first run on a new GPU
        # also JIT-compiles CUDA kernels (cached by the driver afterwards)
        from jarvis_app.actions import TOOLS
        prov = brain.provider("ollama"); t = time.monotonic()
        prov.chat(model, brain._system(True), [prov.user_message("Привет")], TOOLS, timeout=600)
        res["first_prompt_s"] = round(time.monotonic() - t, 1)
        print(f"  first prompt after load: {res['first_prompt_s']}s", flush=True)
    if "lat" in SECTIONS:
        lats, totals = [], []
        for rep in range(a.reps):
            for q in LAT_CASES:
                brain.reset(); first = []
                t = time.monotonic()
                try:
                    r = brain.ask(q, on_text=lambda d: first or first.append(time.monotonic()), voice=True, fast=True)
                except Exception as e:
                    r = f"ERROR {type(e).__name__}: {e}"
                tt = (first[0] if first else time.monotonic()) - t
                lats.append(tt); totals.append(time.monotonic() - t)
                if rep == 0: print(f"  ttft {tt:.2f}s total {totals[-1]:.2f}s | {q} -> {r[:100]!r}", flush=True)
                if not model: time.sleep(4)
        lats.sort(); totals.sort()
        res["ttft_median_s"] = round(lats[len(lats) // 2], 2); res["ttft_max_s"] = round(lats[-1], 2)
        res["reply_total_median_s"] = round(totals[len(totals) // 2], 2)
        print(f"  TTFT median {res['ttft_median_s']}s max {res['ttft_max_s']}s; full reply median {res['reply_total_median_s']}s", flush=True)
    if "lang" in SECTIONS and model:
        out = []
        for lang, q in LANG_CASES:
            s["answer_lang"] = lang; brain.reset()
            t = time.monotonic()
            try:
                r = brain.ask(q, voice=True)
            except Exception as e:
                r = f"ERROR {type(e).__name__}: {e}"
            ok = script_ok(lang, r)
            out.append({"lang": lang, "q": q, "a": r, "script_ok": ok, "s": round(time.monotonic() - t, 1)})
            print(f"  [{lang}] {'ok ' if ok else 'BAD'} {time.monotonic()-t:.1f}s {r!r}", flush=True)
        s.pop("answer_lang", None)
        res["lang"] = out; res["lang_script_ok"] = sum(o["script_ok"] for o in out)
    if "tools" in SECTIONS:
        good, rows = 0, []
        for q, chk in TOOL_CASES:
            brain.reset(); rec.calls.clear()
            t = time.monotonic()
            try:
                r = brain.ask(q, voice=True)
            except Exception as e:
                r = f"ERROR {type(e).__name__}: {e}"
            try:
                ok = bool(chk(rec.calls, r))
            except Exception:
                ok = False
            good += ok
            rows.append({"q": q, "ok": ok, "calls": rec.calls[:], "reply": r, "s": round(time.monotonic() - t, 1)})
            print(f"  {'PASS' if ok else 'FAIL'} {time.monotonic()-t:.1f}s {q} -> {rec.calls} {r[:80]!r}", flush=True)
            if not model: time.sleep(4)
        res["tools"] = rows; res["tools_ok"] = f"{good}/{len(TOOL_CASES)}"
        print(f"  tools {res['tools_ok']}", flush=True)
    if "screen" in SECTIONS:
        rows, good = [], 0
        want = {"code_error": True, "tiktok": True, "essay": False, "code_ok": False}
        st = dict(s, live_mode=True, live_talk="some")
        for kind, should in want.items():
            img, title, proc = LM.screen(kind)
            jpeg = image_to_jpeg(img, max_side=1024, quality=60)
            pol = LivePolicy(); pol.reset()
            if kind == "tiktok":
                pol.remember(-90.0, "листает TikTok", title, silent_reason="первое наблюдение")
            sysp, up = build_prompt(st, title=title, process=proc, since_remark=None, memory=pol.memory_lines(0.0))
            t = time.monotonic()
            try:
                d = brain.live_decide(jpeg, sysp, up); acc, why = pol.accept(d, 1000.0, st)
            except Exception as e:
                d, acc, why = {"speak": None, "text": "", "reason": f"ERROR {e}"}, None, "error"
            ok = acc == should; good += ok
            rows.append({"case": "live:" + kind, "ok": ok, "speak": acc, "s": round(time.monotonic() - t, 1), "text": d.get("text") or d.get("reason"), "via": d.get("provider") or d.get("model")})
            print(f"  {'PASS' if ok else 'FAIL'} live {kind:10s} {time.monotonic()-t:.1f}s say={acc} via={d.get('provider') or d.get('model')} | {d.get('text') or d.get('reason')}", flush=True)
            if not model: time.sleep(4)
        for kind, task, want_on in (("tiktok", "сочинение по литературе", False), ("essay", "сочинение по литературе", True),
                                    ("code_error", "домашка по программированию", True)):
            img, title, proc = LM.screen(kind); jpeg = image_to_jpeg(img)
            t = time.monotonic()
            try:
                v = brain.classify_screen(jpeg, task, title, proc)
            except Exception as e:
                v = {"valid": False, "error": str(e)}
            ok = bool(v.get("valid")) and v.get("on_task") == want_on; good += ok
            rows.append({"case": "focus:" + kind, "ok": ok, "verdict": v, "s": round(time.monotonic() - t, 1)})
            print(f"  {'PASS' if ok else 'FAIL'} focus {kind:10s} {time.monotonic()-t:.1f}s {v}", flush=True)
            if not model: time.sleep(4)
        img, title, proc = LM.screen("code_error")
        t = time.monotonic()
        try:
            d = brain.describe_screen(image_to_jpeg(img), "Что за ошибка у меня в терминале и как её исправить?")
        except Exception as e:
            d = f"ERROR {e}"
        ok = "scores" in d.lower() or "score" in d.lower(); good += ok
        rows.append({"case": "describe:code_error", "ok": ok, "text": d, "s": round(time.monotonic() - t, 1)})
        print(f"  {'PASS' if ok else 'FAIL'} describe {time.monotonic()-t:.1f}s {d[:200]!r}", flush=True)
        res["screen"] = rows; res["screen_ok"] = f"{good}/{len(rows)}"
        print(f"  screen {res['screen_ok']}", flush=True)
    if model:
        res["ps_end"] = mgr.is_loaded(model); res["vram_end_mb"] = smi()
    return res


results = []
if os.path.exists(a.out):
    try: results = json.load(open(a.out, encoding="utf-8"))
    except Exception: results = []
import traceback
for m in [x for x in a.models.split(",") if x]:
    try:
        results.append(run_model(m))
    except Exception:
        traceback.print_exc(); sys.stdout.flush()
        results.append({"model": m, "error": traceback.format_exc()[-500:]})
    json.dump(results, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
if a.cloud:
    results.append(run_model(None))
    json.dump(results, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\nSUMMARY")
for r in results:
    print(json.dumps({k: r.get(k) for k in ("model", "cold_load_s", "ttft_median_s", "ttft_max_s", "reply_total_median_s",
                                            "tools_ok", "screen_ok", "lang_script_ok", "vram_after_mb")}, ensure_ascii=False))
