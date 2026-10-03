"""Windows smoke check (no mic recording, no audio playback, screen not sent anywhere)."""
import os, sys, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis_app import config, watcher, actions, audio
from jarvis_app.brain import Brain

s = config.load_settings()
print("settings ok; provider", s["provider"], "has gemini key:", bool(config.api_key_for(s, "gemini")))
fg = watcher.get_foreground(); print("foreground:", fg.process, "|", fg.title[:60])
t = time.time(); jpg = watcher.capture_screen_jpeg(); print("screen jpeg bytes:", len(jpg or b""), f"{time.time()-t:.2f}s")
idx = actions.AppIndex(); t = time.time(); idx.build()
print("app index:", len(idx.shortcuts), "shortcuts,", len(idx.uwp), "uwp", f"{time.time()-t:.1f}s")
for q in ("калькулятор", "телеграм", "хром", "блокнот", "дискорд", "стим", "вскод"):
    b = idx.best(q); print("  ", q, "->", (b[1], b[2][:60]) if b else None)
import sounddevice as sd
try:
    print("default input:", sd.query_devices(kind="input")["name"], "| output:", sd.query_devices(kind="output")["name"])
except Exception as e:
    print("sd err", e)
p = os.path.join(tempfile.gettempdir(), "jarvis_tts_test.mp3")
t = time.time(); audio.synth_to_file("Проверка голоса. Я готов.", s["voice"], 5, p); print("tts bytes:", os.path.getsize(p), f"{time.time()-t:.1f}s"); os.remove(p)
b = Brain(lambda: s, None)
t = time.time(); print("test_connection:", b.test_connection("gemini"), f"{time.time()-t:.1f}s")
