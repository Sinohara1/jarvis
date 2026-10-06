"""Debug one local turn: raw Ollama /api/chat with the app's system prompt + tools (non-stream), prints message fields."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from jarvis_app.config import load_settings, normalize_settings
from jarvis_app.brain import Brain
from jarvis_app.actions import TOOLS
from jarvis_app.local_ai import OllamaProvider
model = sys.argv[1]; texts = sys.argv[2:]
s = dict(load_settings()); s.pop("answer_lang", None); s = normalize_settings(s)
b = Brain(lambda: s, None, lambda: "Фокус-сессия не запущена.")
p = OllamaProvider()
for text in texts:
    for stream in (False, True):
        pl = p._payload(model, b._system(True), [p.user_message(text)], TOOLS, 0.7, 600, stream)
        t = time.monotonic()
        r = requests.post(os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat", json=pl, timeout=120, stream=stream)
        if not stream:
            d = r.json(); m = d.get("message") or {}
            print(f"[{time.monotonic()-t:.1f}s] {text!r} content={m.get('content')!r} thinking={str(m.get('thinking'))[:300]!r} tools={m.get('tool_calls')} eval={d.get('eval_count')} done={d.get('done_reason')}", flush=True)
        else:
            parts, th, calls, n = [], [], [], 0
            for line in r.iter_lines():
                if not line: continue
                d = json.loads(line); m = d.get("message") or {}; n += 1
                parts.append(m.get("content") or ""); th.append(m.get("thinking") or "")
                if m.get("tool_calls"): calls += m["tool_calls"]
                if d.get("done"): print(f"   stream [{time.monotonic()-t:.1f}s] chunks={n} content={''.join(parts)!r} thinking={''.join(th)[:300]!r} tools={calls} eval={d.get('eval_count')} done={d.get('done_reason')}", flush=True)
