"""Answer / speech languages (v1.4): prompt rules, language detection, localized spoken phrases.

The UI stays Russian. What changes with «Язык ответов»: the reply language (system prompt for chat,
voice, live mode, screen answers), the short phrases the app says by itself (tool acks, focus phases,
reminders, guard nudges) and the TTS voice, which always follows the language of the text it speaks.
"""

from __future__ import annotations

import random
import re

AUTO = "auto"
CODES = ("ru", "uk", "en", "de", "pl")

LANGS: dict[str, dict] = {
    "ru": {
        "label": "Русский", "short": "RU", "rule": "по-русски", "native_rule": "",
        "name_gen": "русский",
        "edge": {"м": "ru-RU-DmitryNeural", "ж": "ru-RU-SvetlanaNeural"},
        "vosk": ("vosk-model-small-ru-0.22", 46_236_750),
        "preview": "Привет{user}. Я {name}. Так звучит мой голос.",
        "ack": {"open": "Открываю.", "search": "Ищу.", "files": "Ищу файлы.", "focus": "Запускаю фокус.",
                "stop": "Останавливаю.", "remind": "Ставлю напоминание.", "cancel": "Отменяю.",
                "screen": "Смотрю на экран."},
        "t": {"break": "Блок закончен, отличная работа! Перерыв {mins}. Встань и разомнись.",
              "back": "Перерыв окончен. Возвращаемся к задаче: {task}.",
              "done": "Готово! Фокус-сессия «{task}» завершена. Сегодня в фокусе уже {today}.",
              "remind": "Напоминаю: {text}.", "task": "твоя задача", "little": "немного",
              "did": "Сделал, что смог.", "ok": "Готово."},
        "nudges": None,  # Russian uses the character presets (persona.py)
    },
    "uk": {
        "label": "Українська", "short": "UK", "rule": "по-украински", "native_rule": "Відповідай українською мовою.",
        "name_gen": "украинский",
        "edge": {"м": "uk-UA-OstapNeural", "ж": "uk-UA-PolinaNeural"},
        "vosk": ("vosk-model-small-uk-v3-nano", 77_622_640),
        "preview": "Привіт{user}. Я {name}. Так звучить мій голос.",
        "ack": {"open": "Відкриваю.", "search": "Шукаю.", "files": "Шукаю файли.", "focus": "Запускаю фокус.",
                "stop": "Зупиняю.", "remind": "Ставлю нагадування.", "cancel": "Скасовую.",
                "screen": "Дивлюся на екран."},
        "t": {"break": "Блок завершено, чудова робота! Перерва {mins}. Встань і розімнися.",
              "back": "Перерва закінчилася. Повертаємося до задачі: {task}.",
              "done": "Готово! Фокус-сесію «{task}» завершено. Сьогодні у фокусі вже {today}.",
              "remind": "Нагадую: {text}.", "task": "твоя задача", "little": "трохи",
              "did": "Зробив, що зміг.", "ok": "Готово."},
        "nudges": {
            1: ["Гей, ми ж робимо {task}. Давай повернемося.", "Здається, ти відволікся. Повертаємося до задачі: {task}.",
                "Невелике нагадування: зараз час для «{task}»."],
            2: ["{user}, це вже вдруге. Закривай і повертайся до задачі: {task}.",
                "Не залипай. Залишилося {left}, давай доробимо {task}.", "{task} саме себе не зробить."],
            3: ["Серйозно, досить гортати. Закрий це і повернися до задачі: {task}.",
                "Стоп. Ти обіцяв собі {task}. Закривай стрічку, у тебе вийде.",
                "Це вже третій дзвіночок. Прибери відволікання і зроби хоча б п'ять хвилин: {task}."]},
    },
    "en": {
        "label": "English", "short": "EN", "rule": "по-английски", "native_rule": "Always answer in English.",
        "name_gen": "английский",
        "edge": {"м": "en-US-GuyNeural", "ж": "en-US-JennyNeural"},
        "vosk": ("vosk-model-small-en-us-0.15", 41_205_931),
        "preview": "Hi{user}. I'm {name}. This is how my voice sounds.",
        "ack": {"open": "Opening.", "search": "Searching.", "files": "Looking for files.", "focus": "Starting focus.",
                "stop": "Stopping.", "remind": "Setting a reminder.", "cancel": "Cancelling.",
                "screen": "Looking at the screen."},
        "t": {"break": "Block done, great work! Break for {mins}. Stand up and stretch.",
              "back": "Break is over. Back to the task: {task}.",
              "done": "Done! Focus session \"{task}\" is finished. You've focused for {today} today.",
              "remind": "Reminder: {text}.", "task": "your task", "little": "a little",
              "did": "Done what I could.", "ok": "Done."},
        "nudges": {
            1: ["Hey, we're doing {task}. Let's get back to it.", "Looks like you got distracted. Back to {task}.",
                "Quick reminder: this is time for {task}."],
            2: ["{user}, that's the second time. Close it and get back to {task}.",
                "Don't get stuck. {left} left, let's finish {task}.", "{task} won't do itself."],
            3: ["Seriously, stop scrolling. Close it right now and get back to {task}.",
                "Stop. You promised yourself {task}. Close the feed, you can do it.",
                "That's the third call. Put the distraction away and do at least five minutes of {task}."]},
    },
    "de": {
        "label": "Deutsch", "short": "DE", "rule": "по-немецки", "native_rule": "Antworte immer auf Deutsch.",
        "name_gen": "немецкий",
        "edge": {"м": "de-DE-ConradNeural", "ж": "de-DE-KatjaNeural"},
        "vosk": ("vosk-model-small-de-0.15", 46_499_967),
        "preview": "Hallo{user}. Ich bin {name}. So klingt meine Stimme.",
        "ack": {"open": "Ich öffne es.", "search": "Ich suche.", "files": "Ich suche Dateien.",
                "focus": "Ich starte den Fokus.", "stop": "Ich stoppe.", "remind": "Ich stelle eine Erinnerung.",
                "cancel": "Ich breche ab.", "screen": "Ich schaue auf den Bildschirm."},
        "t": {"break": "Block geschafft, super Arbeit! {mins} Pause. Steh auf und streck dich.",
              "back": "Die Pause ist vorbei. Zurück zur Aufgabe: {task}.",
              "done": "Fertig! Die Fokus-Session „{task}“ ist beendet. Heute warst du schon {today} im Fokus.",
              "remind": "Erinnerung: {text}.", "task": "deine Aufgabe", "little": "ein bisschen",
              "did": "Erledigt, so gut es ging.", "ok": "Fertig."},
        "nudges": {
            1: ["Hey, wir machen doch {task}. Lass uns weitermachen.", "Sieht aus, als wärst du abgelenkt. Zurück zu {task}.",
                "Kleine Erinnerung: Jetzt ist Zeit für {task}."],
            2: ["{user}, das ist schon das zweite Mal. Mach das zu und zurück zu {task}.",
                "Nicht hängen bleiben. Noch {left}, lass uns {task} fertig machen.", "{task} erledigt sich nicht von selbst."],
            3: ["Im Ernst, hör auf zu scrollen. Mach das sofort zu und zurück zu {task}.",
                "Stopp. Du hast dir {task} vorgenommen. Schließ den Feed, du schaffst das.",
                "Das ist schon die dritte Erinnerung. Weg mit der Ablenkung und mindestens fünf Minuten {task}."]},
    },
    "pl": {
        "label": "Polski", "short": "PL", "rule": "по-польски", "native_rule": "Zawsze odpowiadaj po polsku.",
        "name_gen": "польский",
        "edge": {"м": "pl-PL-MarekNeural", "ж": "pl-PL-ZofiaNeural"},
        "vosk": ("vosk-model-small-pl-0.22", 52_979_372),
        "preview": "Cześć{user}. Jestem {name}. Tak brzmi mój głos.",
        "ack": {"open": "Otwieram.", "search": "Szukam.", "files": "Szukam plików.", "focus": "Włączam fokus.",
                "stop": "Zatrzymuję.", "remind": "Ustawiam przypomnienie.", "cancel": "Anuluję.",
                "screen": "Patrzę na ekran."},
        "t": {"break": "Blok skończony, świetna robota! Przerwa {mins}. Wstań i rozciągnij się.",
              "back": "Przerwa się skończyła. Wracamy do zadania: {task}.",
              "done": "Gotowe! Sesja fokusu „{task}” zakończona. Dziś w fokusie już {today}.",
              "remind": "Przypominam: {text}.", "task": "twoje zadanie", "little": "trochę",
              "did": "Zrobiłem, co mogłem.", "ok": "Gotowe."},
        "nudges": {
            1: ["Hej, przecież robimy {task}. Wracajmy.", "Chyba się rozproszyłeś. Wracamy do zadania: {task}.",
                "Małe przypomnienie: teraz czas na {task}."],
            2: ["{user}, to już drugi raz. Zamknij to i wróć do zadania: {task}.",
                "Nie zawieszaj się. Zostało {left}, dokończmy {task}.", "{task} samo się nie zrobi."],
            3: ["Serio, przestań scrollować. Zamknij to od razu i wróć do zadania: {task}.",
                "Stop. Obiecałeś sobie {task}. Zamknij feed, dasz radę.",
                "To już trzecie przypomnienie. Odłóż rozpraszacz i zrób chociaż pięć minut: {task}."]},
    },
}

TOOL_ACK_KIND = {"open_app": "open", "open_url": "open", "open_path": "open", "web_search": "search",
                 "find_files": "files", "start_focus": "focus", "stop_focus": "stop", "set_reminder": "remind",
                 "cancel_reminders": "cancel", "look_at_screen": "screen"}


def norm(code, allow_auto: bool = False, default: str = "ru") -> str:
    c = str(code or "").strip().lower()
    if allow_auto and c == AUTO:
        return AUTO
    return c if c in LANGS else default


def label(code: str) -> str:
    return "Как я спросил" if code == AUTO else LANGS.get(code, LANGS["ru"])["label"]


# ── detection (short texts: script + letters + a few stop words) ──
_CYR = re.compile(r"[а-яёіїєґ]", re.I)
_LAT = re.compile(r"[a-zäöüßąćęłńóśźż]", re.I)
_UK_ONLY = re.compile(r"[іїєґ]", re.I)
_RU_ONLY = re.compile(r"[ыэъё]", re.I)
_PL_CHARS = re.compile(r"[ąćęłńśźż]", re.I)
_DE_CHARS = re.compile(r"[äöüß]", re.I)
_STOP = {
    "en": {"the", "is", "are", "you", "what", "how", "and", "to", "of", "it", "please", "open", "my", "i", "a",
           "can", "with", "for", "this", "that", "why", "time", "today", "set", "minutes", "doing"},
    "de": {"der", "die", "das", "und", "ist", "ich", "nicht", "wie", "was", "mit", "bitte", "öffne", "mein",
           "ein", "eine", "warum", "zu", "es", "du", "sind", "heute", "für", "auf", "spät", "uhr", "minuten"},
    "pl": {"jest", "nie", "jak", "co", "się", "to", "czy", "proszę", "otwórz", "mój", "moje", "dlaczego",
           "ile", "jaka", "jaki", "teraz", "dzisiaj", "dla", "na", "w", "z", "i", "minut", "robię"},
    "uk": {"що", "як", "це", "я", "ти", "мені", "відкрий", "будь", "ласка", "чому", "котра", "година",
           "зараз", "сьогодні", "де", "хвилин", "роблю", "скільки", "привіт", "який", "яка", "ні", "так",
           "мене", "тебе", "дуже", "добре", "можна", "потрібно", "або", "але", "щоб", "коли", "він", "вона"},
    "ru": {"что", "как", "это", "я", "ты", "мне", "открой", "пожалуйста", "почему", "который", "час",
           "сейчас", "сегодня", "где", "минут", "делаю", "сколько", "времени", "привет", "какой", "какая", "нет",
           "меня", "тебя", "очень", "хорошо", "можно", "нужно", "или", "но", "чтобы", "когда", "он", "она"},
}
for _c, _d in LANGS.items():  # the app's own phrases («Otwieram.», «Öffne.») must be recognised too
    for _p in list(_d["ack"].values()) + list(_d["t"].values()):
        _STOP[_c] |= {w for w in re.findall(r"[a-zа-яёіїєґäöüßąćęłńóśźż']+", re.sub(r"\{\w+\}", "", _p.lower()))
                      if len(w) >= 5}


def detect(text: str, prefer: str | None = None) -> str:
    """Best guess among CODES. `prefer` wins whenever the text does not clearly contradict it."""
    t = (text or "").lower()
    cyr, lat = len(_CYR.findall(t)), len(_LAT.findall(t))
    prefer = prefer if prefer in LANGS else None
    if cyr == 0 and lat == 0:
        return prefer or "ru"
    words = set(re.findall(r"[a-zа-яёіїєґäöüßąćęłńóśźż']+", t))
    if cyr >= lat:
        if _UK_ONLY.search(t) and not _RU_ONLY.search(t):
            return "uk"
        if _RU_ONLY.search(t):
            return "ru"
        uk, ru = len(words & _STOP["uk"]), len(words & _STOP["ru"])
        if uk > ru:
            return "uk"
        if ru > uk:
            return "ru"
        return prefer if prefer in ("ru", "uk") else "ru"
    pl_c, de_c = len(_PL_CHARS.findall(t)), len(_DE_CHARS.findall(t))
    if pl_c and not de_c:
        return "pl"
    if de_c and not pl_c:
        return "de"
    score = {k: len(words & _STOP[k]) for k in ("en", "de", "pl")}
    best = max(score, key=lambda k: score[k])
    if prefer in ("en", "de", "pl") and score[prefer] >= score[best]:
        return prefer
    if score[best] > 0:
        return best
    return prefer if prefer in ("en", "de", "pl") else "en"


# ── which language to answer in ──
def answer_setting(settings: dict) -> str:
    return norm(settings.get("answer_lang"), allow_auto=True)


def speech_lang(settings: dict) -> str:
    return norm(settings.get("speech_lang"))


def reply_lang(settings: dict, user_lang: str | None = None) -> str:
    """Concrete language for a reply / proactive phrase: fixed setting, or the user's last language."""
    a = answer_setting(settings)
    if a != AUTO:
        return a
    return user_lang if user_lang in LANGS else speech_lang(settings)


def prompt_rule(settings: dict, user_lang: str | None = None, *, proactive: bool = False) -> str:
    """One line for a system prompt."""
    a = answer_setting(settings)
    if proactive:  # nobody asked anything: the fixed language, or the one he used last
        code = reply_lang(settings, user_lang)
        return (f"Говори {LANGS[code]['rule']}. {LANGS[code]['native_rule']}" + _name_rule(settings, code)).strip()
    if a == AUTO:
        fb = LANGS[speech_lang(settings)]["rule"]
        return ("Отвечай на том же языке, на котором написана последняя реплика пользователя (русский, украинский, "
                f"английский, немецкий, польский или другой). Если язык непонятен — отвечай {fb}.")
    L = LANGS[a]
    return (f"Всегда отвечай {L['rule']}, даже если пользователь пишет или говорит на другом языке "
            f"(но если он прямо попросил перевести или сказать что-то на другом языке — сделай это). "
            f"{L['native_rule']}").strip()


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
               ["a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g"]))


def translit(name: str) -> str:
    """«Вова» → «Vova» (names inside Latin-script replies; Piper voices can't read Cyrillic)."""
    out = []
    for ch in name or "":
        lo = ch.lower()
        if lo in _TR:
            t = _TR[lo]
            out.append(t.capitalize() if ch != lo else t)
        else:
            out.append(ch)
    return "".join(out)


def turn_rule(settings: dict, code: str) -> str:
    """Last line of the system prompt for one reply: the concrete language (models drift without it)."""
    code = norm(code)
    L = LANGS[code]
    if answer_setting(settings) == AUTO:
        line = (f"\nЯзык этого ответа: пользователь сейчас обратился {L['rule']} — отвечай {L['rule']} "
                f"(если его последняя реплика явно на другом языке — отвечай на её языке).")
    else:
        line = (f"\nЯзык этого ответа: {L['rule']} — даже если вопрос задан по-русски или на другом языке. "
                f"{L['native_rule']}")
    if code != "ru":
        line += " Весь ответ целиком на этом языке, не переходи на русский посреди фразы."
        if code == "uk":
            line += " Это украинский, не русский: украинская лексика и буквы і, ї, є, ґ."
    return line + _name_rule(settings, code) + "\n"


def _name_rule(settings: dict, code: str) -> str:
    user = str(settings.get("user_name") or "").strip()
    if user and code in ("en", "de", "pl") and re.search(r"[а-яёіїєґ]", user, re.I):
        return f" Имя пользователя пиши латиницей: {translit(user)}."
    return ""


# ── localized phrases ──
def ack(tool: str, code: str) -> str | None:
    k = TOOL_ACK_KIND.get(tool)
    return LANGS[norm(code)]["ack"][k] if k else None


def text(key: str, code: str, **kw) -> str:
    return LANGS[norm(code)]["t"][key].format(**kw)


def minutes(n: int, code: str) -> str:
    n = int(n)
    code = norm(code)
    if code == "ru":
        from .focus import minutes_phrase
        return minutes_phrase(n)
    if code in ("uk", "pl"):
        forms = ("хвилина", "хвилини", "хвилин") if code == "uk" else ("minuta", "minuty", "minut")
        if n % 10 == 1 and n % 100 != 11:
            w = forms[0] if code == "uk" or n == 1 else forms[2]
        elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            w = forms[1]
        else:
            w = forms[2]
        return f"{n} {w}"
    if code == "de":
        return f"{n} Minute" if n == 1 else f"{n} Minuten"
    return f"{n} minute" if n == 1 else f"{n} minutes"


def nudge(code: str, level: int, task: str, left: str, user: str) -> str | None:
    """Neutral guard nudge in a non-Russian language (Russian keeps the character presets)."""
    pool = LANGS[norm(code)]["nudges"]
    if not pool:
        return None
    lvl = max(1, min(3, int(level)))
    t = random.choice(pool[lvl])
    if not user:
        t = t.replace("{user}, ", "").replace("{user}", "")
        t = t[:1].upper() + t[1:]
    return t.format(task=task or LANGS[norm(code)]["t"]["task"], left=left or LANGS[norm(code)]["t"]["little"],
                    user=latin_for(code, user))


def preview_text(code: str, name: str, user: str = "") -> str:
    name, user = latin_for(code, name), latin_for(code, user)
    return LANGS[norm(code)]["preview"].format(name=name, user=(", " + user) if user else "")


def latin_for(code: str, s: str) -> str:
    """Transliterate a Cyrillic name for Latin-script languages (en/de/pl)."""
    return translit(s) if norm(code) in ("en", "de", "pl") else (s or "")


def edge_voice(settings: dict, code: str) -> str:
    """Microsoft Edge voice for a language; Russian keeps the user's choice, others match its gender."""
    code = norm(code)
    ru_voice = str(settings.get("voice") or "ru-RU-DmitryNeural")
    if code == "ru":
        return ru_voice
    g = "ж" if any(x in ru_voice for x in ("Svetlana", "Dariya")) else "м"
    return LANGS[code]["edge"][g]


def payload() -> list[dict]:
    return [{"code": k, "label": v["label"], "short": v["short"],
             "vosk_mb": round(v["vosk"][1] / 1e6)} for k, v in LANGS.items()]
