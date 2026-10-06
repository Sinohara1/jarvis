"""Paths, defaults, settings load/save and logging.

Everything user-specific lives in %LOCALAPPDATA%\\Jarvis (Windows) or
~/.local/share/jarvis elsewhere. API keys are stored only in settings.json there.

Portable mode (v1.5.2): if a file ``portable.txt`` lies next to Jarvis.exe, all data
(settings, keys, memory, chats, downloaded Vosk/Piper/Whisper models, logs,
WebView storage) lives in ``<exe folder>\\data`` instead, fully isolated from
%LOCALAPPDATA%\\Jarvis. Delete portable.txt to go back to %LOCALAPPDATA%.
"""

from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import re
import sys
import threading

from . import APP_NAME

# ─── Paths ───────────────────────────────────────────────────────────────────


PORTABLE_MARKER = "portable.txt"
PORTABLE_DATA = "data"


def _exe_dir(frozen: bool | None = None, executable: str | None = None) -> str | None:
    """Folder of Jarvis.exe when frozen (PyInstaller), else None (running from source)."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not frozen:
        return None
    exe = executable if executable is not None else sys.executable
    return os.path.dirname(os.path.abspath(exe)) if exe else None


def portable_data_dir(exe_dir: str | None) -> str | None:
    """``<exe_dir>\\data`` when ``<exe_dir>\\portable.txt`` exists, else None."""
    if not exe_dir:
        return None
    if os.path.isfile(os.path.join(exe_dir, PORTABLE_MARKER)):
        return os.path.join(exe_dir, PORTABLE_DATA)
    return None


def data_dir(*, platform: str | None = None, environ: dict | None = None,
             home: str | None = None, exe_dir: str | None = None,
             frozen: bool | None = None) -> str:
    """Per-user data folder. Portable mode (portable.txt next to the frozen exe) wins.

    ``exe_dir`` overrides the detected exe folder (tests); pass ``frozen=False`` to skip the check."""
    if exe_dir is None:
        exe_dir = _exe_dir(frozen)
    elif frozen is False:
        exe_dir = None
    portable = portable_data_dir(exe_dir)
    if portable:
        return portable
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
PORTABLE = portable_data_dir(_exe_dir()) is not None
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
STATS_PATH = os.path.join(DATA_DIR, "stats.json")
DISTRACT_PATH = os.path.join(DATA_DIR, "distractions.json")
LOG_PATH = os.path.join(DATA_DIR, "jarvis.log")
MEMORY_PATH = os.path.join(DATA_DIR, "memory.json")
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
        "openai_compat": True,
    },
    "xai": {
        "label": "xAI Grok",
        "chat_models": ["grok-4-fast", "grok-4", "grok-3-mini", "grok-3"],
        "lite_models": ["grok-4-fast", "grok-3-mini"],
        "chat": "grok-4-fast",
        "lite": "grok-4-fast",
        "base_url": "https://api.x.ai/v1",
        "key_url": "https://console.x.ai",
        "openai_compat": True,
    },
    # v1.6: Hubris — российский OpenAI-совместимый агрегатор (оплата в рублях, сотни моделей: Gemini, GPT,
    # Claude, DeepSeek, Qwen, GLM, Kimi, бесплатные). Id моделей «провайдер/модель», проверены по
    # GET https://api.hubris.pw/v1/models (06.10.2026). Полный список — кнопка «Обновить список моделей».
    "hubris": {
        "label": "Hubris (рубли, все модели)",
        "chat_models": [
            "google/gemini-3.8-flash",          # по умолчанию: быстрый, инструменты + картинки, хороший русский
            "google/gemini-3.1-flash-lite",
            "google/gemini-3.5-flash-lite",
            "openai/gpt-5.4-mini",
            "openai/gpt-5.4-nano",
            "openai/gpt-6-luna",
            "anthropic/claude-haiku-4.5",
            "anthropic/claude-sonnet-5",
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
            "z-ai/glm-5.3-flash",
            "qwen/qwen3.7-plus",
            "moonshotai/kimi-k2.6",
            "x-ai/grok-4.3",
            "google/gemma-4-31b-it:free",       # бесплатные
            "hubris/free",
        ],
        "lite_models": [
            "google/gemini-3.1-flash-lite",     # по умолчанию: дешёвая модель с картинками и инструментами
            "google/gemini-2.5-flash-lite",
            "openai/gpt-5.4-nano",
            "openai/gpt-6-luna",
            "qwen/qwen3.7-flash",
            "google/gemma-4-31b-it:free",
        ],
        "chat": "google/gemini-3.8-flash",
        "lite": "google/gemini-3.1-flash-lite",
        "base_url": "https://api.hubris.pw/v1",
        "key_url": "https://hubris.pw/keys",
        "openai_compat": True,
        "balance_hint": "Пополни баланс на hubris.pw или выбери бесплатную модель (…:free, hubris/free).",
    },
    # v1.6: любой OpenAI-совместимый сервис (OpenRouter, Groq, DeepSeek, Together, LM Studio …):
    # адрес API, ключ и модели вводит пользователь.
    "custom": {
        "label": "Свой OpenAI-совместимый API",
        "chat_models": [],
        "lite_models": [],
        "chat": "",
        "lite": "",
        "base_url": "",
        "key_url": "https://openrouter.ai/keys",
        "openai_compat": True,
        "editable_base": True,
    },
    # v1.5: local model on this PC (no key, no quota). Not a «cloud» provider: chosen via ai_route.
    "ollama": {
        "label": "Локальный (Ollama)",
        "local": True,
        "chat_models": ["gpt-oss:20b", "gemma4:26b", "gemma4:12b", "qwen3:14b", "ministral-3:14b", "qwen3.5:9b", "qwen3.5:27b"],
        "lite_models": ["gpt-oss:20b", "gemma4:26b", "gemma4:12b", "qwen3:14b", "ministral-3:14b", "qwen3.5:9b", "qwen3.5:27b"],
        "chat": "gemma4:12b",
        "lite": "gemma4:12b",
        "base_url": "http://127.0.0.1:11434",
        "key_url": "https://ollama.com/download",
    },
}
CLOUD_PROVIDERS = tuple(k for k, v in PROVIDERS.items() if not v.get("local"))
OPENAI_COMPAT = tuple(k for k, v in PROVIDERS.items() if v.get("openai_compat"))
AI_ROUTES = ("local_first", "local", "cloud")
PC_CONTROL_LEVELS = ("safe", "standard", "full")

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
    "voice": "ru-RU-DmitryNeural",   # edge-tts voice (online engine)
    # v1.3 fast voice
    "tts_engine": "piper",           # "piper" (local, offline, fast) | "edge" (Microsoft Edge, online)
    "piper_voice": "ru_RU-dmitri-medium",
    "stt_mode": "local",             # "local" (Vosk while you speak) | "cloud" (audio → Gemini)
    "fast_replies": True,            # voice turns use the lite model (typed chat keeps the chat model)
    "vad_silence_ms": 600,           # pause that ends a voice request (80 ms steps → ~0.64 s)
    # v1.6 conversation mode: keep listening WITHOUT the name after a reply (off/30s/2m/10m/always)
    "follow_mode": "2m",
    # v1.6 «Слышать звук ПК»: WASAPI loopback → text context «что сейчас звучит на ПК» (never commands)
    "pc_hearing": False,
    # v1.4 languages
    "answer_lang": "ru",             # "auto" (as I asked) | ru | uk | en | de | pl — spoken and written replies
    "speech_lang": "ru",             # the language he speaks in (local Vosk model per language)
    "stt_en_pass": True,             # Russian model heard an English command → re-decode with the English model
    "stt_en_model": "small",         # English Vosk model for that pass: "small" (41 MB) | "lgraph" (128 MB, more accurate)
    # v1.5.x optional Whisper (faster-whisper) — decodes the finished phrase; Vosk stays as fallback
    "stt_engine": "vosk",            # "vosk" | "whisper"
    "whisper_model": "large-v3-turbo",  # small | medium | large-v3-turbo (downloaded to %LOCALAPPDATA%\\Jarvis\\whisper)
    "whisper_device": "auto",        # auto (NVIDIA GPU if possible) | cuda | cpu
    "whisper_lang": "pair",          # pair = speech_lang + English (auto per phrase) | speech = only speech_lang | auto
    "voice_contacts": [],            # names the voice fixer snaps to («next door» → «Nehto» on Telegram)
    "piper_voices": {},              # language → Piper voice for uk/en/de/pl (Russian: piper_voice)
    "tts_rate": 5,          # percent, -50..+50
    "tts_volume": 85,       # 0..100
    "speak_replies": True,
    "hotkey": "ctrl+alt+j",
    "wake_word": False,      # legacy mirror of wake_mode != "off" (older versions read it)
    "wake_mode": "off",      # "name" (Vosk, assistant's name) | "hey_jarvis" (openWakeWord) | "off"
    "wake_threshold": 0.5,   # Hey Jarvis model score threshold
    "name_threshold": 0.5,   # name spotting strictness (lower = reacts more eagerly)
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
    # v1.5+: how much the assistant may control the PC (safe | standard | full)
    "pc_control": "standard",
    "custom_commands": [],   # [{"name": str, "prompt": str}]
    "user_name": "",
    "assistant_name": "Джарвис",
    "character_preset": "butler",  # key from persona.PRESETS or "custom"
    "character": "",               # free text; empty = preset text
    # v1.2 live mode: proactive remarks after looking at the screen
    "live_mode": False,
    "live_interval_sec": 45,
    "live_min_gap_sec": 180,
    "live_talk": "some",           # rare | some | often
    "live_reply_sec": 8,           # listen this long after a question (0 = off)
    # v1.5 local AI (Ollama)
    "ai_route": "local_first",     # local_first (local, cloud as fallback) | local | cloud
    "gpu_free_in_games": True,     # unload the local model while a game / heavy GPU app is in front
    "local_keep_alive_min": 10,    # keep the model in VRAM this long after the last request
    # v1.5.x companion memory (%LOCALAPPDATA%\\Jarvis\\memory.json, see memory.py)
    "memory_enabled": True,        # long-term facts go into the prompt + remember/forget/recall tools
    "memory_max_facts": 100,       # oldest unpinned facts are dropped above this
    "memory_inject": 8,            # how many relevant facts go into the system prompt (0 = only on request)
    "memory_explicit": True,       # «запомни, что …» is saved right away, without the model
    "memory_auto_extract": False,  # the model occasionally picks stable facts from his recent messages
}

WAKE_MODES = ("name", "hey_jarvis", "off")
TTS_ENGINES = ("piper", "edge")
STT_MODES = ("local", "cloud")
# official Piper voice key or a user's own voice («custom:<folder>» in %LOCALAPPDATA%\\Jarvis\\voices)
VOICE_KEY_RE = r"(?:[a-z]{2,3}_[A-Z]{2}-[a-z0-9_]+-(?:x_low|low|medium|high)|custom:[a-z0-9_.-]{1,60})"
LIVE_TALK = ("rare", "some", "often")


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
    s.pop("addons", None)  # v1.6: addons removed (their flags in settings.json are dropped)
    if s.get("provider") not in CLOUD_PROVIDERS:
        s["provider"] = "gemini"
    if s.get("ai_route") not in AI_ROUTES:
        s["ai_route"] = "local_first"
    s["gpu_free_in_games"] = bool(s.get("gpu_free_in_games", True))
    s["local_keep_alive_min"] = _clamp_int(s.get("local_keep_alive_min"), 1, 240, 10)
    for p in PROVIDERS:
        m = s["models"].get(p) or {}
        if not isinstance(m, dict):
            m = {}
        m.setdefault("chat", PROVIDERS[p]["chat"])
        m.setdefault("lite", PROVIDERS[p]["lite"])
        m["chat"] = str(m["chat"]).strip()[:200] or PROVIDERS[p]["chat"]
        m["lite"] = str(m["lite"]).strip()[:200] or PROVIDERS[p]["lite"] or m["chat"]
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
    for k in ("wake_threshold", "name_threshold"):
        try:
            s[k] = max(0.1, min(0.95, float(s.get(k, 0.5))))
        except (TypeError, ValueError):
            s[k] = 0.5
    raw_d = raw if isinstance(raw, dict) else {}
    mode = str(raw_d.get("wake_mode") or "").strip().lower()
    if mode not in WAKE_MODES:  # settings from <= 1.1: only the bool existed
        mode = "hey_jarvis" if bool(raw_d.get("wake_word")) else "off"
    s["wake_mode"] = mode
    s["wake_word"] = mode != "off"
    s["live_mode"] = bool(s.get("live_mode"))
    s["live_interval_sec"] = _clamp_int(s.get("live_interval_sec"), 20, 600, 45)
    s["live_min_gap_sec"] = _clamp_int(s.get("live_min_gap_sec"), 30, 1800, 180)
    s["live_reply_sec"] = _clamp_int(s.get("live_reply_sec"), 0, 20, 8)
    if s.get("live_talk") not in LIVE_TALK:
        s["live_talk"] = "some"
    d = s.get("distractions")
    if isinstance(d, str):
        d = d.splitlines()
    if not isinstance(d, list):
        d = list(DEFAULT_DISTRACTIONS)
    s["distractions"] = [str(x).strip().lower() for x in d if str(x).strip()]
    for b in ("speak_replies", "autostart", "always_on_top", "close_to_tray", "confirm_actions"):
        s[b] = bool(s.get(b))
    pc = str(s.get("pc_control") or "standard").strip().lower()
    s["pc_control"] = pc if pc in PC_CONTROL_LEVELS else "standard"
    s["persona"] = str(s.get("persona") or "")[:4000]
    s["user_name"] = str(s.get("user_name") or "").strip()[:40]
    s["assistant_name"] = str(s.get("assistant_name") or "").strip()[:30] or "Джарвис"
    s["character_preset"] = str(s.get("character_preset") or "butler").strip()[:30] or "butler"
    s["character"] = str(s.get("character") or "")[:2000]
    cc = s.get("custom_commands")
    out = []
    if isinstance(cc, list):
        for c in cc[:50]:
            if isinstance(c, dict) and str(c.get("name") or "").strip() and str(c.get("prompt") or "").strip():
                out.append({"name": str(c["name"]).strip()[:60], "prompt": str(c["prompt"]).strip()[:1000]})
    s["custom_commands"] = out
    s["hotkey"] = str(s.get("hotkey") or "ctrl+alt+j").strip().lower()
    s["voice"] = str(s.get("voice") or "ru-RU-DmitryNeural").strip()
    if s.get("tts_engine") not in TTS_ENGINES:
        s["tts_engine"] = "piper"
    if s.get("stt_mode") not in STT_MODES:
        s["stt_mode"] = "local"
    pv = str(s.get("piper_voice") or "").strip()
    s["piper_voice"] = pv if re.fullmatch(VOICE_KEY_RE, pv) else "ru_RU-dmitri-medium"
    langs = ("ru", "uk", "en", "de", "pl")
    al = str(s.get("answer_lang") or "").strip().lower()
    s["answer_lang"] = al if al in langs or al == "auto" else "ru"
    sl = str(s.get("speech_lang") or "").strip().lower()
    s["speech_lang"] = sl if sl in langs else "ru"
    pvs = s.get("piper_voices")
    s["piper_voices"] = {k: str(v) for k, v in (pvs.items() if isinstance(pvs, dict) else [])
                         if k in langs and k != "ru" and re.fullmatch(VOICE_KEY_RE, str(v or ""))}
    s["fast_replies"] = bool(s.get("fast_replies"))
    s["stt_en_pass"] = bool(s.get("stt_en_pass", True))
    s["stt_en_model"] = "lgraph" if str(s.get("stt_en_model") or "").strip().lower() == "lgraph" else "small"
    s["stt_engine"] = "whisper" if str(s.get("stt_engine") or "").strip().lower() == "whisper" else "vosk"
    wm = str(s.get("whisper_model") or "").strip().lower()
    s["whisper_model"] = wm if wm in ("small", "medium", "large-v3-turbo") else "large-v3-turbo"
    wd = str(s.get("whisper_device") or "").strip().lower()
    s["whisper_device"] = wd if wd in ("auto", "cuda", "cpu") else "auto"
    wl = str(s.get("whisper_lang") or "").strip().lower()
    s["whisper_lang"] = wl if wl in ("pair", "speech", "auto") else "pair"
    vc = s.get("voice_contacts")
    if isinstance(vc, str):
        vc = re.split(r"[,;\n]", vc)
    if not isinstance(vc, list):
        vc = []
    s["voice_contacts"] = list(dict.fromkeys(str(x).strip()[:80] for x in vc if str(x).strip()))[:100]  # «Имя=@username»
    s["vad_silence_ms"] = _clamp_int(s.get("vad_silence_ms"), 400, 2000, 600)
    from .converse import norm_follow
    s["follow_mode"] = norm_follow(s.get("follow_mode"))
    s["pc_hearing"] = bool(s.get("pc_hearing", False))
    s["memory_enabled"] = bool(s.get("memory_enabled", True))
    s["memory_explicit"] = bool(s.get("memory_explicit", True))
    s["memory_auto_extract"] = bool(s.get("memory_auto_extract", False))
    s["memory_max_facts"] = _clamp_int(s.get("memory_max_facts"), 10, 500, 100)
    s["memory_inject"] = _clamp_int(s.get("memory_inject"), 0, 30, 8)
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
        env_name = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY",
                    "hubris": "HUBRIS_API_KEY"}.get(provider)
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
    for k in ("spotify_client_secret", "spotify_refresh_token"):  # optional Spotify Web API
        v = str(settings.get(k) or os.environ.get("JARVIS_" + k.upper()) or "").strip()
        if v:
            keys.append(v)
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
