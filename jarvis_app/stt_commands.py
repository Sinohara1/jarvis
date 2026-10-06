"""Post-STT fixer for spoken *English* command templates (v1.5.x).

Small Vosk models mangle short English commands, and the Russian one can only spell them in Cyrillic:

    said : «Write Nehto on Telegram, hello I'm Jarvis»
    en   : «right next door on telegram fellow i'm jarvis»
    ru   : «райт нехто он телеграм хелоу айм джарвис»

A pure acoustic model can't know the contact «Nehto», so this module repairs the *structure* instead:
it only rewrites an utterance that matches a command template —

    <write|right|rite|райт…> [to] <contact> <on|in|он…> <Telegram>[,] <message>
    <write> <on> <Telegram> [to] <contact> <message>
    <play|плей…> <query> <on|in|он…> <Spotify|spot if i…>

— and inside it fixes the verb, the app name, a mangled «hello» at the start of the message, «I'm»
and the assistant's name. The contact is snapped to a known name (settings «voice_contacts») with a
phonetic fuzzy match that also works across scripts («нехто» / «next door» → «Nehto»).
Anything that doesn't match a template (ordinary Russian speech) is returned unchanged.
"""

from __future__ import annotations

import difflib
import re
from typing import Iterable

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ@_]+(?:['’][A-Za-z]+)?")

# ─── vocabulary (lowercase, «ё» → «е») ──────────────────────────────────────────

# Cyrillic spellings of English words the Russian model outputs → the English word. Only used
# when the whole utterance then matches a template, so Russian «он»/«ин» are never touched otherwise.
CYR_EN: dict[str, str] = {
    "райт": "write", "врайт": "write", "райд": "write", "райтс": "write", "уайт": "write", "врайд": "write",
    "плей": "play", "плэй": "play", "плеи": "play", "плейс": "play", "плэи": "play",
    "он": "on", "ин": "in", "ту": "to", "фром": "from",
    "хелло": "hello", "хеллоу": "hello", "хелоу": "hello", "хэллоу": "hello", "хэлло": "hello", "хэлоу": "hello",
    "хало": "hello", "халло": "hello", "хеллo": "hello", "хай": "hi",
    "айм": "I'm", "ам": "am", "ай": "I",
    "сенд": "send", "мессадж": "message", "месседж": "message", "меседж": "message", "мэсэдж": "message",
    "телл": "tell",
}
# Cyrillic words that are clearly English (used by the bilingual gate; «он», «ту» are too Russian).
CYR_EN_STRONG = frozenset({"райт", "врайт", "райд", "уайт", "плей", "плэй", "плеи", "хелло", "хеллоу", "хелоу",
                           "хэллоу", "хэлло", "айм", "сенд", "мессадж", "месседж", "меседж", "фром"})

# «write» as the small English model hears it.
TG_VERBS = frozenset({"write", "writes", "wright", "right", "rights", "rite", "ride", "white", "light", "bright",
                      "wrote", "ryte", "text", "message", "send", "tell", "dm"})
PREPS = frozenset({"on", "in", "at", "via", "over", "through", "into", "own", "an",
                   "на", "в", "во", "через"})
TG_APP_1 = frozenset({"telegram", "telegrams", "telegraph", "telegram's", "telegrams'", "telegrammes", "telegramme",
                      "tg", "телеграм", "телеграмм", "телеграме", "телеграмме", "телегу", "телеге", "тг"})
TG_APP_2 = frozenset({("tele", "gram"), ("tell", "gram"), ("tella", "gram"), ("teller", "gram"), ("telly", "gram"),
                      ("tele", "grams")})
TG_APP_3 = frozenset({("tell", "a", "gram"), ("tell", "the", "gram"), ("tell", "a", "graham")})

SP_VERBS = frozenset({"play", "plays", "played", "playing", "clay", "pray", "blay", "plate", "plague"})
SP_PREPS = frozenset({"on", "in", "at", "from", "via", "own", "an", "на", "в", "во", "через"})
SP_APP_1 = frozenset({"spotify", "spotty", "spotifi", "spotifies", "spotify's", "spotted", "spoti",
                      "спотифай", "спотифае", "спотифи", "спотик", "спотике", "споти", "спотт"})
SP_APP_2 = frozenset({("spot", "if"), ("spot", "fi"), ("spot", "ify"), ("spotty", "fi"), ("spotty", "fy"),
                      ("spot", "fly"), ("spotty", "fly"), ("spot", "fy"), ("spot", "five"), ("spot", "ifi")})
SP_APP_3 = frozenset({("spot", "if", "i"), ("spot", "if", "eye"), ("spot", "if", "y"), ("spot", "a", "fly"),
                      ("spot", "of", "i"), ("spot", "if", "a"), ("spot", "if", "by"), ("spot", "a", "fi")})

HELLO_MANGLED = frozenset({"hello", "fellow", "yellow", "hollow", "hallo", "halo", "hullo", "jello", "mellow",
                           "fellows", "hell", "hallow", "hellow"})
FILLERS = frozenset({"hey", "ok", "okay", "please", "so", "um", "uh", "эй", "хей", "окей", "ок", "слушай", "пожалуйста"})
# «right now on telegram …» is not a command: a mangled verb («right», «light») needs a contact that
# isn't a plain English function word.
REAL_TG_VERBS = frozenset({"write", "writes", "wrote", "text", "message", "send", "tell", "dm"})
NOT_CONTACT = frozenset({"now", "here", "there", "then", "away", "back", "it", "this", "that", "up", "down", "out",
                         "off", "the", "a", "all", "so", "something", "anything", "everything", "is", "was", "next"})
MSG_LEAD = frozenset({"that", "saying", "say", "says", "message", "text"})

# Latin spellings of «Jarvis» the English model produces; safe to fix anywhere (not real words).
JARVIS_LATIN = frozenset({"jarvis", "jarvice", "jarviss", "jarvis's", "jervis", "jarves", "jarvus", "jarvas",
                          "jarwis", "jarvys", "jarviz", "jarvise", "jervice", "javis", "jarvi"})
JARVIS_CYR = frozenset({"джарвис", "джервис", "джарвиз", "жарвис", "джарвисс"})
# Latin forms of known assistant names (namewake.KNOWN_LATIN is Latin → Cyrillic; we need the reverse too).
NAME_LATIN = {"джарвис": "Jarvis", "jarvis": "Jarvis", "айри": "Airi", "аири": "Airi", "airi": "Airi",
              "пятница": "Friday", "фрайдей": "Friday", "friday": "Friday", "алиса": "Alice", "alice": "Alice",
              "алекса": "Alexa", "alexa": "Alexa", "сири": "Siri", "siri": "Siri", "макс": "Max", "max": "Max",
              "джек": "Jack", "jack": "Jack"}
AIRI_VARIANTS = frozenset({"airi", "airy", "ari", "eri", "iri", "aeri", "ayri", "airie", "eerie", "айри", "аири",
                           "эйри", "ейри", "ари"})


# ─── small helpers ───────────────────────────────────────────────────────────

def _low(w: str) -> str:
    return (w or "").lower().replace("ё", "е").replace("’", "'")


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text or "")]


def _is_cyr(w: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", w or ""))


def latin_name(name: str) -> str:
    """Spelling of the assistant name inside an English sentence: «Джарвис» → «Jarvis», «Airi» → «Airi»."""
    n = (name or "").strip()
    if not n:
        return "Jarvis"
    k = _low(n)
    if k in NAME_LATIN:
        return NAME_LATIN[k]
    if not _is_cyr(n):
        return n
    from .lang import translit
    return translit(n)


def _name_forms(assistant_name: str) -> tuple[frozenset[str], str]:
    """(lowercase spoken forms that mean the configured name, canonical Latin spelling)."""
    canon = latin_name(assistant_name)
    forms: set[str] = {_low(canon), _low(assistant_name or "")}
    if canon == "Jarvis":
        forms |= JARVIS_LATIN | JARVIS_CYR
    elif canon == "Airi":
        forms |= AIRI_VARIANTS
    try:
        from .namewake import name_variants
        forms |= {v.replace(" ", "") for v in name_variants(assistant_name or canon)}
    except Exception:
        pass
    forms.discard("")
    return frozenset(forms), canon


# ─── phonetic fuzzy match for contact names ─────────────────────────────────

_PH = (("sch", "sh"), ("tch", "ch"), ("ck", "k"), ("x", "ks"), ("ph", "f"), ("kh", "h"), ("gh", "g"), ("q", "k"),
       ("w", "v"), ("y", "i"), ("ee", "i"), ("oo", "u"), ("ou", "u"), ("c", "k"), ("dzh", "j"), ("zh", "j"))


def phonetic_key(s: str) -> str:
    """Script-independent sound skeleton: «Nehto», «нехто», «Nekhto» → «nehto»; spaces dropped."""
    from .lang import translit
    t = translit(_low(s))
    t = re.sub(r"[^a-z]", "", t)
    for a, b in _PH:
        t = t.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", t)


def name_score(heard: str, name: str) -> float:
    """0..1 how well a heard span (possibly split into wrong English words) matches a contact name."""
    a, b = phonetic_key(heard), phonetic_key(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    best = difflib.SequenceMatcher(None, a, b).ratio()
    # The model often appends a junk syllable/word («next door» for «Nehto»): compare the prefix too.
    for extra in (0, 1, 2):
        pre = a[:len(b) + extra]
        if len(pre) < len(a):
            best = max(best, 0.92 * difflib.SequenceMatcher(None, pre, b).ratio())
    words = [w for w in re.split(r"\s+", _low(heard)) if w]
    if len(words) > 1:  # «nick toe» → also try the first word alone
        best = max(best, 0.9 * difflib.SequenceMatcher(None, phonetic_key(words[0]), b).ratio())
    if a[:1] == b[:1]:
        best = min(1.0, best + 0.04)
    return round(best, 3)


def match_name(heard: str, names: Iterable[str], min_score: float = 0.58, margin: float = 0.06) -> str | None:
    """Best known name for ``heard`` if it is clearly better than the runner-up, else None."""
    scored = sorted(((name_score(heard, n), n) for n in dict.fromkeys(n.strip() for n in names if n and n.strip())),
                    reverse=True)
    if not scored or scored[0][0] < min_score:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < margin and scored[0][1].lower() != scored[1][1].lower():
        return None
    return scored[0][1]


# ─── template parsing ────────────────────────────────────────────────────────

def _mapped(toks: list[tuple[str, int, int]], cyr: bool) -> list[str]:
    out = []
    for w, _, _ in toks:
        k = _low(w)
        out.append(CYR_EN.get(k, k) if cyr else k)
    return out


def _app_at(words: list[str], i: int, one, two, three) -> int:
    """Length (1..3) of an app name starting at words[i], else 0."""
    if i + 2 < len(words) and (words[i], words[i + 1], words[i + 2]) in three:
        return 3
    if i + 1 < len(words) and (words[i], words[i + 1]) in two:
        return 2
    if i < len(words) and words[i] in one:
        return 1
    return 0


def _skip_lead(words: list[str], name_forms: frozenset[str]) -> int:
    i = 0
    for _ in range(3):
        if i < len(words) and (words[i] in FILLERS or words[i] in name_forms):
            i += 1
    return i


def _parse_telegram(toks, words, name_forms, names) -> dict | None:
    i = _skip_lead(words, name_forms)
    if i >= len(words) or words[i] not in TG_VERBS:
        return None
    v = i
    i += 1
    if i + 1 < len(words) and words[i] == "a" and words[i + 1] == "message":
        i += 2
    if i < len(words) and words[i] in ("to", "ту"):
        i += 1
    # form A: verb [to] CONTACT prep TELEGRAM msg
    for j in range(i + 1, min(len(words), i + 6)):
        if words[j - 1] in PREPS or words[j - 1] in ("on", "in"):
            n = _app_at(words, j, TG_APP_1, TG_APP_2, TG_APP_3)
            if n and j - 1 > i:
                return {"verb": v, "contact": (i, j - 1), "msg": j + n}
        n = _app_at(words, j, TG_APP_1, TG_APP_2, TG_APP_3)
        if n and j > i and words[j - 1] not in PREPS and j - i <= 3 and words[i] not in PREPS:
            # «write mom telegram hi» (preposition swallowed)
            return {"verb": v, "contact": (i, j), "msg": j + n}
    # form B: verb prep TELEGRAM [to] CONTACT msg
    if i < len(words) and words[i] in PREPS:
        n = _app_at(words, i + 1, TG_APP_1, TG_APP_2, TG_APP_3)
        if n:
            k = i + 1 + n
            if k < len(words) and words[k] in ("to", "ту"):
                k += 1
            if k >= len(words):
                return None
            span = 1
            if names:  # longest prefix (1..3 words) that snaps to a known name
                best = (0.0, 1)
                for L in (1, 2, 3):
                    if k + L <= len(words):
                        heard = " ".join(t[0] for t in toks[k:k + L])
                        m = match_name(heard, names)
                        if m:
                            sc = name_score(heard, m)
                            if sc > best[0]:
                                best = (sc, L)
                span = best[1]
            return {"verb": v, "contact": (k, k + span), "msg": k + span}
    return None


def _parse_spotify(words) -> dict | None:
    i = _skip_lead(words, frozenset())
    if i >= len(words) or words[i] not in SP_VERBS:
        return None
    v = i
    for j in range(len(words) - 1, i + 1, -1):
        if words[j - 1] in SP_PREPS:
            n = _app_at(words, j, SP_APP_1, SP_APP_2, SP_APP_3)
            if n and j + n == len(words) and j - 1 > i + 0:
                return {"verb": v, "query": (i + 1, j - 1)}
    return None


def _fix_message(msg: str, name_forms: frozenset[str], canon: str, cyr: bool) -> str:
    toks = _tokens(msg)
    if not toks:
        return msg.strip(" ,.:;-—")
    out: list[str] = []
    pos = toks[0][1]
    first = True
    for idx, (w, a, b) in enumerate(toks):
        sep = msg[pos:a]
        k = _low(w)
        rep = w
        if cyr and k in CYR_EN:
            rep = CYR_EN[k]
            k = _low(rep)
        if first and k in MSG_LEAD and len(toks) > 1:
            pos = b
            continue  # «… on telegram saying hi» → «hi»
        if first and k in HELLO_MANGLED:
            rep = "hello"
        elif k in ("im", "i'm"):
            rep = "I'm"
        elif k in ("and", "um", "an", "am") and idx + 1 < len(toks) and idx >= 1 \
                and _low(toks[idx - 1][0]) in HELLO_MANGLED | {"hi", "hey", "хелоу", "хелло", "хеллоу"} \
                and (_low(toks[idx + 1][0]) in name_forms or _low(toks[idx + 1][0]) in JARVIS_LATIN):
            rep = "I'm"  # «hello and jarvis» = «hello, I'm Jarvis»
        elif k == "i":
            rep = "I"
        elif k in name_forms or k in JARVIS_LATIN:
            prev = _low(toks[idx - 1][0]) if idx else ""
            prev = CYR_EN.get(prev, prev) if cyr else prev
            # a bare «airy»/«ari» is only the name after «I'm / I am / it's / this is», Jarvis-forms always
            if k in JARVIS_LATIN or k in JARVIS_CYR or prev in ("i'm", "im", "am", "it's", "is") or k == _low(canon):
                rep = canon if (k not in JARVIS_LATIN and k not in JARVIS_CYR) else "Jarvis"
        out.append(("" if first else sep) + rep)
        first = False
        pos = b
    tail = msg[pos:]
    return ("".join(out) + tail).strip(" ,:;-—")


def parse_command(text: str, *, names: Iterable[str] = (), assistant_name: str = "Jarvis") -> dict | None:
    """Recognise a spoken English command template. Returns a dict with ``kind`` and the fixed
    pieces (telegram: contact, message; spotify: query), or None for anything else."""
    t = (text or "").strip()
    if not t or len(t) > 300:
        return None
    toks = _tokens(t)
    if len(toks) < 3:
        return None
    names = [n for n in names if n and str(n).strip()]
    name_forms, canon = _name_forms(assistant_name)
    for cyr in (False, True):
        if cyr and not any(_is_cyr(w) for w, _, _ in toks):
            break
        words = _mapped(toks, cyr)
        p = _parse_telegram(toks, words, name_forms, names)
        if p:
            a, b = p["contact"]
            raw = t[toks[a][1]:toks[b - 1][2]]
            if words[p["verb"]] not in REAL_TG_VERBS and all(w in NOT_CONTACT for w in words[a:b]) \
                    and not (names and match_name(raw, names)):
                p = None
        if p:
            contact = match_name(raw, names) if names else None
            contact = contact or raw.strip()
            msg_raw = t[toks[p["msg"]][1]:] if p["msg"] < len(toks) else ""
            message = _fix_message(msg_raw, name_forms, canon, cyr)
            return {"kind": "telegram", "contact": contact, "message": message, "heard_contact": raw.strip()}
        p = _parse_spotify(words)
        if p:
            a, b = p["query"]
            query = t[toks[a][1]:toks[b - 1][2]].strip(" ,.:;")
            if not query:
                return None
            return {"kind": "spotify", "query": query}
    return None


def render(cmd: dict) -> str:
    if cmd["kind"] == "telegram":
        s = f"Write {cmd['contact']} on Telegram"
        return f"{s}, {cmd['message']}" if cmd.get("message") else s
    if cmd["kind"] == "spotify":
        return f"Play {cmd['query']} on Spotify"
    return ""


def fix_commands(text: str, *, names: Iterable[str] = (), assistant_name: str = "Jarvis") -> str:
    """Rewrite a mangled English command into its canonical form; any other text is returned as is.

    Outside templates only the unmistakable Latin «Jarvis» misspellings (jarvice, jervis…) are fixed."""
    if not text or not str(text).strip():
        return text or ""
    cmd = parse_command(text, names=names, assistant_name=assistant_name)
    if cmd:
        out = render(cmd)
        end = text.rstrip()[-1:]
        return out + end if end in "?!" and cmd["kind"] != "telegram" else out
    return _fix_jarvis_latin(text)


_JARVIS_RE = re.compile(r"(?<![A-Za-z'])(?:" + "|".join(sorted((re.escape(w) for w in JARVIS_LATIN if w != "jarvi"),
                                                               key=len, reverse=True)) + r")(?![A-Za-z'])",
                        re.IGNORECASE)


def _fix_jarvis_latin(text: str) -> str:
    return _JARVIS_RE.sub("Jarvis", text)


# ─── bilingual gate (Russian model heard English) ───────────────────────────

_EN_CMD_RE = re.compile(r"\b(write|play|send|message|text|open|on telegram|on spotify|hello|i'm)\b", re.IGNORECASE)


def english_score(text: str) -> int:
    """How strongly a transcript looks like an *English* command: 0 = Russian speech, ≥2 = English.
    Works on Latin text and on the Russian model's Cyrillic spelling of English («райт … он телеграм»)."""
    toks = [_low(w) for w, _, _ in _tokens(text)]
    if not toks:
        return 0
    score = sum(1 for w in toks if w in CYR_EN_STRONG)
    if parse_command(text):
        score += 2
    if score and any(w in TG_APP_1 or w in SP_APP_1 for w in toks):
        score += 1
    if not any(_is_cyr(w) for w in toks) and _EN_CMD_RE.search(text or ""):
        score += 1
    return score


def wants_english_pass(text: str) -> bool:
    """Russian transcript that is clearly English speech («райт … он телеграм») → worth re-decoding
    with the English model, even if that model must be downloaded first."""
    return english_score(text) >= 2


# What the Russian small model really makes of English commands (measured with Piper voices):
#   «Write Nehto on Telegram, hello I'm Jarvis» → «крайне мере он тело краям лоу а мчались»
#   «Write Mama on Telegram, I am home»         → «варить мама он тело глэм а имхо мхом»
#   «Play Numb on Spotify»                      → «нам он спар фар» / «плэй нам он спад фай»
#   «Write Mama on Telegram, I am home»         → «вайт мама он тело двойным агентом» / «войти мама он глэм я»
# Only «он» + something that *sounds like* the app survives, so the weak gate looks for exactly that.
_APP_SOUNDS = (("telegram", 0.6), ("spotifai", 0.4), ("spotify", 0.4))
_GRAM_RE = re.compile(r"(?:gr|gl)[aei]+m$")   # «…грэм», «глэм», «грам» — the tail of «telegram»


def english_hint(text: str) -> int:
    """0 = Russian; 1 = might be English (re-decode only if the English model is already there);
    2 = clearly English (``wants_english_pass``)."""
    if wants_english_pass(text):
        return 2
    toks = [_low(w) for w, _, _ in _tokens(text)]
    if not any(_is_cyr(w) for w in toks):
        return 0
    # a mangled «…грэм / глэм» tail, but not a correctly heard Russian «в телеграм(м)»
    if any(_GRAM_RE.search(phonetic_key(w)) and not phonetic_key(w).startswith("tele") for w in toks[1:]):
        return 1
    for i in range(1, len(toks) - 1):
        if toks[i] not in ("он", "ин", "on", "in", "ан", "оне", "мон"):
            continue
        if toks[i + 1].startswith("тел"):  # «он тело …», «он телега»
            return 1
        for span in (1, 2):
            seg = " ".join(toks[i + 1:i + 1 + span])
            if len(toks) >= i + 1 + span and any(
                    difflib.SequenceMatcher(None, phonetic_key(seg), app).ratio() >= thr for app, thr in _APP_SOUNDS):
                return 1
    return 0


# ─── settings glue ───────────────────────────────────────────────────────────

def _contact_entries(settings: dict | None) -> list[str]:
    raw = (settings or {}).get("voice_contacts") or []
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]", raw)
    return [str(x).strip() for x in raw if str(x).strip()][:100]


def voice_names(settings: dict | None) -> list[str]:
    """Contact names the fixer may snap to (settings «voice_contacts»). An entry may carry a Telegram
    target after «=»: «Nehto=@nehto_tg» → the name is «Nehto»."""
    out = []
    for e in _contact_entries(settings):
        name = e.split("=", 1)[0].strip()
        if name:
            out.append(name)
    return out


def contact_aliases(settings: dict | None) -> dict[str, str]:
    """«Мама=@mama_tg», «Nehto = Nehto Ivanov» → {"мама": "@mama_tg", "nehto": "Nehto Ivanov"}."""
    out: dict[str, str] = {}
    for e in _contact_entries(settings):
        if "=" in e:
            name, target = (x.strip() for x in e.split("=", 1))
            if name and target:
                out[name.lower().replace("ё", "е")] = target
    return out


_RU_CASE_ENDINGS = ("ой", "ей", "ом", "ем", "ою", "ею", "ой", "ую", "юю", "у", "ю", "е", "ы", "и", "а", "я")


def _ru_stem(w: str) -> str:
    w = w.lower().replace("ё", "е")
    if not re.fullmatch(r"[а-яіїєґ]+", w):
        return w
    for end in _RU_CASE_ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[: -len(end)]
    return w


def resolve_contact(name: str, settings: dict | None) -> str:
    """Chat name for Telegram from what the model/voice produced: the «voice_contacts» alias target
    («Мама=@mama_tg» → «@mama_tg»), or the listed spelling of a declined form («Мамой» → «Мама» when
    «Мама» is listed — Telegram search is prefix based, «Мамой» finds nothing). Unknown names pass."""
    raw = (name or "").strip()
    if not raw:
        return raw
    aliases = contact_aliases(settings)
    names = voice_names(settings)
    key = raw.lower().replace("ё", "е")
    if key in aliases:
        return aliases[key]
    hit = None
    for n in names:
        if n.lower().replace("ё", "е") == key:
            hit = n
            break
    if hit is None and " " not in raw:
        st = _ru_stem(raw)
        for n in names:
            if " " not in n and _ru_stem(n) == st and len(st) >= 3:
                hit = n
                break
    if hit is None:
        return raw
    return aliases.get(hit.lower().replace("ё", "е"), hit)


def pick_bilingual(primary: str, english: str, en_conf: float = 0.0, *, names: Iterable[str] = (),
                   assistant_name: str = "Jarvis", min_conf: float = 0.45) -> str:
    """Choose between the Russian-model transcript and the English-model re-decode of the same audio.

    Both are *raw* transcripts; the result is the raw text to normalize. When both read as the same
    Telegram/Spotify command they are merged: the contact from whichever snapped to a known name (the
    Russian model's phonetic spelling «нехто» often matches better than English «next door»), the
    message / track from the English pass (real English words instead of Cyrillic guesses)."""
    names = list(names)
    english = (english or "").strip()
    if not english:
        return primary
    ru = parse_command(primary, names=names, assistant_name=assistant_name)
    en = parse_command(english, names=names, assistant_name=assistant_name)
    if en and ru and en["kind"] == ru["kind"] == "telegram":
        def snapped(c):
            return bool(names) and match_name(c["heard_contact"], names) is not None
        contact = en["contact"]
        if snapped(ru) and not snapped(en):
            contact = ru["contact"]
        return render({"kind": "telegram", "contact": contact, "message": en["message"] or ru["message"]})
    if en and ru and en["kind"] == ru["kind"] == "spotify":
        return render(en)
    if en and (not ru or en_conf >= 0.5):
        return english
    if ru:
        return primary
    if en_conf >= min_conf and (english_score(english) >= 2 or english_score(primary) >= 2):
        return english  # the Russian reading itself looked like English → trust the English model
    return primary
