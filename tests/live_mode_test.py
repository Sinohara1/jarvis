"""Live-mode + name-wake tests against the real provider (needs GEMINI_API_KEY in env; never printed).
Synthetic screenshots are drawn in memory (nothing written to disk).
Run: python tests/live_mode_test.py [--wake <vosk-model-dir> <wav-dir>]"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw, ImageFont
from jarvis_app.config import normalize_settings
from jarvis_app.brain import Brain
from jarvis_app.actions import Actions
from jarvis_app.live import LivePolicy, build_prompt
from jarvis_app.watcher import image_to_jpeg
from jarvis_app import providers as P

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
def F(sz, mono=False):
    try: return ImageFont.truetype(MONO if mono else FONT, sz)
    except Exception: return ImageFont.load_default()

def taskbar(d, title):
    d.rectangle([0, 1040, 1920, 1080], fill=(32, 32, 32)); d.text((20, 1050), "⊞  " + title, font=F(18), fill="white")

def screen(kind):
    if kind == "code_error":
        img = Image.new("RGB", (1920, 1080), (30, 30, 30)); d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 1920, 34], fill=(50, 50, 50)); d.text((12, 6), "bot.py - homework_bot - Visual Studio Code", font=F(18), fill=(220, 220, 220))
        code = ["import json", "", "def load_scores(path):", "    with open(path) as f:", "        data = json.load(f)",
                "    return data", "", "scores = load_scores('scores.json')", "total = 0", "for s in scores:",
                "    total += s['points']", "print('Среднее:', total / len(score))"]
        for i, l in enumerate(code):
            d.text((60, 60 + i * 30), f"{i+1:>3}  {l}", font=F(20, True), fill=(212, 212, 212))
        d.line([(430, 60 + 11 * 30 + 26), (520, 60 + 11 * 30 + 26)], fill=(240, 60, 60), width=3)
        d.rectangle([0, 640, 1920, 1040], fill=(24, 24, 24)); d.text((12, 648), "TERMINAL   PROBLEMS (1)   OUTPUT", font=F(16), fill=(200, 200, 200))
        tb = ["PS C:\\Users\\vova\\homework_bot> python bot.py", "Traceback (most recent call last):",
              '  File "C:\\Users\\vova\\homework_bot\\bot.py", line 12, in <module>',
              "    print('Среднее:', total / len(score))", "                                  ^^^^^",
              "NameError: name 'score' is not defined. Did you mean: 'scores'?", "PS C:\\Users\\vova\\homework_bot> "]
        for i, l in enumerate(tb):
            d.text((20, 690 + i * 30), l, font=F(20, True), fill=(240, 90, 90) if "Error" in l else (220, 220, 220))
        taskbar(d, "Visual Studio Code"); return img, "bot.py - homework_bot - Visual Studio Code", "Code.exe"
    if kind == "code_ok":
        img = Image.new("RGB", (1920, 1080), (30, 30, 30)); d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 1920, 34], fill=(50, 50, 50)); d.text((12, 6), "game.py - snake - Visual Studio Code", font=F(18), fill=(220, 220, 220))
        code = ["import pygame", "import random", "", "WIDTH, HEIGHT = 640, 480", "CELL = 20", "", "class Snake:",
                "    def __init__(self):", "        self.body = [(5, 5), (4, 5), (3, 5)]", "        self.dir = (1, 0)", "",
                "    def move(self):", "        head = self.body[0]", "        new = (head[0] + self.dir[0], head[1] + self.dir[1])",
                "        self.body.insert(0, new)", "        self.body.pop()", "", "    def grow(self):", "        self.body.append(self.body[-1])", "", "    def"]
        for i, l in enumerate(code):
            d.text((60, 60 + i * 30), f"{i+1:>3}  {l}", font=F(20, True), fill=(212, 212, 212))
        d.rectangle([60 + 14 * 12 + 80, 60 + 20 * 30, 60 + 14 * 12 + 82, 60 + 20 * 30 + 24], fill="white")
        taskbar(d, "Visual Studio Code"); return img, "game.py - snake - Visual Studio Code", "Code.exe"
    if kind == "tiktok":
        img = Image.new("RGB", (1920, 1080), (18, 18, 18)); d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 1920, 70], fill=(30, 30, 30)); d.text((20, 15), "TikTok - Make Your Day   |  For You   Following   LIVE", font=F(28), fill="white")
        d.rectangle([700, 90, 1220, 1030], fill=(60, 20, 80)); d.text((730, 900), "@funnycats  #fyp #viral #cats", font=F(28), fill="white")
        d.text((730, 950), "♫ original sound - funnycats", font=F(28), fill="white")
        for i, s_ in enumerate(["♥ 1.2M", "💬 8932", "↗ Share"]): d.text((1250, 500 + i * 90), s_, font=F(28), fill="white")
        taskbar(d, "Google Chrome — TikTok"); return img, "TikTok - Make Your Day - Google Chrome", "chrome.exe"
    if kind == "essay":
        img = Image.new("RGB", (1920, 1080), (243, 243, 243)); d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 1920, 60], fill=(43, 87, 154)); d.text((20, 15), "Сочинение_Война_и_мир.docx - Word", font=F(26), fill="white")
        d.rectangle([360, 100, 1560, 1030], fill="white")
        lines = ["Сочинение: «Образ Наташи Ростовой в романе „Война и мир“»", "",
                 "Наташа Ростова — одна из самых живых и обаятельных героинь русской",
                 "литературы. Толстой показывает её взросление: от весёлой девочки на",
                 "именинах до зрелой женщины, пережившей потери и войну.",
                 "В сцене первого бала мы видим её искренность и умение радоваться",
                 "жизни. Позже, после ошибки с Анатолем Курагиным, Наташа глубоко",
                 "раскаивается, и это показывает её нравственный рост.", "Во время пожара Москвы"]
        for i, l in enumerate(lines):
            d.text((430, 150 + i * 48), l, font=F(28 if i else 30), fill=(20, 20, 20))
        taskbar(d, "Word"); return img, "Сочинение_Война_и_мир.docx - Word", "WINWORD.EXE"
    raise ValueError(kind)

def main():
    settings = normalize_settings({"user_name": "Вова", "live_mode": True})
    class Core:
        live_calls = []
        def set_live(self, on): self.live_calls.append(on); return {"ok": True, "live_mode": on}
        def focus_status(self): return {"active": False}
    core = Core()
    brain = Brain(lambda: settings, Actions(core), lambda: "Фокус-сессия не запущена.")
    ok = True
    def check(name, cond, info=""):
        nonlocal ok
        ok &= bool(cond); print(("PASS" if cond else "FAIL"), name, info, flush=True)

    # 1. decisions on synthetic screens; expectation: speak on obvious cases, silent on normal work
    want = {"code_error": True, "tiktok": True, "essay": False, "code_ok": False}
    only = os.environ.get("ONLY")
    for talk in ("some", "rare"):
        st = dict(settings, live_talk=talk)
        for kind, should in want.items():
            if only and kind != only: continue
            img, title, proc = screen(kind)
            jpeg = image_to_jpeg(img, max_side=1024, quality=60)
            spoke = []
            for rep in range(2):
                pol = LivePolicy(); pol.reset()
                if kind == "tiktok":  # the loop has already seen him scrolling once and stayed quiet
                    pol.remember(-90.0, "листает TikTok", title, silent_reason="первое наблюдение")
                sysp, up = build_prompt(st, title=title, process=proc, since_remark=None, memory=pol.memory_lines(0.0))
                for attempt in range(4):
                    try:
                        t = time.time(); d = brain.live_decide(jpeg, sysp, up); break
                    except P.RateLimitError as e:
                        w = min(60, (e.retry_after or 20) + 1); print("  429, wait", w); time.sleep(w)
                acc, why = pol.accept(d, 1000.0, st)
                spoke.append(acc)
                print(f"  [{talk}] {kind:10s} speak={d['speak']!s:5} kind={d['kind']:8s} conf={d['confidence']:.2f} -> {'SAY' if acc else 'silent'} ({why or 'ok'}) "
                      f"{time.time()-t:.1f}s via {d.get('model')} | {d['text'] or d['reason']}")
                time.sleep(4)
            check(f"live [{talk}] {kind}", all(x == should for x in spoke) if not should else any(spoke) if talk == "rare" else all(spoke),
                  f"expected {'speak' if should else 'silent'}")

    # 2. memory: after a remark about the same error, it should not repeat itself
    img, title, proc = screen("code_error"); jpeg = image_to_jpeg(img, 1024, 60)
    pol = LivePolicy(); pol.reset()
    pol.remember(0.0, "исправляет NameError в bot.py", title, said="В двенадцатой строке опечатка: score вместо scores.", kind="hint")
    pol.on_remark(-400.0)
    sysp, up = build_prompt(settings, title=title, process=proc, since_remark=400, memory=pol.memory_lines(240.0))
    d = brain.live_decide(jpeg, sysp, up); acc, why = pol.accept(d, 1000.0, settings)
    check("no repeat of the same hint", not acc, f"speak={d['speak']} why={why} | {d['text'] or d['reason']}")
    time.sleep(4)

    # 3. intents via function calling
    for phrase, want_on in (("Следи и подсказывай", True), ("Хватит, выключи живой режим", False), ("Включи живой режим", True), ("тихо, не мешай со своими подсказками", False)):
        core.live_calls.clear()
        for attempt in range(4):
            try:
                r = brain.ask(phrase); break
            except P.RateLimitError as e:
                time.sleep(min(60, (e.retry_after or 20) + 1))
        check(f"intent «{phrase}»", core.live_calls[-1:] == [want_on], f"calls={core.live_calls} reply={r!r}")
        time.sleep(3)

    # 4. name wake end-to-end: «Пятница, открой телеграм» in one breath → cut request → Gemini transcript
    if "--wake" in sys.argv:
        import wave, numpy as np, vosk, glob
        vosk.SetLogLevel(-1)
        mdir, wdir = sys.argv[sys.argv.index("--wake") + 1], sys.argv[sys.argv.index("--wake") + 2]
        from jarvis_app.namewake import NameSpotter, name_variants
        model = vosk.Model(mdir)
        rng = np.random.default_rng(3)
        def load(p):
            w = wave.open(p); return np.frombuffer(w.readframes(w.getnframes()), np.int16)
        def stream(arrs):
            parts = []
            for a in arrs: parts += [np.zeros(16000, np.int16), a]
            parts.append(np.zeros(32000, np.int16))
            x = np.concatenate(parts).astype(np.float32) + rng.normal(0, 80, sum(len(p) for p in parts))
            return np.clip(x, -32768, 32767).astype(np.int16)
        def run(name, files):
            sp = NameSpotter(model, name_variants(name), 0.5); dets = []
            x = stream([load(os.path.join(wdir, f)) for f in files])
            for i in range(0, len(x), 1280):
                d = sp.feed(x[i:i + 1280])
                if d: dets.append(d); sp.reset()
            return dets
        normals = sorted(os.path.basename(p) for p in glob.glob(os.path.join(wdir, "normal*.wav")) + glob.glob(os.path.join(wdir, "pt_mid*.wav"))
                         + glob.glob(os.path.join(wdir, "ai_mid*.wav")) + glob.glob(os.path.join(wdir, "name_al*.wav")))
        for name, pos in (("Пятница", ["name_pt_D.wav", "name_pt_S.wav", "ey_pt_D.wav", "ey_pt_S.wav", "pt_req_D.wav", "pt_req_S.wav"]),
                          ("Джарвис", ["name_dj_D.wav", "name_dj_S.wav", "ey_dj_D.wav", "ey_dj_S.wav"]),
                          ("Airi", ["name_ai_D.wav", "name_ai_S.wav", "ey_ai_D.wav", "ey_ai_S.wav", "ai_req_D.wav", "ai_req_S.wav"])):
            if not all(os.path.exists(os.path.join(wdir, f)) for f in pos):
                continue
            hits = sum(1 for f in pos if run(name, [f]))
            check(f"name wake «{name}» detects", hits == len(pos), f"{hits}/{len(pos)}")
            fp = run(name, normals)
            check(f"name wake «{name}» no false positives on {len(normals)} normal phrases (~{len(normals)*4}s speech)", not fp, str([d['heard'] for d in fp]))
        dets = run("Пятница", ["normal3_D.wav", "pt_req_S.wav"])
        req = dets[0]["request_wav"] if dets else None
        check("same-breath request captured", bool(req), f"{len(req or b'')} bytes")
        if req:
            tr = brain.transcribe(req)
            check("request transcript", ("телеграм" in tr.lower() or "telegram" in tr.lower()) and "пятниц" not in tr.lower(), repr(tr))
        # live rename: same spotter, new name
        sp = NameSpotter(model, name_variants("Пятница"), 0.5); sp.variants = name_variants("Джарвис")
        x = stream([load(os.path.join(wdir, "name_dj_S.wav"))]); got = None
        for i in range(0, len(x), 1280):
            got = sp.feed(x[i:i + 1280]) or got
        check("rename applies live", bool(got))
    print("ALL OK" if ok else "SOME FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
