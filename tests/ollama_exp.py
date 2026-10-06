"""Experiment: which request options make gemma4 tool calls reliable (empty/length replies).
Gemma 4 with think=false sometimes loops on empty thought blocks (<|channel>thought\\n<channel|>) until num_predict."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from jarvis_app.config import load_settings, normalize_settings
from jarvis_app.brain import Brain
from jarvis_app.actions import TOOLS
from jarvis_app.local_ai import OllamaProvider
model = sys.argv[1]; reps = int(sys.argv[2]); only = sys.argv[3].split(",") if len(sys.argv) > 3 else None
URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
texts = ["Я делаю домашку по физике сорок минут.", "Что у меня на экране?", "я сейчас делаю домашку по физике 40 минут. И открой сайт youtube.com, мне нужен урок",
         "Поставь напоминание через десять минут выпить воды.", "Почему небо голубое?"]
s = dict(load_settings()); s.pop("answer_lang", None); s = normalize_settings(s)
b = Brain(lambda: s, None, lambda: "Фокус-сессия не запущена.")
p = OllamaProvider()
variants = {
    "base": lambda pl: None,
    "stop_channel": lambda pl: pl["options"].update(stop=["<|channel>"]),
    "stop_channel2": lambda pl: pl["options"].update(stop=["<channel|>"]),
    "rp1.2": lambda pl: pl["options"].update(repeat_penalty=1.2),
    "presence1.0": lambda pl: pl["options"].update(presence_penalty=1.0),
    "think_np1500": lambda pl: (pl.update(think=True), pl["options"].update(num_predict=1500)),
    "think_low": lambda pl: pl.update(think="low"),
}
for vn, fn in variants.items():
    if only and vn not in only: continue
    good = bad = 0; ts = []
    for text in texts:
        for _ in range(reps):
            pl = p._payload(model, b._system(True), [p.user_message(text)], TOOLS, 0.7, 600, False)
            fn(pl)
            t = time.monotonic()
            d = requests.post(URL + "/api/chat", json=pl, timeout=120).json()
            if "error" in d: print("  ERR", d["error"][:200]); bad += 1; continue
            m = d.get("message") or {}
            want_tool = "небо" not in text
            ok = (bool(m.get("tool_calls")) if want_tool else bool(m.get("content"))) and d.get("done_reason") == "stop"
            good += ok; bad += not ok; ts.append(time.monotonic() - t)
            if not ok: print(f"  {vn} BAD {text[:30]!r} content={m.get('content','')[:60]!r} think={len(m.get('thinking') or '')} eval={d.get('eval_count')} {d.get('done_reason')} {ts[-1]:.1f}s", flush=True)
    ts.sort()
    print(f"{vn}: ok {good}/{good+bad} median {ts[len(ts)//2]:.2f}s max {ts[-1]:.2f}s", flush=True)
