"""PC actions exposed to the model via function calling.

Only non-destructive actions: open apps / URLs / files, search, find files,
focus-session control, reminders, time, a look at the screen. Nothing deletes,
closes or sends anything.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta

log = logging.getLogger("jarvis")
IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

WEEKDAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS_GEN = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")

# ─── Tool schemas (provider-neutral JSON schema) ─────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "open_app",
        "description": "Открыть приложение на компьютере пользователя (ищет в меню Пуск, на рабочем столе и среди встроенных программ). "
                       "Передавай официальное название программы, обычно латиницей (Telegram, Google Chrome, Visual Studio Code, Spotify, Discord, Steam, Word, Excel), "
                       "или системное (калькулятор, блокнот, проводник, параметры, диспетчер задач).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Название приложения"}},
            "required": ["name"]},
    },
    {
        "name": "open_url",
        "description": "Открыть сайт или ссылку в браузере по умолчанию.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Адрес, например youtube.com или https://..."}},
            "required": ["url"]},
    },
    {
        "name": "web_search",
        "description": "Открыть поиск в браузере (Google по умолчанию, или YouTube / Википедия).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "engine": {"type": "string", "enum": ["google", "youtube", "wikipedia", "yandex"]}},
            "required": ["query"]},
    },
    {
        "name": "find_files",
        "description": "Найти файлы или папки по части имени в папках пользователя (Рабочий стол, Документы, Загрузки, Изображения, Видео, Музыка, OneDrive). "
                       "Возвращает до 10 путей, новые первыми.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Часть имени файла, например «физика» или «.pdf» или «отчёт 2026»"},
            "open_first": {"type": "boolean", "description": "Сразу открыть самый подходящий найденный файл"}},
            "required": ["query"]},
    },
    {
        "name": "open_path",
        "description": "Открыть файл или папку по полному пути (обычно путь из find_files).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "start_focus",
        "description": "Начать фокус-сессию: пользователь сказал, над чем работает и сколько времени. "
                       "Ассистент будет следить за экраном и возвращать к задаче.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "Короткое описание задачи, например «домашка по физике»"},
            "minutes": {"type": "integer", "description": "Длительность одного блока фокуса в минутах (по умолчанию из настроек)"},
            "rounds": {"type": "integer", "description": "Сколько блоков фокуса с перерывами между ними (по умолчанию 1)"}},
            "required": ["task"]},
    },
    {
        "name": "stop_focus",
        "description": "Завершить текущую фокус-сессию.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "pause_focus",
        "description": "Поставить фокус-сессию на паузу или продолжить её.",
        "parameters": {"type": "object", "properties": {
            "resume": {"type": "boolean", "description": "true — продолжить, false — пауза"}},
            "required": ["resume"]},
    },
    {
        "name": "focus_status",
        "description": "Узнать состояние фокус-сессии и статистику фокуса за сегодня.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "set_reminder",
        "description": "Поставить таймер или напоминание. Укажи либо minutes (через сколько минут), либо at_time (ЧЧ:ММ сегодня/завтра).",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "О чём напомнить"},
            "minutes": {"type": "number", "description": "Через сколько минут"},
            "at_time": {"type": "string", "description": "Время в формате ЧЧ:ММ"}},
            "required": ["text"]},
    },
    {
        "name": "list_reminders",
        "description": "Показать активные напоминания и таймеры.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminders",
        "description": "Отменить все активные напоминания и таймеры.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_time",
        "description": "Текущие дата, день недели и время.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "look_at_screen",
        "description": "Посмотреть на экран пользователя (скриншот активного монитора) и ответить на вопрос о том, что там видно.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "Что нужно понять по экрану"}},
            "required": ["question"]},
    },
]

# ─── Helpers ─────────────────────────────────────────────────────────────────

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

APP_ALIASES = {
    "калькулятор": "calc.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "блокнот": "notepad.exe", "notepad": "notepad.exe",
    "проводник": "explorer.exe", "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "paint": "mspaint.exe", "пэйнт": "mspaint.exe", "паинт": "mspaint.exe",
    "диспетчер задач": "taskmgr.exe", "task manager": "taskmgr.exe",
    "параметры": "ms-settings:", "настройки windows": "ms-settings:", "settings": "ms-settings:",
    "командная строка": "cmd.exe", "cmd": "cmd.exe", "powershell": "powershell.exe",
    "терминал": "wt.exe", "terminal": "wt.exe", "windows terminal": "wt.exe",
    "ножницы": "snippingtool.exe", "snipping tool": "snippingtool.exe",
    "хром": "google chrome", "гугл хром": "google chrome", "chrome": "google chrome",
    "ворд": "word", "эксель": "excel", "поверпоинт": "powerpoint", "павер поинт": "powerpoint",
    "вскод": "visual studio code", "vs code": "visual studio code", "vscode": "visual studio code",
    "телега": "telegram", "телеграм": "telegram", "телеграмм": "telegram",
    "дискорд": "discord", "стим": "steam", "спотифай": "spotify", "обс": "obs studio",
    "яндекс браузер": "yandex", "яндекс": "yandex", "фаерфокс": "firefox", "файрфокс": "firefox",
    "эдж": "microsoft edge", "edge": "microsoft edge", "опера": "opera", "ватсап": "whatsapp",
    "зум": "zoom", "ноушен": "notion", "обсидиан": "obsidian",
    "майнкрафт": "minecraft launcher", "брейв": "brave", "фотошоп": "photoshop",
}


def translit(s: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in s.lower())


def norm(s: str) -> str:
    s = translit(s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def score_name(query: str, name: str) -> float:
    """0..1 fuzzy match of an app name query against a shortcut name."""
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if n.startswith(q) or q.startswith(n):
        return 0.92
    if re.search(r"\b" + re.escape(q) + r"\b", n):
        return 0.88
    if q in n:
        return 0.8
    ratio = difflib.SequenceMatcher(None, q, n).ratio()
    # also compare against each word of the name
    best_word = max((difflib.SequenceMatcher(None, q, w).ratio() for w in n.split()), default=0.0)
    return max(ratio, best_word * 0.9)


def user_folders() -> list[str]:
    home = os.path.expanduser("~")
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"]
    out = [os.path.join(home, n) for n in names]
    for env in ("OneDrive", "OneDriveConsumer"):
        od = os.environ.get(env)
        if od:
            out.append(od)
    seen, res = set(), []
    for p in out:
        rp = os.path.normcase(os.path.abspath(p))
        if rp not in seen and os.path.isdir(p):
            seen.add(rp)
            res.append(p)
    return res


SKIP_DIRS = {"node_modules", ".git", "__pycache__", "appdata", ".venv", "venv", "$recycle.bin",
             "site-packages", ".cache", "build", "dist"}


def find_files(query: str, roots: list[str] | None = None, *, limit: int = 10,
               time_budget: float = 4.0, max_depth: int = 7) -> list[str]:
    q = (query or "").strip().lower()
    if not q:
        return []
    q_alt = translit(q)
    roots = roots if roots is not None else user_folders()
    hits: list[tuple[float, str]] = []
    t0 = time.monotonic()
    for root in roots:
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() - t0 > time_budget:
                break
            depth = dirpath.count(os.sep) - base_depth
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")
                           and depth < max_depth]
            for name in dirnames + filenames:
                low = name.lower()
                if q in low or (q_alt != q and q_alt in translit(low)):
                    full = os.path.join(dirpath, name)
                    try:
                        mt = os.path.getmtime(full)
                    except OSError:
                        mt = 0.0
                    hits.append((mt, full))
    hits.sort(reverse=True)
    return [p for _mt, p in hits[:limit]]


BLOCKED_EXT = {".bat", ".cmd", ".ps1", ".vbs", ".js", ".reg", ".msi", ".scr", ".com", ".jse", ".wsf"}


def _startfile(target: str) -> None:
    if IS_WIN:
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_hidden(args: list[str], timeout: float = 20.0) -> str:
    """Run a helper process with no console window; return stdout."""
    kw: dict = {"capture_output": True, "timeout": timeout}
    if IS_WIN:
        kw["creationflags"] = NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kw["startupinfo"] = si
    r = subprocess.run(args, **kw)
    for enc in ("utf-8", "cp1251", "cp866"):
        try:
            return r.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.stdout.decode("utf-8", errors="replace")


class AppIndex:
    """Start-menu / desktop shortcuts + UWP apps (Get-StartApps), cached."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.shortcuts: dict[str, str] = {}
        self.uwp: dict[str, str] = {}
        self._built = 0.0

    def build(self, force: bool = False) -> None:
        with self._lock:
            if not force and self._built and time.time() - self._built < 600:
                return
            sc: dict[str, str] = {}
            dirs = []
            appdata = os.environ.get("APPDATA")
            progdata = os.environ.get("PROGRAMDATA")
            if appdata:
                dirs.append(os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"))
            if progdata:
                dirs.append(os.path.join(progdata, "Microsoft", "Windows", "Start Menu", "Programs"))
            dirs.append(os.path.join(os.path.expanduser("~"), "Desktop"))
            pub = os.environ.get("PUBLIC")
            if pub:
                dirs.append(os.path.join(pub, "Desktop"))
            for d in dirs:
                if not os.path.isdir(d):
                    continue
                for dp, _dn, fns in os.walk(d):
                    for fn in fns:
                        stem, ext = os.path.splitext(fn)
                        if ext.lower() in (".lnk", ".url", ".appref-ms"):
                            low = stem.lower()
                            if "uninstall" in low or "deinstall" in low or "удал" in low or "деинстал" in low:
                                continue
                            sc.setdefault(stem, os.path.join(dp, fn))
            self.shortcuts = sc
            if IS_WIN and not self.uwp:
                try:
                    out = run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                      "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                                      "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
                                     timeout=25)
                    uwp = {}
                    for line in out.splitlines():
                        if "|" in line:
                            n, aid = line.rsplit("|", 1)
                            if n.strip() and aid.strip():
                                uwp[n.strip()] = aid.strip()
                    self.uwp = uwp
                except Exception as e:
                    log.info("Get-StartApps failed: %s", e)
            self._built = time.time()
            log.info("app index: %d shortcuts, %d start apps", len(self.shortcuts), len(self.uwp))

    def best(self, query: str) -> tuple[float, str, str, str] | None:
        """(score, display_name, kind, target)"""
        self.build()
        best: tuple[float, str, str, str] | None = None
        for name, path in self.shortcuts.items():
            s = score_name(query, name)
            if best is None or s > best[0]:
                best = (s, name, "lnk", path)
        for name, aid in self.uwp.items():
            s = score_name(query, name) - 0.01  # prefer real shortcuts on ties
            if best is None or s > best[0]:
                best = (s, name, "uwp", aid)
        return best

    def suggestions(self, query: str, n: int = 5) -> list[str]:
        names = list(self.shortcuts) + list(self.uwp)
        names.sort(key=lambda x: score_name(query, x), reverse=True)
        return names[:n]


# ─── Actions ─────────────────────────────────────────────────────────────────

class Actions:
    def __init__(self, core) -> None:
        self.core = core
        self.apps = AppIndex()
        threading.Thread(target=self._prebuild, name="app-index", daemon=True).start()

    def _prebuild(self) -> None:
        try:
            self.apps.build()
        except Exception:
            log.exception("app index build failed")

    def execute(self, name: str, args: dict) -> dict:
        fn = getattr(self, f"do_{name}", None)
        if fn is None:
            return {"ok": False, "error": f"неизвестное действие {name}"}
        try:
            res = fn(**(args or {}))
        except TypeError as e:
            res = {"ok": False, "error": f"неверные параметры: {e}"}
        except Exception as e:
            log.exception("action %s failed", name)
            res = {"ok": False, "error": str(e)[:200]}
        log.info("action %s(%s) -> %s", name, _short(args), _short(res))
        return res

    # apps / web / files
    def resolve_app(self, name: str) -> tuple[str, str, str] | None:
        """(kind, display, target) without launching. kind: exe | lnk | uwp | path."""
        q = (name or "").strip()
        if not q:
            return None
        alias = APP_ALIASES.get(q.lower()) or APP_ALIASES.get(norm(q))
        if alias and (alias.endswith(".exe") or alias.endswith(":")):
            return ("exe", q, alias)
        query = alias or q
        best = self.apps.best(query)
        if best and best[0] >= 0.72:
            _score, disp, kind, target = best
            return (kind, disp, target)
        if alias is None and query.lower() != norm(query):
            # try transliterated query too ("телеграм" -> "telegram")
            best = self.apps.best(norm(query))
            if best and best[0] >= 0.8:
                _score, disp, kind, target = best
                return (kind, disp, target)
        exe = query if query.lower().endswith(".exe") else query.replace(" ", "") + ".exe"
        if re.fullmatch(r"[A-Za-z0-9_.\-]+\.exe", exe):
            return ("path", exe, exe)
        return None

    def do_open_app(self, name: str) -> dict:
        q = (name or "").strip()
        if not q:
            return {"ok": False, "error": "пустое имя"}
        r = self.resolve_app(q)
        try:
            if r:
                kind, disp, target = r
                if kind == "uwp":
                    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"],
                                     creationflags=NO_WINDOW)
                else:
                    _startfile(target)
                return {"ok": True, "opened": disp}
        except OSError as e:
            log.info("open_app %r failed: %s", q, e)
        return {"ok": False, "error": "не нашёл такое приложение",
                "похожие": self.apps.suggestions(APP_ALIASES.get(q.lower()) or q)}

    def do_open_url(self, url: str) -> dict:
        u = (url or "").strip()
        if not u:
            return {"ok": False, "error": "пустой адрес"}
        if not re.match(r"^https?://", u, re.IGNORECASE):
            if re.match(r"^[a-z]+:", u, re.IGNORECASE):
                return {"ok": False, "error": "разрешены только http/https ссылки"}
            u = "https://" + u
        webbrowser.open(u)
        return {"ok": True, "opened": u}

    def do_web_search(self, query: str, engine: str = "google") -> dict:
        q = urllib.parse.quote_plus((query or "").strip())
        urls = {
            "google": f"https://www.google.com/search?q={q}",
            "youtube": f"https://www.youtube.com/results?search_query={q}",
            "wikipedia": f"https://ru.wikipedia.org/w/index.php?search={q}",
            "yandex": f"https://yandex.ru/search/?text={q}",
        }
        url = urls.get((engine or "google").lower(), urls["google"])
        webbrowser.open(url)
        return {"ok": True, "opened": url}

    def do_find_files(self, query: str, open_first: bool = False) -> dict:
        found = find_files(query)
        res: dict = {"ok": True, "count": len(found), "files": found}
        if open_first and found:
            r = self.do_open_path(found[0])
            res["opened"] = found[0] if r.get("ok") else None
        if not found:
            res["note"] = "ничего не найдено в папках пользователя"
        return res

    def do_open_path(self, path: str) -> dict:
        p = os.path.expandvars(os.path.expanduser((path or "").strip().strip('"')))
        if not p or not os.path.exists(p):
            return {"ok": False, "error": "такого пути нет"}
        if os.path.splitext(p)[1].lower() in BLOCKED_EXT:
            return {"ok": False, "error": "скрипты и установщики я не запускаю из соображений безопасности"}
        _startfile(p)
        return {"ok": True, "opened": p}

    # focus
    def do_start_focus(self, task: str, minutes: int | None = None, rounds: int | None = None) -> dict:
        return self.core.focus_start(task, minutes, rounds)

    def do_stop_focus(self) -> dict:
        return self.core.focus_stop()

    def do_pause_focus(self, resume: bool = False) -> dict:
        return self.core.focus_pause(resume=bool(resume))

    def do_focus_status(self) -> dict:
        return self.core.focus_status()

    # reminders
    def do_set_reminder(self, text: str, minutes: float | None = None, at_time: str | None = None) -> dict:
        now = datetime.now()
        due: datetime | None = None
        if minutes is not None:
            try:
                m = float(minutes)
            except (TypeError, ValueError):
                return {"ok": False, "error": "неверное число минут"}
            if m <= 0 or m > 24 * 60:
                return {"ok": False, "error": "от 1 секунды до 24 часов"}
            due = now + timedelta(minutes=m)
        elif at_time:
            mt = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", str(at_time))
            if not mt:
                return {"ok": False, "error": "время нужно в формате ЧЧ:ММ"}
            hh, mm = int(mt.group(1)), int(mt.group(2))
            if hh > 23 or mm > 59:
                return {"ok": False, "error": "неверное время"}
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
        else:
            return {"ok": False, "error": "укажи minutes или at_time"}
        return self.core.add_reminder(text or "таймер", due)

    def do_list_reminders(self) -> dict:
        return self.core.list_reminders()

    def do_cancel_reminders(self) -> dict:
        return self.core.cancel_reminders()

    def do_get_time(self) -> dict:
        return time_info()

    def do_look_at_screen(self, question: str = "Что на экране?") -> dict:
        return self.core.look_at_screen(question)


def time_info(now: datetime | None = None) -> dict:
    n = now or datetime.now()
    return {
        "time": n.strftime("%H:%M"),
        "date": f"{n.day} {MONTHS_GEN[n.month]} {n.year}",
        "weekday": WEEKDAYS_FULL[n.weekday()],
    }


def _short(x: object, n: int = 300) -> str:
    s = str(x)
    return s if len(s) <= n else s[:n] + "…"
