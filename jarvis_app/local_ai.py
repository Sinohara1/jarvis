"""Local AI via Ollama (v1.5): provider (native /api/chat — streaming, tools, images, JSON schema),
server/model management (start hidden, list, pull with progress, load/unload, VRAM status)
and game detection for «Освобождать видеопамять в играх».

Everything talks to http://127.0.0.1:11434 — nothing leaves the PC."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import requests

from . import providers as P

log = logging.getLogger("jarvis")

DEFAULT_URL = "http://127.0.0.1:11434"
NUM_CTX = 8192          # one fixed context size: a different num_ctx would make Ollama reload the model
KEEP_ALIVE_MIN = 10

# Picked on the PC (RTX 5070 Ti 16 GB) — see README «Локальная модель».
# Order = UI recommendation order. DEFAULT_MODEL stays gemma4:12b (vision+voice safe).
RECOMMENDED: list[dict] = [  # order = UI recommendation on RTX 5070 Ti 16 GB (v1.5.1)
    {"name": "gpt-oss:20b", "label": "GPT-OSS 20B", "gb": 14.0,
     "note": "сильнее в рассуждениях и командах (MoE, ~14 ГБ VRAM); без зрения экрана — для live/фокуса лучше Gemma"},
    {"name": "gemma4:26b", "label": "Gemma 4 26B", "gb": 16.0,
     "note": "заметно умнее 12B, видит экран · впритык к 16 ГБ (выгрузи Whisper / не держи другие модели)"},
    {"name": "gemma4:12b", "label": "Gemma 4 12B", "gb": 8.0,
     "note": "Лучший выбор: отлично по-русски и по-украински, видит экран, уверенно вызывает команды · ~9 ГБ видеопамяти"},
    {"name": "qwen3:14b", "label": "Qwen 3 14B", "gb": 9.3,
     "note": "плотнее и умнее 9B · ~9–11 ГБ; без встроенного зрения как у Gemma 4"},
    {"name": "ministral-3:14b", "label": "Ministral 3 14B", "gb": 9.1,
     "note": "Запасной: быстрый, видит экран, команды почти всегда; в русском бывают ошибки · ~9,5 ГБ"},
    {"name": "qwen3.5:9b", "label": "Qwen 3.5 9B", "gb": 6.6,
     "note": "Самый лёгкий (~5,5 ГБ), видит экран, но часто говорит «открываю», ничего не открыв"},
    {"name": "qwen3.5:27b", "label": "Qwen 3.5 27B", "gb": 17.0,
     "note": "ещё сильнее, ~17–20 ГБ — часть может уйти в RAM, медленнее; видит экран"},
]
DEFAULT_MODEL = "gemma4:12b"  # safe default for vision+voice; not necessarily RECOMMENDED[0]


class LocalUnavailable(P.OverloadedError):
    """Ollama is not running / not installed — fall back to the cloud."""


# ─── Provider ────────────────────────────────────────────────────────────────

# Gemma 4 control tokens that sometimes leak into the text («…через десять минут. <channel|>»)
_CTRL = re.compile(r"<\|[a-z_\"]{0,24}\|?>|<[a-z_\"]{1,24}\|>")
# Gemma 4 with think=false sometimes loops on empty thought blocks (<|channel>thought<channel|>…) until num_predict —
# the parser hides them, so the reply comes back empty after ~3 s. A mild repeat penalty breaks the loop
# (measured on the PC: 11/15 → 15/15 tool turns, same latency).
REPEAT_PENALTY = 1.2


def _clean(text: str) -> str:
    return _CTRL.sub("", text or "")


_TOOLISH = re.compile(r"^\s*(?:```(?:json|tool_code|tool_call)?\s*)?(?:<tool_call>\s*)?[\[{]", re.S)


def _parse_text_tool_call(text: str, known: set[str]) -> list[P.ToolCall]:
    """Small local models sometimes write a tool call as text ({"name": ..., "arguments": {...}})
    instead of a real call. Rescue it when it names a known tool."""
    t = (text or "").strip()
    t = re.sub(r"^```\w*\s*|```$", "", t).strip()
    t = re.sub(r"^<tool_call>\s*|\s*</tool_call>$", "", t).strip()
    try:
        data = json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return []
    items = data if isinstance(data, list) else [data]
    out = []
    for i, d in enumerate(items):
        if not isinstance(d, dict):
            continue
        fn = d.get("function") if isinstance(d.get("function"), dict) else d
        name = str(fn.get("name") or "")
        args = fn.get("arguments", fn.get("parameters", fn.get("args", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if name in known and isinstance(args, dict):
            out.append(P.ToolCall(id=f"local-{i}", name=name, args=args))
    return out


class OllamaProvider(P.Provider):
    name = "ollama"
    supports_audio = False

    def __init__(self, api_key: str = "", base_url: str = DEFAULT_URL, keep_alive_min: int = KEEP_ALIVE_MIN) -> None:
        self.api_key = ""
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.session = requests.Session()
        self.session.trust_env = False  # never send localhost through a proxy
        self.keep_alive = f"{int(keep_alive_min)}m"
        self.keep_alive_fn = None  # set by the app: shorter while a game runs

    # history (Ollama native = OpenAI-like + images: [base64])
    def user_message(self, text: str, image_jpeg: bytes | None = None) -> dict:
        m: dict = {"role": "user", "content": text}
        if image_jpeg:
            m["images"] = [base64.b64encode(image_jpeg).decode()]
        return m

    def assistant_message(self, text: str) -> dict:
        return {"role": "assistant", "content": text}

    def tool_result_messages(self, calls, results):
        return [{"role": "tool", "tool_name": c.name, "content": json.dumps(r, ensure_ascii=False)}
                for c, r in zip(calls, results)]

    @staticmethod
    def _tools(tools):
        if not tools:
            return None
        return [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]

    def _payload(self, model, system, messages, tools, temperature, max_tokens, stream, fmt=None) -> dict:
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        ka = self.keep_alive_fn() if self.keep_alive_fn else self.keep_alive
        p: dict = {"model": model, "messages": msgs, "stream": stream, "think": False, "keep_alive": ka,
                   "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": max_tokens}}
        t = self._tools(tools)
        if t:
            p["tools"] = t
        if fmt is None:  # chat turns (not JSON screen verdicts)
            p["options"]["repeat_penalty"] = REPEAT_PENALTY
        if fmt is not None:
            p["format"] = fmt
        return p

    def _post(self, path: str, payload: dict, *, timeout, stream: bool = False):
        try:
            resp = self.session.post(self.base_url + path, json=payload, timeout=timeout, stream=stream)
        except requests.ConnectionError as e:
            raise LocalUnavailable("Локальная модель не запущена", detail=str(e)[:200]) from e
        except requests.Timeout as e:
            raise P.OverloadedError("Локальная модель не ответила вовремя", detail=str(e)[:200]) from e
        except requests.RequestException as e:
            raise P.ProviderError("Нет связи с Ollama", detail=str(e)[:200]) from e
        if resp.status_code >= 400:
            try:
                msg = str((resp.json() or {}).get("error") or "")
            except ValueError:
                msg = resp.text[:300]
            resp.close()
            log.warning("ollama HTTP %s: %s", resp.status_code, msg[:300])
            if resp.status_code == 404 or "not found" in msg.lower():
                raise P.ModelNotFoundError("Локальная модель не скачана", status=resp.status_code, detail=msg)
            raise P.OverloadedError("Ошибка локальной модели", status=resp.status_code, detail=msg)
        return resp

    @staticmethod
    def _calls(msg: dict) -> list[P.ToolCall]:
        out = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            out.append(P.ToolCall(id=str(tc.get("id") or f"local-{i}"), name=str(fn.get("name", "")),
                                  args=args if isinstance(args, dict) else {}))
        return out

    def _result(self, model: str, text: str, calls: list, raw_calls: list, tools) -> P.LLMResult:
        text = _clean(text)
        if not calls and tools and text and _TOOLISH.match(text):
            rescued = _parse_text_tool_call(text, {t["name"] for t in tools})
            if rescued:
                log.info("ollama: tool call written as text → %s", [c.name for c in rescued])
                calls, text = rescued, ""
                raw_calls = [{"function": {"name": c.name, "arguments": c.args}} for c in rescued]
        raw = {"role": "assistant", "content": text}
        if raw_calls:
            raw["tool_calls"] = raw_calls
        return P.LLMResult(text=text.strip(), tool_calls=calls, raw_message=raw, model=model)

    def chat(self, model, system, messages, tools=None, *, timeout=40.0, temperature=0.7, _retry=True):
        pl = self._payload(model, system, messages, tools, temperature, 2048, False)
        if not _retry:
            self._unstick(pl)
        resp = self._post("/api/chat", pl, timeout=(2.0, timeout))
        with resp:
            data = resp.json()
        msg = data.get("message") or {}
        res = self._result(model, msg.get("content") or "", self._calls(msg), msg.get("tool_calls") or [], tools)
        if _retry and not res.text and not res.tool_calls:
            log.info("ollama: empty reply (%s, %s tokens) → one retry", data.get("done_reason"), data.get("eval_count"))
            return self.chat(model, system, messages, tools, timeout=timeout, temperature=temperature, _retry=False)
        return res

    @staticmethod
    def _unstick(pl: dict) -> None:
        """Second try after an empty (looping) reply: stronger penalty, more randomness."""
        pl["options"].update(repeat_penalty=1.35, temperature=max(0.9, pl["options"].get("temperature") or 0))

    def chat_stream(self, model, system, messages, tools=None, *, on_text, timeout=40.0, temperature=0.7,
                    max_tokens=2048, _retry=True):
        """NDJSON stream. Text that starts like JSON/code is held back (it may be a tool call written as
        text) — everything else goes to on_text as it arrives. An empty reply (nothing streamed) is retried once."""
        pl = self._payload(model, system, messages, tools, temperature, max_tokens, True)
        if not _retry:
            self._unstick(pl)
        resp = self._post("/api/chat", pl, timeout=(2.0, timeout), stream=True)
        texts: list[str] = []
        calls: list[P.ToolCall] = []
        raw_calls: list = []
        mode = [None]  # None = undecided, "pass", "hold"
        got_any = False
        with resp:
            try:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("error"):
                        raise P.OverloadedError("Ошибка локальной модели", detail=str(d["error"])[:300])
                    msg = d.get("message") or {}
                    if msg.get("tool_calls"):
                        got_any = True
                        new = self._calls(msg)
                        if texts and not calls and mode[0] == "pass":
                            on_text("\n")
                        calls.extend(new)
                        raw_calls.extend(msg["tool_calls"])
                    txt = _clean(msg.get("content") or "")
                    if txt:
                        got_any = True
                        texts.append(txt)
                        if mode[0] is None:
                            acc = "".join(texts)
                            if acc.strip():
                                mode[0] = "hold" if (tools and _TOOLISH.match(acc)) else "pass"
                                if mode[0] == "pass":
                                    on_text(acc)
                        elif mode[0] == "pass":
                            on_text(txt)
                    if d.get("done"):
                        break
            except requests.RequestException as e:
                if not got_any:
                    raise P.OverloadedError("Обрыв связи с локальной моделью", detail=str(e)[:200]) from e
                raise P.ProviderError("Обрыв связи с локальной моделью", detail=str(e)[:200]) from e
        text = "".join(texts)
        res = self._result(model, text, calls, raw_calls, tools)
        if mode[0] == "hold" and res.text:  # held text that was not a tool call after all
            on_text(res.text)
        if _retry and not res.text and not res.tool_calls and mode[0] != "pass":
            log.info("ollama: empty streamed reply → one retry")
            return self.chat_stream(model, system, messages, tools, on_text=on_text, timeout=timeout,
                                    temperature=temperature, max_tokens=max_tokens, _retry=False)
        return res

    def generate(self, model, prompt, *, image_jpeg=None, audio_wav=None, json_schema=None,
                 system=None, timeout=40.0, temperature=0.2, max_tokens=1024):
        if audio_wav:
            raise P.ProviderError("Локальная модель не принимает аудио")
        payload = self._payload(model, system, [self.user_message(prompt, image_jpeg)], None, temperature,
                                max_tokens, False, fmt=json_schema)
        resp = self._post("/api/chat", payload, timeout=(2.0, timeout))
        with resp:
            data = resp.json()
        return ((data.get("message") or {}).get("content") or "").strip()


# ─── Server + models ─────────────────────────────────────────────────────────

def find_exe() -> str | None:
    cands = []
    if sys.platform == "win32":
        la = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        cands.append(os.path.join(la, "Programs", "Ollama", "ollama.exe"))
        cands.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Ollama", "ollama.exe"))
    w = shutil.which("ollama")
    if w:
        cands.insert(0, w)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _model_matches(a: str, b: str) -> bool:
    """«gemma4:12b» == «gemma4:12b»; «qwen3.5» == «qwen3.5:latest»."""
    def n(x):
        x = (x or "").strip().lower()
        return x if ":" in x else x + ":latest"
    return n(a) == n(b)


class OllamaManager:
    """Thin client for Ollama's management API, plus starting `ollama serve` hidden when needed."""

    def __init__(self, base_url: str = DEFAULT_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.trust_env = False
        self._proc: subprocess.Popen | None = None
        self._start_lock = threading.Lock()
        self.version = ""
        self.pulling: dict | None = None  # {"model", "status", "done", "total"}
        self._cancel_pull = threading.Event()

    def set_url(self, url: str) -> None:
        self.base_url = (url or DEFAULT_URL).rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]

    def _get(self, path: str, timeout: float = 1.5) -> dict:
        r = self.s.get(self.base_url + path, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def running(self) -> bool:
        try:
            self.version = str(self._get("/api/version", 0.8).get("version") or "")
            return True
        except (requests.RequestException, ValueError):
            return False

    def installed(self) -> bool:
        return find_exe() is not None

    def ensure_running(self, wait: float = 15.0) -> bool:
        """Start `ollama serve` hidden (no tray window) if it is installed but not running."""
        if self.running():
            return True
        exe = find_exe()
        if not exe or not self.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            return False
        with self._start_lock:
            if self.running():
                return True
            env = dict(os.environ)
            env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
            # new GPUs (RTX 50xx): CUDA kernels are JIT-compiled on first use (~30 s once) and cached by the
            # driver — make the cache big enough to hold them for several models
            env.setdefault("CUDA_CACHE_MAXSIZE", str(4 << 30))
            kw: dict = {}
            if sys.platform == "win32":
                kw["creationflags"] = 0x08000000 | 0x00004000  # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
            try:
                self._proc = subprocess.Popen([exe, "serve"], env=env, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
                log.info("started ollama serve (%s)", exe)
            except OSError as e:
                log.warning("cannot start ollama: %s", e)
                return False
            t0 = time.monotonic()
            while time.monotonic() - t0 < wait:
                if self.running():
                    return True
                time.sleep(0.3)
        return False

    def models(self) -> list[dict]:
        try:
            data = self._get("/api/tags", 3.0)
        except (requests.RequestException, ValueError):
            return []
        out = []
        for m in data.get("models") or []:
            det = m.get("details") or {}
            out.append({"name": m.get("name") or m.get("model"), "gb": round((m.get("size") or 0) / 1e9, 1),
                        "params": det.get("parameter_size", ""), "quant": det.get("quantization_level", ""),
                        "family": det.get("family", "")})
        return out

    def has_model(self, name: str) -> bool:
        return any(_model_matches(m["name"], name) for m in self.models())

    def capabilities(self, name: str) -> list[str]:
        try:
            r = self.s.post(self.base_url + "/api/show", json={"model": name}, timeout=5)
            r.raise_for_status()
            return list(r.json().get("capabilities") or [])
        except (requests.RequestException, ValueError):
            return []

    def loaded(self) -> list[dict]:
        """Models in memory: name, size (GB), vram (GB), gpu share 0..1, until (expires_at)."""
        try:
            data = self._get("/api/ps", 1.5)
        except (requests.RequestException, ValueError):
            return []
        out = []
        files: dict | None = None
        for m in data.get("models") or []:
            name = m.get("name") or m.get("model")
            size, vram = m.get("size") or 0, m.get("size_vram") or 0
            share = vram / size if size else 0.0
            # Ollama 0.3x (llama-server runner) reports only KV cache + buffers here, without the weights:
            # 1.3 GB for gemma4:12b that really takes ~9.2 GB. Add the weights file size back.
            if files is None:
                files = {x["name"]: x["gb"] * 1e9 for x in self.models()}
            fsize = next((v for k, v in files.items() if _model_matches(k, name)), 0)
            if size and fsize and size < fsize * 0.9:
                size += fsize
                vram = size * share
            out.append({"name": name, "gb": round(size / 1e9, 1), "vram_gb": round(vram / 1e9, 1),
                        "gpu": round(share, 2), "until": m.get("expires_at", ""), "ctx": m.get("context_length")})
        return out

    def is_loaded(self, name: str) -> dict | None:
        for m in self.loaded():
            if _model_matches(m["name"], name):
                return m
        return None

    def load(self, name: str, keep_alive_min: int = KEEP_ALIVE_MIN, timeout: float = 120.0) -> float:
        """Load into (V)RAM now; returns seconds. Same num_ctx as chat requests, so no reload later."""
        t0 = time.monotonic()
        r = self.s.post(self.base_url + "/api/generate",
                        json={"model": name, "prompt": "", "keep_alive": f"{int(keep_alive_min)}m",
                              "options": {"num_ctx": NUM_CTX}}, timeout=timeout)
        r.raise_for_status()
        return time.monotonic() - t0

    def unload(self, name: str) -> bool:
        try:
            r = self.s.post(self.base_url + "/api/generate", json={"model": name, "keep_alive": 0}, timeout=15)
            return r.status_code < 400
        except requests.RequestException:
            return False

    def unload_all(self) -> list[str]:
        names = [m["name"] for m in self.loaded()]
        for n in names:
            self.unload(n)
        return names

    def pull(self, name: str, on_progress=None) -> bool:
        """Download a model (resumable). on_progress(status, done, total)."""
        self._cancel_pull.clear()
        self.pulling = {"model": name, "status": "начинаю", "done": 0, "total": 0}
        try:
            with self.s.post(self.base_url + "/api/pull", json={"model": name, "stream": True},
                             timeout=(3, 120), stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if self._cancel_pull.is_set():
                        raise RuntimeError("отменено")
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("error"):
                        raise RuntimeError(str(d["error"])[:200])
                    st = str(d.get("status") or "")
                    if d.get("total"):
                        self.pulling.update(status=st, done=int(d.get("completed") or 0), total=int(d["total"]))
                    else:
                        self.pulling.update(status=st)
                    if on_progress:
                        on_progress(dict(self.pulling))
                    if st == "success":
                        return True
            return self.has_model(name)
        finally:
            self.pulling = None

    def cancel_pull(self) -> None:
        self._cancel_pull.set()


# ─── Games: give the VRAM back ───────────────────────────────────────────────

GAME_DIRS = ("\\steamapps\\common\\", "\\epic games\\", "\\riot games\\", "\\gog galaxy\\games\\", "\\xboxgames\\",
             "\\ea games\\", "\\electronic arts\\", "\\ubisoft game launcher\\games\\", "\\battle.net\\",
             "\\games\\", "\\steamlibrary\\", "\\windowsapps\\microsoft.minecraft")
NOT_GAMES = {"steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "wallpaper32.exe", "wallpaper64.exe",
             "riotclientservices.exe", "riotclientux.exe", "battle.net.exe", "galaxyclient.exe", "eadesktop.exe",
             "upc.exe", "ubisoftconnect.exe", "overwolf.exe", "jarvis.exe"}
# fullscreen but not a game: browsers, players, the desktop, office
FULLSCREEN_OK = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "yandex.exe",
                 "browser.exe", "vlc.exe", "mpc-hc64.exe", "mpc-be64.exe", "potplayermini64.exe", "mpv.exe",
                 "explorer.exe", "powerpnt.exe", "applicationframehost.exe", "telegram.exe", "discord.exe",
                 "spotify.exe", "obs64.exe", "code.exe", "windowsterminal.exe", "jarvis.exe", "python.exe",
                 "pythonw.exe", "searchhost.exe", "startmenuexperiencehost.exe", "lockapp.exe"}
GPU_APPS = {"blender.exe", "resolve.exe", "unrealeditor.exe", "unity.exe", "afterfx.exe", "adobe premiere pro.exe",
            "houdini.exe", "substance painter.exe", "comfyui.exe"}


def classify_app(process: str, path: str = "", fullscreen: bool = False) -> str | None:
    """'game' / 'gpu' / None for the foreground app."""
    p = (process or "").lower()
    if not p or p in NOT_GAMES:
        return None
    if p in GPU_APPS:
        return "gpu"
    full = (path or "").lower().replace("/", "\\")
    if full and any(d in full for d in GAME_DIRS):
        return "game"
    if fullscreen and p not in FULLSCREEN_OK:
        return "game"
    return None


class GameGuard:
    """Decides when the local model must give the GPU back: a game / heavy GPU app is in the foreground
    now, or was within `linger` seconds and is still running (alt-tab to Discord ≠ game over)."""

    def __init__(self, linger: float = 120.0) -> None:
        self.linger = linger
        self.active_app = ""      # process name of the game we are yielding to
        self.kind = ""
        self.last_seen = 0.0
        self._pid = 0

    def update(self, process: str, path: str, fullscreen: bool, pid: int, now: float, alive=None) -> bool:
        kind = classify_app(process, path, fullscreen)
        if kind:
            self.active_app, self.kind, self.last_seen, self._pid = process, kind, now, pid
            return True
        if self.active_app:
            still = alive(self._pid) if (alive and self._pid) else True
            if still and now - self.last_seen < self.linger:
                return True
            self.active_app, self.kind, self._pid = "", "", 0
        return False

    @property
    def active(self) -> bool:
        return bool(self.active_app)


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong(0)
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


# ─── Controller: routing local ↔ cloud, warm-up, games ──────────────────────

ROUTES = ("local_first", "local", "cloud")


class LocalController:
    """Owns the Ollama manager and decides, per request, local model vs cloud.

    route(kind) → provider names in order, e.g. ["ollama", "gemini"]:
    • «Локально, облако запасное» (local_first): local when Ollama runs, the model is downloaded and no game
      holds the GPU. A cold model (not in VRAM) is warmed in the background and this one interactive turn
      goes to the cloud — no 3–8 s wait. Background screen checks may wait for the load.
    • «Только локально» (local): always local (short keep-alive while a game runs).
    • «Только облако» (cloud): never local."""

    def __init__(self, get_settings, emit=None, foreground=None, manager: OllamaManager | None = None) -> None:
        self.get_settings = get_settings
        self.emit = emit or (lambda *a, **k: None)
        self.foreground = foreground  # () -> (process, path, fullscreen, pid)
        self.mgr = manager or OllamaManager()
        self.guard = GameGuard()
        self._lock = threading.Lock()
        self._state = {"installed": False, "running": False, "model_ok": False, "loaded": None, "models": [],
                       "game": "", "version": ""}
        self._warming = False
        self._unloaded_for_game = False
        self._last_poll = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.last_load_sec: float | None = None

    # ── settings ──
    def model(self) -> str:
        s = self.get_settings()
        return ((s.get("models") or {}).get("ollama") or {}).get("chat") or DEFAULT_MODEL

    def mode(self) -> str:
        r = self.get_settings().get("ai_route")
        return r if r in ROUTES else "local_first"

    def keep_alive(self) -> str:
        if self.guard.active and self.get_settings().get("gpu_free_in_games", True):
            return "20s"
        return f"{int(self.get_settings().get('local_keep_alive_min') or KEEP_ALIVE_MIN)}m"

    # ── state ──
    def refresh(self) -> dict:
        s = self.get_settings()
        self.mgr.set_url((s.get("base_urls") or {}).get("ollama") or DEFAULT_URL)
        running = self.mgr.running()
        models = self.mgr.models() if running else []
        m = self.model()
        loaded = None
        if running:
            for x in self.mgr.loaded():
                if _model_matches(x["name"], m):
                    loaded = x
        with self._lock:
            self._state.update(installed=self.mgr.installed() or running, running=running, version=self.mgr.version,
                               models=models, model_ok=any(_model_matches(x["name"], m) for x in models),
                               loaded=loaded, game=self.guard.active_app)
            self._last_poll = time.monotonic()
            return dict(self._state)

    def state(self) -> dict:
        with self._lock:
            st = dict(self._state)
        st.update(model=self.model(), mode=self.mode(), warming=self._warming, pulling=self.mgr.pulling,
                  error=self.last_error, keep_alive=self.keep_alive(), load_sec=self.last_load_sec,
                  gpu_free=bool(self.get_settings().get("gpu_free_in_games", True)), recommended=RECOMMENDED)
        return st

    def available(self) -> bool:
        with self._lock:
            return bool(self._state["running"] and self._state["model_ok"])

    def blocked(self) -> str:
        """Reason the local model must not be used right now ('' = free to use)."""
        if self.guard.active and self.get_settings().get("gpu_free_in_games", True):
            return self.guard.active_app
        return ""

    # ── routing ──
    def route(self, kind: str, cloud: str, cloud_ok: bool) -> list[str]:
        mode = self.mode()
        if mode == "cloud":
            return [cloud]
        if mode == "local":
            if not self.available():
                threading.Thread(target=self.start_server, daemon=True).start()
            return ["ollama"]
        out: list[str] = []
        if self.available() and not self.blocked():
            with self._lock:
                loaded = self._state["loaded"] is not None
            if loaded or not cloud_ok or kind in ("screen", "live"):
                out.append("ollama")
            else:
                self.warm_async()
        if cloud_ok or not out:
            out.append(cloud)
        return out

    def note_result(self, ok: bool, err: str = "") -> None:
        """After a local request: it's loaded now (or broken)."""
        self.last_error = "" if ok else err[:200]
        if ok:
            with self._lock:
                if self._state["loaded"] is None:
                    self._state["loaded"] = {"name": self.model(), "gb": 0, "vram_gb": 0, "gpu": 0, "until": ""}
            threading.Thread(target=self._refresh_emit, daemon=True).start()

    # ── actions ──
    def start_server(self) -> bool:
        ok = self.mgr.ensure_running()
        self._refresh_emit()
        return ok

    def warm_async(self) -> None:
        if self._warming or self.blocked() or self.mode() == "cloud":
            return
        self._warming = True

        def run():
            try:
                if not self.mgr.ensure_running():
                    return
                m = self.model()
                if not self.mgr.has_model(m):
                    return
                if self.mgr.is_loaded(m):
                    return
                self.emit_state()
                sec = self.mgr.load(m, int(self.get_settings().get("local_keep_alive_min") or KEEP_ALIVE_MIN))
                self.last_load_sec = round(sec, 1)
                log.info("local model %s loaded in %.1fs", m, sec)
            except Exception as e:
                self.last_error = f"не загрузилась: {e}"[:200]
                log.warning("local model warm-up failed: %s", e)
            finally:
                self._warming = False
                self._refresh_emit()

        threading.Thread(target=run, name="ollama-warm", daemon=True).start()

    def unload(self) -> None:
        names = self.mgr.unload_all()
        if names:
            log.info("local model unloaded: %s", names)
        self._refresh_emit()

    def pull_async(self, name: str) -> bool:
        if self.mgr.pulling:
            return False

        def run():
            last = [0.0]

            def prog(p):
                now = time.monotonic()
                if now - last[0] > 0.4 or p.get("status") == "success":
                    last[0] = now
                    self.emit("local_pull", **p)
            try:
                if not self.mgr.ensure_running():
                    raise RuntimeError("Ollama не запущена")
                ok = self.mgr.pull(name, prog)
                self.emit("local_pull", model=name, status="success" if ok else "error", done=1, total=1,
                          error="" if ok else "не скачалась")
            except Exception as e:
                log.warning("pull %s failed: %s", name, e)
                self.emit("local_pull", model=name, status="error", done=0, total=0, error=str(e)[:200])
            finally:
                self._refresh_emit()

        threading.Thread(target=run, name="ollama-pull", daemon=True).start()
        return True

    def settings_changed(self, old: dict) -> None:
        """Route / model / address changed in the UI."""
        s = self.get_settings()
        old_model = ((old.get("models") or {}).get("ollama") or {}).get("chat")
        if s.get("ai_route") == "cloud":
            self.unload()
            return
        if old_model and old_model != self.model() and self.mgr.running():
            self.mgr.unload(old_model)
        self.mgr.ensure_running()
        self.refresh()
        if self.available() and not self.blocked():
            self.warm_async()
        self.emit_state()

    def shutdown(self) -> None:
        """App exit: give the VRAM back now; stop the server only if we started it."""
        try:
            if self.mgr.running():
                self.mgr.unload(self.model())
        except Exception:
            pass
        p = self.mgr._proc
        if p is not None and p.poll() is None:
            # the model runs in a child llama-server: terminating only `ollama serve` would orphan it with the VRAM
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=10,
                                   creationflags=0x08000000)
                else:
                    p.terminate()
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass

    # ── background loop: games, status ──
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="local-ai", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self.mode() != "cloud" and self.mgr.installed():
            self.mgr.ensure_running()
        self._refresh_emit()
        if self.mode() != "cloud" and self.available() and not self.blocked():
            self.warm_async()  # first answer without the 3–8 s load
        while not self._stop.wait(2.0):
            try:
                self.tick()
            except Exception:
                log.exception("local-ai tick failed")

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        was = self.guard.active
        if self.foreground:
            proc, path, full, pid = self.foreground()
            self.guard.update(proc, path, full, pid, now, alive=pid_alive)
        s = self.get_settings()
        if self.guard.active != was:
            log.info("game guard: %s", f"{self.guard.kind} «{self.guard.active_app}» — GPU goes to it"
                     if self.guard.active else "game over — local model may load again")
            if self.guard.active and s.get("gpu_free_in_games", True):
                with self._lock:
                    had = self._state["loaded"] is not None
                if had or self.mgr.running():
                    threading.Thread(target=self.unload, daemon=True).start()
                    self._unloaded_for_game = True
            elif not self.guard.active:
                if self._unloaded_for_game and self.mode() != "cloud":
                    self.warm_async()  # «reload when back»
                self._unloaded_for_game = False
            self._refresh_emit()
        elif now - self._last_poll > 15:
            self._refresh_emit()

    def _refresh_emit(self) -> None:
        try:
            self.refresh()
        except Exception:
            pass
        self.emit_state()

    def emit_state(self) -> None:
        self.emit("local", **self.state())
