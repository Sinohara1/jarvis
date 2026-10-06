"""Настраиваемая память компаньона: факты о пользователе, предпочтения, люди, проекты.

Хранится локально в %LOCALAPPDATA%\\Jarvis\\memory.json (вне Windows — рядом с settings.json).
Встраивается в мозг тремя путями:
  1. в системный промпт попадает короткий блок самых подходящих фактов (memory_inject штук, ≤ ~700 символов);
  2. модели доступны инструменты remember_fact / forget_fact / recall_memory;
  3. «Запомни, что …» сохраняется сразу, без модели (memory_explicit), а при memory_auto_extract
     модель изредка сама выбирает устойчивые факты из последних реплик пользователя.

Секреты (пароли, ключи, токены, номера карт, IBAN…) не сохраняются. Тексты фактов не пишутся в лог —
только id, категория и длина.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
import time
import uuid

from .config import MEMORY_PATH, ensure_dir

log = logging.getLogger("jarvis")

CATEGORIES: dict[str, str] = {
    "user": "О тебе",
    "preference": "Предпочтения",
    "people": "Люди",
    "project": "Проекты и дела",
    "other": "Разное",
}
DEFAULT_CATEGORY = "other"
MAX_FACT_LEN = 300
PROMPT_BUDGET = 700           # символов на блок памяти в системном промпте
MAX_FACTS_LIMIT = (10, 500)
INJECT_LIMIT = (0, 30)
EXTRACT_EVERY = 4             # авто-извлечение: раз в N реплик пользователя

# ─── secrets guard ───────────────────────────────────────────────────────────

_SECRET_WORDS = re.compile(
    r"(парол|password|passwd|\bpwd\b|пин[- ]?код|\bpin\b|\bcvv\b|\bcvc\b|токен|token|api[- _]?key|апи[- ]?ключ|"
    r"ключ(?:и)? (?:от )?api|секретн\w* (?:ключ|фраз|слов|код)|secret|seed[- ]?phrase|сид[- ]?фраз|мнемоническ|"
    r"приватн\w* ключ|private key|2fa|двухфактор|код из смс|код подтвержд|одноразов\w* код|"
    r"номер (?:банковской )?карт|card number|\biban\b|логин и пароль)",
    re.IGNORECASE)
_CARD_RE = re.compile(r"(?:\d[ -]?){13,19}")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b")
_KEYLIKE_RE = re.compile(r"(?=[A-Za-z0-9_\-]{24,})(?=\S*\d)(?=\S*[A-Za-z])[A-Za-z0-9_\-]{24,}")
_KNOWN_KEY_RE = re.compile(r"\b(?:sk-gw-[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_\-]{20,}|xai-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                           r"\d{6,}:[A-Za-z0-9_\-]{25,})")


def looks_secret(text: str) -> bool:
    """Грубая, но безопасная проверка: лучше не запомнить лишнее, чем сохранить пароль."""
    t = str(text or "")
    if not t.strip():
        return False
    if _SECRET_WORDS.search(t) or _KNOWN_KEY_RE.search(t) or _KEYLIKE_RE.search(t) or _IBAN_RE.search(t):
        return True
    for m in _CARD_RE.finditer(t):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ─── text helpers ────────────────────────────────────────────────────────────

_STOP = set("""
что это как мне меня мой моя моё мое мои я ты вы он она они мы у в во на и а но или же ли то так тоже не да нет
его её ее их там тут где когда кто чем чём для по про из за от до к ко с со о об при над под очень уже ещё еще
бы был была были быть есть будет просто всё все весь вся надо нужно можно этот эта эти тот та те свой своя
the a an and or of to in on at is are was were be my me i you it that this with for about do does
""".split())
_WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


def _norm_text(s: str) -> str:
    s = str(s or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def _stems(s: str) -> set[str]:
    out = set()
    for w in _WORD_RE.findall(str(s or "").lower().replace("ё", "е")):
        if len(w) < 3 or w in _STOP:
            continue
        out.add(w[:5] if len(w) > 5 else w)
    return out


def clean_fact(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip().strip("«»\"' ")
    t = re.sub(r"[.!…\s]+$", "", t)
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t[:MAX_FACT_LEN]


def norm_category(cat: str | None) -> str:
    c = str(cat or "").strip().lower()
    aliases = {"me": "user", "about": "user", "о тебе": "user", "обо мне": "user", "пользователь": "user",
               "prefs": "preference", "preferences": "preference", "предпочтения": "preference", "вкусы": "preference",
               "person": "people", "люди": "people", "человек": "people", "контакты": "people",
               "projects": "project", "проекты": "project", "проект": "project", "дела": "project", "task": "project",
               "разное": "other", "misc": "other"}
    c = aliases.get(c, c)
    return c if c in CATEGORIES else DEFAULT_CATEGORY


# «Запомни, что …» / «Джарвис, запомни: …» / «remember that …»
_EXPLICIT_RE = re.compile(
    r"^\s*(?:(?:эй|слушай|hey)[,\s]+)?(?:[\w\-]{2,30}[,\s]+)?(?:а\s+|и\s+|ну\s+)?(?:пожалуйста[,\s]+)?"
    r"(?:запомни(?:те)?|запиши\s+(?:себе\s+)?в\s+память|сохрани\s+в\s+памят[иь]|remember)\b"
    r"(?:\s*,?\s*пожалуйста)?\s*(?:[,:—-]\s*)?(?:(?:что|that|чтобы|:)\s*)?(?P<fact>.+?)\s*$",
    re.IGNORECASE | re.DOTALL)
_VAGUE = re.compile(r"^(это|это всё|это все|вот это|его|её|ее|их|то|that|this|it)$", re.IGNORECASE)


def explicit_fact(text: str) -> str:
    """Текст факта из явной просьбы «запомни, что …», иначе ''."""
    m = _EXPLICIT_RE.match(str(text or ""))
    if not m:
        return ""
    fact = clean_fact(m.group("fact"))
    if len(fact) < 6 or len(fact.split()) < 2 or _VAGUE.match(_norm_text(fact)):
        return ""
    return fact


def guess_category(text: str) -> str:
    t = _norm_text(text)
    if re.search(r"\b(люблю|нравит|не люблю|ненавижу|предпочитаю|обожаю|любимы|любимая|любимое|терпеть не могу|"
                 r"prefer|like|love|hate|favorite|favourite)", t):
        return "preference"
    if re.search(r"\b(мам|пап|брат|сестр|друг|подруг|девушк|парен|жена|муж|сын|доч|бабушк|дедушк|коллег|начальник|"
                 r"учитель|сосед|зовут|день рождения|mom|dad|brother|sister|friend|wife|husband)", t):
        return "people"
    if re.search(r"\b(проект|работаю над|делаю|пишу|курсов|диплом|экзамен|егэ|огэ|сессия|дедлайн|готовлюсь|учу\b|"
                 r"project|deadline|exam)", t):
        return "project"
    if re.search(r"\b(я |мне |меня |мой |моя |у меня|живу|работаю|учусь|родился|i am|i'm|my )", t + " "):
        return "user"
    return DEFAULT_CATEGORY


# ─── settings helpers ────────────────────────────────────────────────────────

def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def memory_settings(settings: dict | None) -> dict:
    s = settings or {}
    return {
        "enabled": bool(s.get("memory_enabled", True)),
        "max_facts": _clamp(s.get("memory_max_facts"), *MAX_FACTS_LIMIT, 100),
        "inject": _clamp(s.get("memory_inject"), *INJECT_LIMIT, 8),
        "explicit": bool(s.get("memory_explicit", True)),
        "auto_extract": bool(s.get("memory_auto_extract", False)),
    }


# ─── tool schemas ────────────────────────────────────────────────────────────

MEMORY_TOOLS: list[dict] = [
    {
        "name": "remember_fact",
        "description": "Запомнить надолго факт о пользователе: кто он, что любит, близкие люди, текущие проекты и планы. "
                       "Вызывай на «запомни, что …», а также когда пользователь сам сообщает важное устойчивое о себе "
                       "(«я учусь в 10 классе», «мою сестру зовут Аня»). Один короткий факт на вызов, своими словами от лица "
                       "пользователя или в третьем лице. Никогда не запоминай пароли, коды, ключи, номера карт и другие секреты.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Короткий факт, например «Учится в 10 классе, готовится к ЕГЭ по физике»"},
            "category": {"type": "string", "enum": list(CATEGORIES),
                         "description": "user — о нём самом; preference — вкусы и предпочтения; people — люди; "
                                        "project — проекты и дела; other — разное"}},
            "required": ["text"]},
    },
    {
        "name": "forget_fact",
        "description": "Забыть факт из долгой памяти: «забудь, что …», «это уже неправда». "
                       "query — о чём факт (несколько слов) или его id из recall_memory.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Ключевые слова факта или id"}},
            "required": ["query"]},
    },
    {
        "name": "recall_memory",
        "description": "Посмотреть, что ты помнишь о пользователе: «что ты обо мне знаешь?», «что я говорил про …?». "
                       "query — тема (пусто — всё самое важное).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Тема поиска; пусто — общий список"}},
            "required": []},
    },
]
MEMORY_TOOL_NAMES = frozenset(t["name"] for t in MEMORY_TOOLS)


# ─── store ───────────────────────────────────────────────────────────────────

class MemoryStore:
    """Thread-safe JSON store. Every method is cheap (dozens..hundreds of facts)."""

    def __init__(self, path: str = MEMORY_PATH, get_settings=None, on_change=None) -> None:
        self.path = path
        self.get_settings = get_settings or (lambda: {})
        self.on_change = on_change
        self._lock = threading.RLock()
        self._facts: list[dict] = []
        self._mtime: float | None = None
        self._loaded = False

    # persistence
    def _load(self) -> None:
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            mt = None
        if self._loaded and mt == self._mtime:
            return
        facts: list[dict] = []
        if mt is not None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("facts") if isinstance(data, dict) else data
                for x in raw if isinstance(raw, list) else []:
                    if isinstance(x, dict) and str(x.get("text") or "").strip():
                        facts.append(self._coerce(x))
            except (OSError, ValueError) as e:
                # keep the broken file for the user (hand edit gone wrong) instead of overwriting it on the next save
                bad = self.path + ".bad"
                try:
                    os.replace(self.path, bad)
                    mt = None
                except OSError:
                    bad = ""
                log.warning("memory: не удалось прочитать memory.json (%s) — начинаю с пустой памяти%s",
                            type(e).__name__, ", копия: memory.json.bad" if bad else "")
        self._facts = facts
        self._mtime = mt
        self._loaded = True

    @staticmethod
    def _coerce(x: dict) -> dict:
        now = time.time()
        return {
            "id": str(x.get("id") or uuid.uuid4().hex[:8])[:16],
            "text": clean_fact(x.get("text")),
            "category": norm_category(x.get("category")),
            "source": str(x.get("source") or "user")[:12],
            "pinned": bool(x.get("pinned")),
            "created": float(x.get("created") or now),
            "updated": float(x.get("updated") or x.get("created") or now),
        }

    def _save(self) -> None:
        ensure_dir(os.path.dirname(self.path))
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "facts": self._facts}, f, ensure_ascii=False, indent=1)
                f.write("\n")
            os.replace(tmp, self.path)
            self._mtime = os.path.getmtime(self.path)
        except OSError as e:
            log.warning("memory: сохранение не удалось (%s)", type(e).__name__)

    def _changed(self, what: str) -> None:
        if self.on_change:
            try:
                self.on_change(what, len(self._facts))
            except Exception:
                log.debug("memory on_change failed", exc_info=True)

    # queries
    def settings(self) -> dict:
        try:
            return memory_settings(self.get_settings())
        except Exception:
            return memory_settings({})

    @property
    def enabled(self) -> bool:
        return self.settings()["enabled"]

    def all(self) -> list[dict]:
        with self._lock:
            self._load()
            return [dict(f) for f in self._facts]

    def count(self) -> int:
        with self._lock:
            self._load()
            return len(self._facts)

    def _find_similar(self, text: str) -> dict | None:
        """Same fact re-stated / refined («Мне 15 лет» → «Мне 16 лет», «Сестру зовут Аня» → «… Аня Петрова»):
        the content words (numbers aside) are equal or one set contains the other, and the strings are close.
        «Проект сайта» vs «Проект бота» stay two facts."""
        n = _norm_text(text)
        words = {w for w in _stems(text) if not w.isdigit()}
        best, best_r = None, 0.0
        for f in self._facts:
            fn = _norm_text(f["text"])
            if fn == n:
                return f
            fw = {w for w in _stems(f["text"]) if not w.isdigit()}
            if not words or not fw or not (words == fw or words < fw or fw < words):
                continue
            r = difflib.SequenceMatcher(None, fn, n).ratio()
            if r > best_r:
                best, best_r = f, r
        return best if best_r >= 0.8 else None

    # mutations
    def add(self, text: str, category: str | None = None, *, source: str = "user", pinned: bool = False) -> dict:
        fact = clean_fact(text)
        if len(fact) < 3:
            return {"ok": False, "error": "пустой факт"}
        if looks_secret(fact):
            log.info("memory: отказ — похоже на секрет (len=%d)", len(fact))
            return {"ok": False, "error": "это похоже на пароль, ключ или другие секретные данные — такое я не запоминаю"}
        cat = norm_category(category) if category else guess_category(fact)
        with self._lock:
            self._load()
            now = time.time()
            same = self._find_similar(fact)
            if same is not None:
                same["text"] = fact
                if category:
                    same["category"] = cat
                same["updated"] = now
                same["pinned"] = same["pinned"] or bool(pinned)
                self._save()
                log.info("memory: обновлён факт %s (%s, len=%d, %s)", same["id"], same["category"], len(fact), source)
                self._changed("update")
                return {"ok": True, "id": same["id"], "updated": True, "category": same["category"]}
            item = {"id": uuid.uuid4().hex[:8], "text": fact, "category": cat, "source": source[:12],
                    "pinned": bool(pinned), "created": now, "updated": now}
            self._facts.append(item)
            evicted = self._evict()
            self._save()
        log.info("memory: новый факт %s (%s, len=%d, %s)%s", item["id"], cat, len(fact), source,
                 f", вытеснено {evicted}" if evicted else "")
        self._changed("add")
        out = {"ok": True, "id": item["id"], "category": cat}
        if evicted:
            out["evicted"] = evicted
        return out

    def _evict(self) -> int:
        limit = self.settings()["max_facts"]
        n = 0
        while len(self._facts) > limit:
            pool = [f for f in self._facts if not f["pinned"]] or self._facts
            oldest = min(pool, key=lambda f: f["updated"])
            self._facts.remove(oldest)
            n += 1
        return n

    def enforce_limit(self) -> int:
        with self._lock:
            self._load()
            n = self._evict()
            if n:
                self._save()
        if n:
            self._changed("evict")
        return n

    def update(self, fact_id: str, text: str | None = None, category: str | None = None,
               pinned: bool | None = None) -> dict:
        with self._lock:
            self._load()
            f = next((x for x in self._facts if x["id"] == str(fact_id)), None)
            if f is None:
                return {"ok": False, "error": "факт не найден"}
            if text is not None:
                t = clean_fact(text)
                if len(t) < 3:
                    return {"ok": False, "error": "пустой факт"}
                if looks_secret(t):
                    return {"ok": False, "error": "это похоже на секретные данные — такое я не запоминаю"}
                f["text"] = t
            if category is not None:
                f["category"] = norm_category(category)
            if pinned is not None:
                f["pinned"] = bool(pinned)
            f["updated"] = time.time()
            self._save()
        self._changed("update")
        return {"ok": True, "id": f["id"]}

    def delete(self, fact_id: str) -> dict:
        with self._lock:
            self._load()
            before = len(self._facts)
            self._facts = [f for f in self._facts if f["id"] != str(fact_id)]
            if len(self._facts) == before:
                return {"ok": False, "error": "факт не найден"}
            self._save()
        log.info("memory: удалён факт %s", fact_id)
        self._changed("delete")
        return {"ok": True}

    def forget(self, query: str) -> dict:
        """Delete by id or by best keyword match (needs a clear winner)."""
        q = str(query or "").strip()
        if not q:
            return {"ok": False, "error": "не понял, что забыть"}
        with self._lock:
            self._load()
            if any(f["id"] == q for f in self._facts):
                fid = q
            else:
                qs = _stems(q)
                hits = sorted(((len(qs & _stems(f["text"])), f) for f in self._facts), key=lambda x: -x[0])
                hits = [(n, f) for n, f in hits if n > 0]
                if not hits:
                    return {"ok": False, "error": "такого факта в памяти нет", "facts": self._brief(self._facts[-8:])}
                if len(hits) > 1 and hits[0][0] == hits[1][0]:
                    return {"ok": False, "error": "подходит несколько фактов — уточни, какой забыть",
                            "candidates": self._brief([f for n, f in hits if n == hits[0][0]][:5])}
                fid = hits[0][1]["id"]
            text = next(f["text"] for f in self._facts if f["id"] == fid)
        r = self.delete(fid)
        if r.get("ok"):
            r["forgotten"] = text
        return r

    def clear(self) -> dict:
        with self._lock:
            self._load()
            n = len(self._facts)
            self._facts = []
            self._save()
        log.info("memory: очищено (%d фактов)", n)
        self._changed("clear")
        return {"ok": True, "removed": n}

    # relevance
    def _rank(self, query: str, *, only_matching: bool = False) -> list[tuple[float, dict]]:
        qs = _stems(query)
        now = time.time()
        out = []
        for f in self._facts:
            overlap = len(qs & _stems(f["text"] + " " + CATEGORIES.get(f["category"], "")))
            if only_matching and overlap == 0:
                continue
            score = overlap * 3.0
            if f["pinned"]:
                score += 100.0
            score += {"user": 1.0, "preference": 0.6, "people": 0.4, "project": 0.5}.get(f["category"], 0.0)
            age_days = max(0.0, (now - f["updated"]) / 86400.0)
            score += 1.0 / (1.0 + age_days / 14.0)  # fresher facts slightly first
            out.append((score, f))
        out.sort(key=lambda x: (-x[0], -x[1]["updated"]))
        return out

    def relevant(self, query: str = "", limit: int | None = None) -> list[dict]:
        limit = self.settings()["inject"] if limit is None else limit
        if limit <= 0:
            return []
        with self._lock:
            self._load()
            return [dict(f) for _s, f in self._rank(query)[:limit]]

    @staticmethod
    def _brief(facts: list[dict]) -> list[dict]:
        return [{"id": f["id"], "text": f["text"], "category": f["category"]} for f in facts]

    def recall(self, query: str = "", limit: int = 12) -> dict:
        with self._lock:
            self._load()
            if not self._facts:
                return {"ok": True, "count": 0, "facts": [], "note": "память пока пустая"}
            ranked = self._rank(query, only_matching=bool(str(query or "").strip()))
            if not ranked and query:
                ranked = self._rank("")
                note = "по этой теме ничего, вот что помню в целом"
            else:
                note = ""
            res = {"ok": True, "count": len(self._facts), "facts": self._brief([f for _s, f in ranked[:limit]])}
            if note:
                res["note"] = note
            return res

    def prompt_block(self, query: str = "") -> str:
        """Short block for the system prompt ('' when memory is off / empty)."""
        st = self.settings()
        if not st["enabled"] or st["inject"] <= 0:
            return ""
        facts = self.relevant(query, st["inject"])
        if not facts:
            return ""
        lines, used = [], 0
        for f in facts:
            line = f"- {f['text']}"
            if used + len(line) > PROMPT_BUDGET and lines:
                break
            lines.append(line)
            used += len(line) + 1
        return ("\nЧто ты помнишь о пользователе (долгая память; используй к месту и ненавязчиво, не пересказывай "
                "без повода; если факт устарел — forget_fact, новый важный факт — remember_fact):\n"
                + "\n".join(lines) + "\n")

    # UI
    def payload(self) -> dict:
        st = self.settings()
        facts = sorted(self.all(), key=lambda f: (not f["pinned"], -f["updated"]))
        return {"ok": True, "facts": facts, "count": len(facts), "max_facts": st["max_facts"],
                "categories": [{"key": k, "label": v} for k, v in CATEGORIES.items()],
                "path": self.path, "settings": st}


# ─── auto-extraction (optional, uses the model) ──────────────────────────────

EXTRACT_SYSTEM = (
    "Ты помогаешь ассистенту вести долгую память о пользователе. Тебе дают последние реплики пользователя и уже "
    "известные факты. Выбери максимум 3 НОВЫХ устойчивых факта, которые пригодятся через недели: кто он, учёба/работа, "
    "вкусы и предпочтения, близкие люди (имена), текущие проекты и цели. Не бери разовые просьбы («открой ютуб»), "
    "настроение на минуту, команды, догадки и то, что уже известно. Никогда не бери пароли, коды, ключи, номера карт, "
    "адреса и другие секреты. Каждый факт — короткая фраза по-русски от лица пользователя или в третьем лице. "
    "Ответь только JSON: {\"facts\": [{\"text\": \"...\", \"category\": \"user|preference|people|project|other\"}]} "
    "или {\"facts\": []}, если нечего запомнить."
)


def parse_extracted(raw: str) -> list[dict]:
    data = None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", str(raw or ""), re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except ValueError:
                data = None
    items = data.get("facts") if isinstance(data, dict) else data if isinstance(data, list) else None
    out = []
    for x in items if isinstance(items, list) else []:
        if isinstance(x, str):
            x = {"text": x}
        if not isinstance(x, dict):
            continue
        t = clean_fact(x.get("text"))
        if len(t) >= 6 and not looks_secret(t):
            out.append({"text": t, "category": norm_category(x.get("category"))})
    return out[:3]


def extract_prompt(user_lines: list[str], known: list[dict]) -> str:
    k = "\n".join(f"- {f['text']}" for f in known[:40]) or "(пока ничего)"
    u = "\n".join(f"- {t[:400]}" for t in user_lines if t.strip())
    return f"Уже известно:\n{k}\n\nПоследние реплики пользователя:\n{u}\n\nНовые факты (JSON):"
