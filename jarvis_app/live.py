"""Live mode (v1.2): every N seconds the assistant looks at the screen and decides
by itself whether to say or ask something right now.

Pure logic lives here (policy, prompt, decision parsing) so it is unit-testable;
JarvisCore drives it from its ticker. Screenshots never leave memory.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import deque
from datetime import datetime

from . import persona

# Talkativeness → model confidence threshold and multipliers for the gap / check interval.
TALK = {
    "rare": {"label": "Редко", "conf": 0.85, "gap": 2.0, "interval": 1.5},
    "some": {"label": "Иногда", "conf": 0.72, "gap": 1.0, "interval": 1.0},
    "often": {"label": "Часто", "conf": 0.6, "gap": 0.6, "interval": 0.75},
}
KINDS = ("hint", "question", "nudge", "praise")
SETTLE_SEC = 20.0        # quiet period after enabling / after the foreground window changes
MAX_SLOW = 8.0           # interval multiplier cap after repeated 429s
MEMORY = 8

LIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "activity": {"type": "string"},
        "reason": {"type": "string"},
        "speak": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(KINDS)},
        "confidence": {"type": "number"},
        "text": {"type": "string"},
    },
    "required": ["activity", "reason", "speak", "kind", "confidence", "text"],
    "propertyOrdering": ["activity", "reason", "speak", "kind", "confidence", "text"],
}


def talk_params(settings: dict, *, in_focus: bool = False) -> dict:
    t = TALK.get(settings.get("live_talk"), TALK["some"])
    interval = max(20.0, float(settings.get("live_interval_sec", 45)) * t["interval"])
    if in_focus:
        interval *= 2.0  # the focus guard already watches the screen; save quota
    gap = max(30.0, float(settings.get("live_min_gap_sec", 180)) * t["gap"])
    return {"interval": interval, "gap": gap, "conf": t["conf"]}


def _norm_text(s: str) -> str:
    return re.sub(r"[^\wа-яё]+", " ", (s or "").lower()).strip()


def similar(a: str, b: str) -> float:
    a, b = _norm_text(a), _norm_text(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


class LivePolicy:
    """When to look, and whether a model decision may actually be spoken.
    All times are monotonic seconds passed in by the caller (testable)."""

    def __init__(self) -> None:
        self.memory: deque = deque(maxlen=MEMORY)
        self.reset()

    def reset(self, now: float | None = None) -> None:
        self.enabled_at = now
        self.last_check: float | None = None
        self.last_remark: float | None = None
        self.last_remark_src = ""
        self.fg_key: tuple | None = None
        self.fg_changed_at = 0.0
        self.fail = 0
        self.slow = 1.0
        self.backoff_until = 0.0
        self.memory.clear()

    def note_foreground(self, key: tuple, now: float) -> bool:
        """Track foreground changes (title/process). Returns True if it changed."""
        if key != self.fg_key:
            first = self.fg_key is None
            self.fg_key = key
            if not first:
                self.fg_changed_at = now
            return not first
        return False

    def gap_left(self, now: float, settings: dict, *, in_focus: bool = False) -> float:
        if self.last_remark is None:
            return 0.0
        return max(0.0, talk_params(settings, in_focus=in_focus)["gap"] - (now - self.last_remark))

    def due(self, now: float, settings: dict, *, in_focus: bool = False) -> tuple[bool, str]:
        """Is it time to take a screenshot and ask the model? (reason when not)"""
        p = talk_params(settings, in_focus=in_focus)
        if now < self.backoff_until:
            return False, "backoff"
        if self.enabled_at is not None and now - self.enabled_at < SETTLE_SEC:
            return False, "settle"
        if self.fg_changed_at and now - self.fg_changed_at < SETTLE_SEC:
            return False, "change"
        if self.gap_left(now, settings, in_focus=in_focus) > 0:
            return False, "gap"   # it could not speak anyway — don't spend quota
        if self.last_check is not None and now - self.last_check < p["interval"] * self.slow:
            return False, "interval"
        return True, ""

    def accept(self, d: dict, now: float, settings: dict, *, in_focus: bool = False) -> tuple[bool, str]:
        """May this decision be spoken now?"""
        p = talk_params(settings, in_focus=in_focus)
        if not d.get("valid"):
            return False, "invalid"
        if not d.get("speak"):
            return False, "silent"
        if not d.get("text"):
            return False, "empty"
        if d.get("confidence", 0.0) < p["conf"]:
            return False, "unsure"
        if in_focus and d.get("kind") == "nudge":
            return False, "guard"  # focus guard owns distraction nudges
        if self.gap_left(now, settings, in_focus=in_focus) > 0:
            return False, "gap"
        if self.fg_changed_at and now - self.fg_changed_at < SETTLE_SEC:
            return False, "change"
        for m in self.memory:
            if m.get("said") and similar(m["said"], d["text"]) >= 0.72:
                return False, "repeat"
        return True, ""

    def on_check(self, now: float) -> None:
        self.last_check = now

    def on_remark(self, now: float, src: str = "live") -> None:
        """Any proactive speech (live remark or focus-guard nudge) restarts the gap."""
        self.last_remark = now
        self.last_remark_src = src

    def remember(self, now: float, activity: str, app: str, said: str = "", kind: str = "",
                 silent_reason: str = "") -> None:
        self.memory.append({"t": now, "activity": (activity or "")[:80], "app": (app or "")[:80],
                            "said": (said or "")[:300], "kind": kind, "why": (silent_reason or "")[:80]})

    def on_success(self) -> None:
        self.fail = 0
        self.slow = max(1.0, self.slow * 0.75)

    def on_rate_limit(self, now: float, interval: float, retry_after: float | None = None,
                      daily: bool = False) -> float:
        """Back off after HTTP 429 and slow the interval. Returns the pause in seconds."""
        self.fail += 1
        self.slow = min(MAX_SLOW, self.slow * 2.0)
        if daily:
            back = 3600.0
        else:
            back = min(900.0, max(retry_after or 0.0, interval * (2 ** self.fail)))
        self.backoff_until = now + back
        return back

    def on_error(self, now: float) -> float:
        self.fail += 1
        back = min(600.0, 30.0 * self.fail)
        self.backoff_until = now + back
        return back

    def memory_lines(self, now: float) -> list[str]:
        out = []
        for m in self.memory:
            ago = max(0, int((now - m["t"]) // 60))
            when = "только что" if ago == 0 else f"{ago} мин назад"
            what = m["activity"] or m["app"] or "?"
            if m["said"]:
                out.append(f"- {when}: {what}. Ты сказал ({m['kind']}): «{m['said']}»")
            else:
                out.append(f"- {when}: {what}. Ты промолчал.")
        return out


def build_prompt(settings: dict, *, task: str = "", focus_phase: str = "", title: str = "", process: str = "",
                 fullscreen: bool = False, since_remark: float | None = None, memory: list[str] | None = None,
                 now: datetime | None = None) -> tuple[str, str]:
    """(system, user prompt) for one live-mode decision."""
    name = persona.assistant_name(settings)
    user = str(settings.get("user_name") or "").strip()
    talk = TALK.get(settings.get("live_talk"), TALK["some"])["label"].lower()
    wishes = str(settings.get("persona") or "").strip()
    n = now or datetime.now()
    system = (
        f"Ты — {name}, голосовой ИИ-ассистент на компьютере пользователя{f' по имени {user}' if user else ''}. "
        f"Твой характер: {persona.character_text(settings)}\n"
        "Сейчас включён «живой режим»: ты время от времени смотришь на экран и сам решаешь, стоит ли "
        "прямо сейчас что-то сказать или спросить. По умолчанию ты МОЛЧИШЬ. Пользователь не просил тебя ни о чём — "
        "любая реплика его отвлекает, поэтому говори, только если она реально полезна именно сейчас.\n"
        "Когда стоит заговорить:\n"
        "- hint: видна конкретная проблема, которую ты можешь помочь решить: ошибка/исключение в коде или терминале, "
        "явная фактическая или грамматическая ошибка в уже написанном тексте, неправильный ответ в задаче. Подсказка должна быть конкретной.\n"
        "- question: по недавним наблюдениям видно, что он застрял (та же ошибка или тот же экран несколько проверок подряд) — "
        "коротко предложи помощь вопросом. По одному снимку не делай вывод, что он застрял.\n"
        "- nudge: явное залипание в развлечения (TikTok, Shorts, Reels, ленты соцсетей) — особенно если есть задача или поздно.\n"
        "- praise: он заметно что-то закончил (тесты прошли, работа сдана, документ дописан).\n"
        "Когда молчать (speak=false): обычная работа или учёба без видимых проблем; чтение, просмотр обучающего видео; "
        "игры, фильмы, музыка, общение в мессенджере, отдых без задачи; сомневаешься. "
        "Текст или код, который он прямо сейчас пишет, естественно не дописан — незаконченное предложение, абзац или функция "
        "это НЕ проблема, не комментируй их и не предлагай дописать. "
        "Если в недавних наблюдениях ты уже говорил о том же самом (та же ошибка, то же залипание), молчи — "
        "даже если проблема всё ещё на экране: он мог не успеть исправить, повторять не нужно. "
        "Не комментируй очевидное и не говори ради разговора.\n"
        "confidence — насколько ты уверен, что реплика сейчас уместна и полезна (0..1).\n"
        "text — 1–2 коротких разговорных предложения по-русски на «ты», в твоём характере, без markdown и эмодзи; "
        "для question — заканчивай вопросом. Если speak=false, text пустой.\n"
        "activity — что он делает (3–6 слов), reason — почему говоришь или молчишь (коротко).\n"
        f"Разговорчивость, которую выбрал пользователь: {talk}."
        + (f"\nПожелания пользователя: {wishes[:800]}" if wishes else "")
    )
    lines = [f"Сейчас {n.strftime('%H:%M')}."]
    if task and focus_phase in ("focus", "break"):
        ph = "идёт блок фокуса" if focus_phase == "focus" else "перерыв"
        lines.append(f"Фокус-сессия: задача «{task}», {ph}. Напоминания об отвлечениях делает страж фокуса — "
                     "сам не напоминай про отвлечения (не используй nudge).")
    else:
        lines.append("Фокус-сессия не запущена — задачи он не называл.")
    if title or process:
        lines.append(f"Активное окно: «{title[:150]}» ({process}).")
    if fullscreen:
        lines.append("Окно развёрнуто на весь экран — говори только если это явное отвлечение.")
    if since_remark is None:
        lines.append("Ты ещё ничего не говорил сам в этом режиме.")
    else:
        lines.append(f"Твоя последняя реплика была {int(since_remark // 60)} мин назад.")
    if memory:
        lines.append("Недавние наблюдения (не повторяйся):\n" + "\n".join(memory))
    lines.append("Посмотри на скриншот и реши. Верни JSON.")
    return system, "\n".join(lines)


def parse_decision(raw: str) -> dict:
    data: object = {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except ValueError:
                data = {}
    if not isinstance(data, dict):
        data = {}
    sp = data.get("speak")
    if isinstance(sp, str):
        sp = sp.strip().lower() in ("true", "yes", "1", "да")
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    if conf > 1.0:
        conf /= 100.0
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in KINDS:
        kind = "hint"
    text = re.sub(r"[*_`#>]+", "", str(data.get("text") or "")).strip()[:400]
    return {"speak": bool(sp), "kind": kind, "text": text, "confidence": max(0.0, min(1.0, conf)),
            "reason": str(data.get("reason") or "")[:200], "activity": str(data.get("activity") or "")[:80],
            "valid": sp is not None}


# ── local safety net for on/off commands ──
# Function calling is the main path; this only catches clear commands the
# model answered in words without actually calling set_live_mode.
_LIVE_OFF = [
    r"\b(выключи|отключи|останови|выруби|убери|деактивируй|отмени)\w*\b.{0,30}\bжив\w* режим",
    r"\bжив\w* режим\w*.{0,12}\b(выкл|откл|стоп|off)",
    r"\b(хватит|перестань|прекрати|не надо|не нужно|не мешай|тихо|помолчи|замолчи)\b.{0,40}\b(подсказ|комментир|следить|советов|советы|живой)",
    r"\b(не|больше не)\s+(следи|подсказывай|комментируй)\b",
]
_LIVE_ON = [
    r"\b(включи|запусти|активируй|вруби|верни)\w*\b.{0,30}\bжив\w* режим",
    r"\bжив\w* режим\w*.{0,12}\b(вкл|on)\b",
    r"\bследи\b.{0,30}\bподсказывай\b",
    r"^\s*(подсказывай|следи за (мной|экраном))\b",
]


def live_intent(text: str):
    """True = turn live mode on, False = off, None = not a clear live-mode command."""
    t = (text or "").lower().replace("ё", "е")
    if len(t) > 120:
        return None
    if any(re.search(p, t) for p in _LIVE_OFF):
        return False
    if any(re.search(p, t) for p in _LIVE_ON):
        return True
    return None
