"""Post-STT normalizer: fix Russian Vosk mangling of English app/brand names.

Voice transcripts go through ``normalize_transcript`` before brain/tools, so
«открой спотт» becomes «открой Spotify» and open_app routing matches the Latin name.
Extend ``BRANDS`` (canonical → aliases) to teach new names; no other call sites needed.
Spoken *English* command templates («right next door on telegram fellow i'm jarvis») are repaired
by ``stt_commands`` from here as well (``normalize_brands`` is the brand-only pass).
"""

from __future__ import annotations

import re

# Canonical Start-menu / open_app name → Russian spellings + typical Vosk mangled forms.
# Keep the Latin self-alias so mixed «открой Spotify» stays stable.
BRANDS: dict[str, tuple[str, ...]] = {
    "Spotify": (
        "spotify", "спотифай", "спотифи", "спотифаи", "спотифй", "споти", "спотт", "спотти",
        "спотфай", "спотифей", "spoti", "spot",
    ),
    "Telegram": (
        "telegram", "телеграм", "телеграмм", "телега", "тг", "телеграмме", "телеграме",
    ),
    "Discord": (
        "discord", "дискорд", "дискор", "дискордт", "дискордс",
    ),
    "Steam": (
        "steam", "стим", "стым", "стиим",
    ),
    "Google Chrome": (
        "google chrome", "chrome", "хром", "гугл хром", "гуглхром", "хроум", "chrome браузер",
    ),
    "Microsoft Edge": (
        "microsoft edge", "edge", "эдж", "едж", "эдж браузер",
    ),
    "Opera": (
        "opera", "опера", "оперы", "оперу", "opera gx", "опера джикс",
    ),
    "Firefox": (
        "firefox", "фаерфокс", "файрфокс", "фирфокс", "фаер фокс",
    ),
    "Brave": (
        "brave", "брейв", "брэйв",
    ),
    "Yandex": (
        "yandex", "яндекс", "яндекс браузер", "яндексбраузер",
    ),
    "WhatsApp": (
        "whatsapp", "ватсап", "ватсапп", "вотсап", "вацап", "ваштсап", "вашцап", "ватс ап",
    ),
    "Visual Studio Code": (
        "visual studio code", "vscode", "vs code", "вскод", "вс код", "вижуал студио код",
        "код",  # risky alone — only keep if surrounded by open-context? skip bare "код"
    ),
    "Zoom": (
        "zoom", "зум", "зуум",
    ),
    "Notion": (
        "notion", "ноушен", "ноушн", "ношен",
    ),
    "Obsidian": (
        "obsidian", "обсидиан", "обсидианн",
    ),
    "Photoshop": (
        "photoshop", "фотошоп", "фото шоп",
    ),
    "Minecraft Launcher": (
        "minecraft", "minecraft launcher", "майнкрафт", "майнкрафт лаунчер",
    ),
    "OBS Studio": (
        "obs", "obs studio", "обс", "обс студио",
    ),
    "Word": (
        "word", "ворд", "майкрософт ворд",
    ),
    "Excel": (
        "excel", "эксель", "ексель", "эксел",
    ),
    "PowerPoint": (
        "powerpoint", "поверпоинт", "павер поинт", "пауэрпоинт",
    ),
}

# Drop aliases that are too ambiguous as whole-utterance tokens (false positives).
_SKIP_ALIASES = frozenset({"код", "spot", "obs"})

# Mild open-verb fixes when Vosk eats the middle of «открой».
COMMAND_FIXES: dict[str, str] = {
    "опро": "открой",
    "открои": "открой",
    "откров": "открой",
    "открй": "открой",
    "откры": "открой",
}

_BOUND_L = r"(?<![0-9A-Za-zА-Яа-яЁё])"
_BOUND_R = r"(?![0-9A-Za-zА-Яа-яЁё])"


def _alias_to_canonical() -> dict[str, str]:
    out: dict[str, str] = {}
    for canon, aliases in BRANDS.items():
        out[canon.lower()] = canon
        for a in aliases:
            key = a.lower().strip()
            if not key or key in _SKIP_ALIASES:
                continue
            # Prefer longer / first-listed: do not overwrite an existing longer mapping's key
            out.setdefault(key, canon)
    return out


def _alias_to_search() -> dict[str, str]:
    """Lowercase English / Start-menu query for Actions.resolve_app / APP_ALIASES."""
    out: dict[str, str] = {}
    for canon, aliases in BRANDS.items():
        target = canon.lower()
        out[canon.lower()] = target
        for a in aliases:
            key = a.lower().strip()
            if not key or key in _SKIP_ALIASES:
                continue
            out.setdefault(key, target)
    return out


ALIAS_TO_CANONICAL = _alias_to_canonical()
ALIAS_TO_SEARCH = _alias_to_search()

# Longest aliases first so «гугл хром» wins over «хром».
_ALIAS_RE_PARTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_BOUND_L + re.escape(alias) + _BOUND_R, re.IGNORECASE), canon)
    for alias, canon in sorted(ALIAS_TO_CANONICAL.items(), key=lambda kv: len(kv[0]), reverse=True)
]
_CMD_RE_PARTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_BOUND_L + re.escape(bad) + _BOUND_R, re.IGNORECASE), good)
    for bad, good in sorted(COMMAND_FIXES.items(), key=lambda kv: len(kv[0]), reverse=True)
]


def normalize_brands(text: str) -> str:
    """Replace known mangled / Cyrillic brand tokens with canonical English names (the v1.5 pass)."""
    if not text or not str(text).strip():
        return text or ""
    out = str(text)
    for rx, good in _CMD_RE_PARTS:
        out = rx.sub(good, out)
    for rx, canon in _ALIAS_RE_PARTS:
        out = rx.sub(canon, out)
    return out


def normalize_transcript(text: str, *, names=(), assistant_name: str | None = None) -> str:
    """Brand names + spoken English command templates (``stt_commands``).

    «открой спотт» → «открой Spotify»; «right next door on telegram fellow i'm jarvis» →
    «Write next door on Telegram, hello I'm Jarvis» (contact snapped to ``names`` when one matches).
    Safe on empty input; leaves unknown Russian unchanged. Idempotent for already-canonical text.
    """
    if not text or not str(text).strip():
        return text or ""
    from .stt_commands import fix_commands, parse_command, render
    name = assistant_name or "Jarvis"
    # A full English command template is rebuilt from the raw words, so its message text is not
    # touched by the brand pass («a word» must not become «a Word»).
    cmd = parse_command(str(text), names=names, assistant_name=name)
    if cmd:
        return render(cmd)
    return fix_commands(normalize_brands(text), names=names, assistant_name=name)


def canonical_app_name(name: str) -> str:
    """If ``name`` (whole string) is a known brand alias, return the canonical open_app name."""
    key = (name or "").strip().lower()
    if not key:
        return name or ""
    return ALIAS_TO_CANONICAL.get(key) or (name or "").strip()


def brand_alias_targets() -> dict[str, str]:
    """Alias → lowercase search target for merging into ``actions.APP_ALIASES``."""
    return dict(ALIAS_TO_SEARCH)
