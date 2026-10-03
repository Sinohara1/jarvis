"""Live AI-layer test against the real provider (needs GEMINI_API_KEY in env).
Never prints the key. Run: python tests/live_ai_test.py"""
import asyncio, os, subprocess, sys, time, webbrowser
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw, ImageFont
from jarvis_app.config import normalize_settings
from jarvis_app.brain import Brain
from jarvis_app.actions import Actions
from jarvis_app.watcher import image_to_jpeg

opened = []
webbrowser.open = lambda u, *a, **k: opened.append(u) or True

class FakeCore:
    def __init__(self): self.calls = []
    def focus_start(self, task, minutes=None, rounds=None):
        self.calls.append(("focus_start", task, minutes, rounds)); return {"ok": True, "task": task, "minutes": minutes or 25}
    def focus_stop(self): self.calls.append(("focus_stop",)); return {"ok": True}
    def focus_pause(self, resume=False): return {"ok": True}
    def focus_status(self): return {"active": False}
    def add_reminder(self, text, due): self.calls.append(("reminder", text, due.strftime("%H:%M"))); return {"ok": True, "at": due.strftime("%H:%M")}
    def list_reminders(self): return {"reminders": []}
    def cancel_reminders(self): return {"ok": True}
    def look_at_screen(self, q): return {"ok": True, "description": "на экране редактор кода"}

settings = normalize_settings({})
core = FakeCore()
actions = Actions(core)
brain = Brain(lambda: settings, actions, lambda: "Фокус-сессия не запущена.")
ok = True

def check(name, cond, info=""):
    global ok
    ok &= bool(cond)
    print(("PASS" if cond else "FAIL"), name, info)

# 1. text
t = time.time(); r = brain.ask("Привет! Ты кто? Ответь одной фразой.")
check("text reply", len(r) > 3, f"{time.time()-t:.1f}s -> {r!r}")

# 2. function calling
t = time.time(); r = brain.ask("я сейчас делаю домашку по физике 40 минут. И открой сайт youtube.com, мне нужен урок")
fc = [c for c in core.calls if c[0] == "focus_start"]
check("tool start_focus", fc and fc[0][2] == 40, f"{fc}")
check("tool open_url", any("youtube" in u for u in opened), f"{opened} reply={r!r} {time.time()-t:.1f}s")
r = brain.ask("напомни через 10 минут выпить воды")
check("tool set_reminder", any(c[0] == "reminder" for c in core.calls), f"{core.calls[-1]} reply={r!r}")

# 3. audio understanding (TTS-generated Russian speech → 16 kHz wav)
import edge_tts
async def synth():
    for i in range(3):
        try:
            await edge_tts.Communicate("Джарвис, сколько сейчас времени?", "ru-RU-DmitryNeural").save("/tmp/q.mp3"); return
        except Exception as e: print("tts retry", e); await asyncio.sleep(1)
asyncio.run(synth())
subprocess.run(["ffmpeg", "-y", "-loglevel", "quiet", "-i", "/tmp/q.mp3", "-ar", "16000", "-ac", "1", "/tmp/q.wav"], check=True)
wav = open("/tmp/q.wav", "rb").read()
t = time.time(); tr = brain.transcribe(wav)
check("audio transcription", "врем" in tr.lower() or "час" in tr.lower(), f"{time.time()-t:.1f}s -> {tr!r}")
r = brain.ask(tr)
check("answer to voice", len(r) > 3, repr(r))
# silence → empty
import wave, io
b = io.BytesIO(); w = wave.open(b, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0" * 16000); w.close()
tr0 = brain.transcribe(b.getvalue())
check("silence → empty transcript", tr0 == "", repr(tr0))

# 4. screen classification
def fake_screen(kind):
    img = Image.new("RGB", (1920, 1080), (18, 18, 18))
    d = ImageDraw.Draw(img)
    try: f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 42); fs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except Exception: f = fs = ImageFont.load_default()
    if kind == "tiktok":
        d.rectangle([0, 0, 1920, 70], fill=(30, 30, 30)); d.text((20, 15), "TikTok - Make Your Day   |  For You   Following   LIVE", font=fs, fill="white")
        d.rectangle([700, 90, 1220, 1060], fill=(60, 20, 80)); d.text((730, 900), "@funnycats  #fyp #viral #cats", font=fs, fill="white")
        d.text((730, 950), "♫ original sound - funnycats", font=fs, fill="white")
        for i, s in enumerate(["♥ 1.2M", "💬 8932", "↗ Share"]): d.text((1250, 500 + i * 90), s, font=fs, fill="white")
    else:
        img = Image.new("RGB", (1920, 1080), (250, 250, 250)); d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 1920, 60], fill=(43, 87, 154)); d.text((20, 12), "Домашнее задание по физике.docx - Word", font=fs, fill="white")
        y = 120
        for line in ["Задача 1. Тело брошено вертикально вверх со скоростью 20 м/с.", "Найти максимальную высоту подъёма.", "Решение: h = v² / (2g) = 400 / 19.6 ≈ 20.4 м", "Задача 2. Определите период колебаний маятника длиной 1 м."]:
            d.text((120, y), line, font=f, fill=(20, 20, 20)); y += 80
    return image_to_jpeg(img)

for kind, want in (("tiktok", False), ("homework", True)):
    t = time.time(); v = brain.classify_screen(fake_screen(kind), "домашка по физике", "TikTok - Make Your Day" if kind == "tiktok" else "Домашнее задание по физике.docx - Word", "chrome.exe" if kind == "tiktok" else "WINWORD.EXE")
    check(f"screen classify {kind}", v["valid"] and v["on_task"] == want, f"{time.time()-t:.1f}s -> {v}")

# 5. describe screen
d = brain.describe_screen(fake_screen("homework"), "Что у меня на экране?")
check("describe screen", len(d) > 5, repr(d))
print("ALL OK" if ok else "SOME FAILED")
