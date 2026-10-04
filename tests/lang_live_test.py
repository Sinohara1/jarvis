"""Answer-language test against the real provider (needs GEMINI_API_KEY in env; never prints it).
1) fixed «Язык ответов» = each language, Russian question → reply must be in that language;
2) «Как я спросил» → questions in each language, reply must match (history kept between turns).
Run: python tests/lang_live_test.py"""
import os, sys, time, webbrowser
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis_app.config import normalize_settings
from jarvis_app.brain import Brain
from jarvis_app.actions import Actions
from jarvis_app import lang as L

webbrowser.open = lambda u, *a, **k: True


class FakeCore:
    def __getattr__(self, name):
        return lambda *a, **k: {"ok": True}


settings = normalize_settings({"user_name": "Вова"})
brain = Brain(lambda: settings, Actions(FakeCore()), lambda: "Фокус-сессия не запущена.")
fails = 0


def run(q, want, user_lang):
    global fails
    t = time.time()
    try:
        r = brain.ask(q, lang=L.reply_lang(settings, user_lang), voice=True, on_text=lambda d: None, fast=True)
    except Exception as e:  # quota etc.
        r = f"<error {type(e).__name__}: {e}>"
    got = L.detect(r)
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} answer_lang={settings['answer_lang']:<4} want={want} got={got} "
          f"{time.time() - t:4.1f}s  {q!r} -> {r[:110]!r}", flush=True)
    time.sleep(6)  # free tier: requests per minute


for code in ("ru", "uk", "en", "de", "pl"):
    settings["answer_lang"] = code
    run("Почему небо голубое? Ответь одним коротким предложением.", code, "ru")

settings["answer_lang"] = "auto"
for code, q in [("en", "What is the capital of Australia? One short sentence."),
                ("de", "Wie viele Beine hat eine Spinne? Ein kurzer Satz."),
                ("uk", "Скільки днів у високосному році? Одним коротким реченням."),
                ("pl", "Jaka jest najdłuższa rzeka w Polsce? Jedno krótkie zdanie."),
                ("ru", "Сколько будет семь умножить на восемь? Одной фразой.")]:
    run(q, code, L.detect(q, prefer="ru"))
print("ALL PASS" if not fails else f"{fails} FAILED")
