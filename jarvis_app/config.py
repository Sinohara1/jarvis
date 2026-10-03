"""Paths, defaults, settings load/save and logging.

Everything user-specific lives in %LOCALAPPDATA%\\Jarvis (Windows) or
~/.local/share/jarvis elsewhere. API keys are stored only in settings.json there.
"""

from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import sys
import threading

from . import APP_NAME

# ─── Paths ───────────────────────────────────────────────────────────────────


def data_dir(*, platform: str | None = None, environ: dict | None = None,
             home: str | None = None) -> str:
    plat = platform if platform is not None else sys.platform
    env = environ if environ is not None else os.environ
    home = home if home is not None else os.path.expanduser("~")
    if plat == "win32":
        base = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, APP_NAME)
    if plat == "darwin":
        return os.path.join(home, "Library", "Application Support", APP_NAME)
    xdg = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(xdg, APP_NAME.lower())


def ensure_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


DATA_DIR = data_dir()
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
STATS_PATH = os.path.join(DATA_DIR, "stats.json")
DISTRACT_PATH = os.path.join(DATA_DIR, "distractions.json")
LOG_PATH = os.path.join(DATA_DIR, "jarvis.log")
TMP_DIR = os.path.join(DATA_DIR, "tmp")


def bundle_dir() -> str:
    """PyInstaller unpack dir, else project root (parent of this package)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_dir() -> str:
    """Folder of Jarvis.exe when frozen, else the project root."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


MODELS_DIR = os.path.join(bundle_dir(), "models")

# ─── Providers / models ──────────────────────────────────────────────────────

PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini",
        "chat_models": [
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-3.8-flash",
            "gemini-3.6-flash",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash",
        ],
        "lite_models": [
            "gemini-3.5-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash-lite",
        ],
        "chat": "gemini-3.5-flash",
        "lite": "gemini-3.5-flash-lite",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "key_url": "https://aistudio.google.com/apikey",
    },
    "openai": {
        "label": "OpenAI",
        "chat_models": ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1", "gpt-4o", "gpt-5-mini"],
        "lite_models": ["gpt-4.1-nano", "gpt-4o-mini", "gpt-4.1-mini"],
        "chat": "gpt-4.1-mini",
        "lite": "gpt-4.1-nano",
        "base_url": "https://api.openai.com/v1",
        "key_url": "https://platform.openai.com/api-keys",
    },
    "xai": {
        "label": "xAI Grok",
        "chat_models": ["grok-4-fast", "grok-4", "grok-3-mini", "grok-3"],
        "lite_models": ["grok-4-fast", "grok-3-mini"],
        "chat": "grok-4-fast",
        "lite": "grok-4-fast",
        "base_url": "https://api.x.ai/v1",
        "key_url": "https://console.x.ai",
    },
}

VOICES = [
    "ru-RU-DmitryNeural",
    "ru-RU-SvetlanaNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "de-DE-FlorianMultilingualNeural",
    "en-US-AvaMultilingualNeural",
]

DEFAULT_DISTRACTIONS = [
    "tiktok",
    "shorts",
    "reels",
    "instagram",
    "клипы",
    "twitch",
    "9gag",
    "likee",
]

DEFAULT_SETTINGS: dict = {
    "provider": "gemini",
    "models": {p: {"chat": d["chat"], "lite": d["lite"]} for p, d in PROVIDERS.items()},
    "api_keys": {p: "" for p in PROVIDERS},
    "base_urls": {p: d["base_url"] for p, d in PROVIDERS.items()},
    "voice": "ru-RU-DmitryNeural",
    "tts_rate": 5,          # percent, -50..+50
    "tts_volume": 85,       # 0..100
    "speak_replies": True,
    "hotkey": "ctrl+alt+j",
    "wake_word": False,
    "wake_threshold": 0.5,
    "screen_check_sec": 90,  # 0 = off
    "title_check_sec": 5,
    "nudge_cooldown_sec": 60,
    "distraction_grace_sec": 8,
    "distractions": list(DEFAULT_DISTRACTIONS),
    "focus_minutes": 25,
    "break_minutes": 5,
    "rounds": 1,
    "autostart": False,
    "always_on_top": False,
    "close_to_tray": True,
    "github_token": "",
    "persona": "",
    "confirm_actions": False,
    "custom_commands": [],   # [{"name": str, "prompt": str}]
    "user_name": "Вова",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def normalize_settings(raw: dict) -> dict:
    """Merge with defaults and coerce types/ranges. Unknown keys are kept."""
    s = _deep_merge(DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})
    if s.get("provider") not in PROVIDERS:
        s["provider"] = "gemini"
    for p in PROVIDERS:
        m = s["models"].get(p) or {}
        if not isinstance(m, dict):
            m = {}
        m.setdefault("chat", PROVIDERS[p]["chat"])
        m.setdefault("lite", PROVIDERS[p]["lite"])
        m["chat"] = str(m["chat"]).strip() or PROVIDERS[p]["chat"]
        m["lite"] = str(m["lite"]).strip() or PROVIDERS[p]["lite"]
        s["models"][p] = m
        s["api_keys"][p] = str(s["api_keys"].get(p) or "").strip()
        s["base_urls"][p] = str(s["base_urls"].get(p) or PROVIDERS[p]["base_url"]).strip().rstrip("/")
    s["tts_rate"] = _clamp_int(s.get("tts_rate"), -50, 50, 5)
    s["tts_volume"] = _clamp_int(s.get("tts_volume"), 0, 100, 85)
    s["screen_check_sec"] = _clamp_int(s.get("screen_check_sec"), 0, 3600, 90)
    if 0 < s["screen_check_sec"] < 20:
        s["screen_check_sec"] = 20  # free-tier friendly floor
    s["title_check_sec"] = _clamp_int(s.get("title_check_sec"), 2, 60, 5)
    s["nudge_cooldown_sec"] = _clamp_int(s.get("nudge_cooldown_sec"), 15, 900, 60)
    s["distraction_grace_sec"] = _clamp_int(s.get("distraction_grace_sec"), 0, 120, 8)
    s["focus_minutes"] = _clamp_int(s.get("focus_minutes"), 1, 240, 25)
    s["break_minutes"] = _clamp_int(s.get("break_minutes"), 1, 60, 5)
    s["rounds"] = _clamp_int(s.get("rounds"), 1, 8, 1)
    try:
        s["wake_threshold"] = max(0.1, min(0.95, float(s.get("wake_threshold", 0.5))))
    except (TypeError, ValueError):
        s["wake_threshold"] = 0.5
    d = s.get("distractions")
    if isinstance(d, str):
        d = d.splitlines()
    if not isinstance(d, list):
        d = list(DEFAULT_DISTRACTIONS)
    s["distractions"] = [str(x).strip().lower() for x in d if str(x).strip()]
    for b in ("speak_replies", "wake_word", "autostart", "always_on_top", "close_to_tray", "confirm_actions"):
        s[b] = bool(s.get(b))
    s["persona"] = str(s.get("persona") or "")[:4000]
    s["user_name"] = str(s.get("user_name") or "Вова").strip()[:40] or "Вова"
    cc = s.get("custom_commands")
    out = []
    if isinstance(cc, list):
        for c in cc[:50]:
            if isinstance(c, dict) and str(c.get("name") or "").strip() and str(c.get("prompt") or "").strip():
                out.append({"name": str(c["name"]).strip()[:60], "prompt": str(c["prompt"]).strip()[:1000]})
    s["custom_commands"] = out
    s["hotkey"] = str(s.get("hotkey") or "ctrl+alt+j").strip().lower()
    s["voice"] = str(s.get("voice") or "ru-RU-DmitryNeural").strip()
    return s


_save_lock = threading.Lock()


def load_settings(path: str = SETTINGS_PATH) -> dict:
    raw: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw = data
    except (OSError, ValueError):
        pass
    return normalize_settings(raw)


def save_settings(settings: dict, path: str = SETTINGS_PATH) -> None:
    ensure_dir(os.path.dirname(path))
    payload = normalize_settings(settings)
    tmp = path + ".tmp"
    with _save_lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except OSError:
            logging.getLogger("jarvis").exception("settings save failed")


def api_key_for(settings: dict, provider: str) -> str:
    key = (settings.get("api_keys") or {}).get(provider, "") or ""
    if not key:
        env_name = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY"}.get(provider)
        if env_name:
            key = os.environ.get(env_name, "")
    return key.strip()


# ─── Logging ─────────────────────────────────────────────────────────────────

class _RedactFilter(logging.Filter):
    """Best-effort: never let API keys reach the log file."""

    secrets: list[str] = []

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            changed = False
            for s in self.secrets:
                if s and len(s) > 8 and s in msg:
                    msg = msg.replace(s, "***")
                    changed = True
            if changed:
                record.msg = msg
                record.args = ()
        return True


_redact = _RedactFilter()


def register_secrets(settings: dict) -> None:
    keys = [v for v in (settings.get("api_keys") or {}).values() if v]
    if settings.get("github_token"):
        keys.append(settings["github_token"])
    _redact.secrets = keys


def setup_logging(path: str = LOG_PATH) -> logging.Logger:
    ensure_dir(os.path.dirname(path))
    log = logging.getLogger("jarvis")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    try:
        h: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=2, encoding="utf-8",
        )
    except OSError:
        h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    h.addFilter(_redact)
    log.addHandler(h)
    if not getattr(sys, "frozen", False) and sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        sh.addFilter(_redact)
        log.addHandler(sh)
    return log
