"""pywebview (Edge WebView2) UI: frameless window, HTML/CSS/JS front-end in /web,
JS bridge to JarvisCore, tray, close-to-tray, event push."""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime

from . import APP_TITLE, APP_VERSION, persona, updater
from .actions import TOOLS
from .config import (DATA_DIR, PROVIDERS, VOICES, api_key_for, bundle_dir, ensure_dir, normalize_settings)
from .core import JarvisCore
from .focus import format_days, format_duration, phase_label
from .hotkey import validate_hotkey
from .tray import Tray
from .winutil import get_autostart, set_autostart
from .winwin import FramelessWindow

log = logging.getLogger("jarvis")

CHAT_PATH = os.path.join(DATA_DIR, "chat.json")
WEB_DIR = os.path.join(bundle_dir(), "web")
WIN_W, WIN_H, MIN_W, MIN_H = 1024, 650, 780, 540

TOOL_RU = {
    "open_app": "открываю приложение", "open_url": "открываю сайт", "web_search": "ищу в интернете",
    "find_files": "ищу файлы", "open_path": "открываю файл", "start_focus": "запускаю фокус",
    "stop_focus": "останавливаю фокус", "pause_focus": "пауза фокуса", "focus_status": "смотрю статус",
    "set_reminder": "ставлю напоминание", "list_reminders": "смотрю напоминания",
    "cancel_reminders": "отменяю напоминания", "get_time": "смотрю время", "look_at_screen": "смотрю на экран",
}

ABILITIES = [
    ("app", "Открыть программу", "Ищет в меню «Пуск» и на рабочем столе", "Открой телеграм"),
    ("globe", "Открыть сайт", "Любая http/https-ссылка в браузере", "Открой ютуб"),
    ("search", "Поиск в интернете", "Google, YouTube и другие", "Найди в интернете рецепт блинов"),
    ("file", "Найти файл", "Ищет по имени в Рабочем столе, Документах, Загрузках…", "Найди файл реферат"),
    ("target", "Фокус-сессия", "Таймер + страж от TikTok и шортсов", "Я делаю домашку по физике 40 минут"),
    ("bell", "Напоминания и таймеры", "Через N минут или в ЧЧ:ММ", "Напомни через 15 минут выпить воды"),
    ("monitor", "Посмотреть на экран", "Скриншот активного монитора → ответ", "Что у меня на экране?"),
    ("clock", "Время и дата", "", "Сколько сейчас времени?"),
]

DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def hotkey_pretty(combo: str) -> str:
    return "+".join(p.strip().capitalize() for p in combo.split("+"))


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class ChatLog:
    """Last chat, persisted so «Последний чат» survives restarts."""

    def __init__(self, path: str = CHAT_PATH, limit: int = 300) -> None:
        self.path, self.limit = path, limit
        self.items: list[dict] = []
        self._lock = threading.Lock()
        self._dirty = False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.items = [x for x in data if isinstance(x, dict)][-limit:]
        except (OSError, ValueError):
            pass

    def add(self, item: dict) -> None:
        with self._lock:
            self.items.append(item)
            del self.items[:-self.limit]
            self._dirty = True

    def clear(self) -> None:
        with self._lock:
            self.items = []
            self._dirty = True
        self.flush()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.items)

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            data = list(self.items)
            self._dirty = False
        try:
            ensure_dir(DATA_DIR)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("chat save failed: %s", e)


class Bridge:
    """Methods exposed to JS as window.pywebview.api.*  (attributes starting with _ are hidden)."""

    def __init__(self, start_minimized: bool = False) -> None:
        self._start_minimized = start_minimized
        self._q: queue.Queue = queue.Queue(maxsize=2000)
        self._ready = threading.Event()
        self._visible = not start_minimized
        self._quitting = False
        self._window = None
        self._frame = FramelessWindow(MIN_W, MIN_H)
        self._chat = ChatLog()
        self._releases: dict = {}
        self._installing = False
        self._tray_hint_shown = False
        self._core = JarvisCore(self._emit)
        self._tray = Tray(persona.assistant_name(self._core.settings), on_show=self.show, on_listen=self.listen,
                          on_focus_stop=self.focus_stop, on_quit=self.quit)

    # ───────────────────────── core → JS ─────────────────────────
    def _emit(self, event: str, **data) -> None:  # any thread
        if event == "chat":
            self._chat.add({"role": data.get("role"), "text": data.get("text", ""),
                            "kind": data.get("kind", ""), "ts": datetime.now().strftime("%H:%M")})
        elif event == "tool":
            data = {"name": data.get("name"), "label": TOOL_RU.get(data.get("name", ""), data.get("name", ""))}
            self._chat.add({"role": "tool", "text": data["label"], "ts": datetime.now().strftime("%H:%M")})
        elif event == "state":
            st = data.get("state", "")
            self._tray.set_tooltip(f"{self._name()} — " + {"idle": "готов", "listening": "слушаю",
                                                        "thinking": "думаю", "speaking": "говорю"}.get(st, st))
        elif event == "notify":
            if not self._visible:
                self._tray.notify(data.get("text", ""))
        elif event in ("focus", "guard", "tick"):
            data = {"focus": self._focus_payload()}
            event = "focus"
        try:
            self._q.put_nowait((event, data))
        except queue.Full:
            pass

    def _pump(self) -> None:
        self._ready.wait()
        while not self._quitting:
            try:
                first = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = [first]
            try:
                while len(batch) < 60:
                    batch.append(self._q.get_nowait())
            except queue.Empty:
                pass
            # collapse noisy mic-level events to the latest one
            levels = [b for b in batch if b[0] == "level"]
            if len(levels) > 1:
                last = levels[-1]
                batch = [b for b in batch if b[0] != "level"] + [last]
            payload = json.dumps([{"e": e, "d": d} for e, d in batch], ensure_ascii=False, default=str)
            try:
                self._window.evaluate_js(f"window.J && J.onEvents({payload})")
            except Exception as ex:
                if not self._quitting:
                    log.debug("evaluate_js failed: %s", ex)

    def _ticker(self) -> None:
        n = 0
        while not self._quitting:
            time.sleep(1.0)
            n += 1
            if self._visible and self._core.session.active:
                self._emit("tick")
            if n % 10 == 0:
                self._chat.flush()

    # ───────────────────────── payloads ─────────────────────────
    def _focus_payload(self) -> dict:
        c = self._core
        snap = c.session.snapshot()
        tot = c.session.phase_total() if c.session.active else snap["focus_min"] * 60
        return {
            "active": c.session.active, "task": snap["task"], "phase": snap["phase"],
            "phase_label": phase_label(snap["phase"]) if c.session.active else "",
            "running": snap["running"], "remaining": int(snap["remaining"]), "total": int(tot or 0),
            "round": snap["round"], "rounds": snap["rounds"], "guard": c.guard_status,
            "today_min": c.stats.today() // 60, "distractions": c.stats.distractions_today(),
        }

    def _stats_payload(self) -> dict:
        st = self._core.stats
        days = [{"label": DAYS_RU[d.weekday()], "date": d.strftime("%d.%m"), "min": sec // 60}
                for d, sec in st.last_days(7)]
        return {"today": format_duration(st.today()), "week": format_duration(st.week()),
                "month": format_duration(st.month()), "total": format_duration(st.total()),
                "streak": format_days(st.streak()), "distractions": st.distractions_today(), "days": days}

    def _settings_payload(self) -> dict:
        s = copy.deepcopy(self._core.settings)
        s.pop("github_token", None)
        return s

    def _name(self) -> str:
        return persona.assistant_name(self._core.settings)

    def _apply_name(self) -> None:
        name = self._name()
        try:
            if self._window is not None:
                self._window.set_title(name)
        except Exception as e:
            log.info("set_title failed: %s", e)
        self._tray.set_tooltip(f"{name} — готов")
        log.info("assistant name: %s", name)

    # ───────────────────────── JS API ─────────────────────────
    def init(self) -> dict:
        c = self._core
        s = c.settings
        prov = s["provider"]
        data = {
            "version": APP_VERSION, "settings": self._settings_payload(),
            "providers": {k: {"label": v["label"], "chat_models": v["chat_models"], "lite_models": v["lite_models"],
                              "key_url": v["key_url"], "base_url": v["base_url"]} for k, v in PROVIDERS.items()},
            "voices": VOICES, "abilities": [dict(zip(("icon", "title", "desc", "example"), a)) for a in ABILITIES],
            "tools": [{"name": t["name"], "label": TOOL_RU.get(t["name"], t["name"]), "desc": t["description"]}
                      for t in TOOLS],
            "chat": self._chat.snapshot(), "focus": self._focus_payload(), "stats": self._stats_payload(),
            "state": c.state, "hotkey": hotkey_pretty(s["hotkey"]), "autostart": get_autostart() or s["autostart"],
            "wake": {"enabled": bool(c.wake), "error": None}, "maximized": self._frame.maximized,
            "has_key": bool(api_key_for(s, prov)), "data_dir": DATA_DIR,
            "presets": persona.presets_payload(),
        }
        self._ready.set()
        log.info("UI connected (js init)")
        auto = os.environ.get("JARVIS_AUTOTEST", "")
        if auto:  # dev/test hook: drive the real UI path (set only by the test launcher)
            def go() -> None:
                time.sleep(2.5)
                for cmd in auto.split(";;"):  # several steps: "tab:ai;;type:#a-aname=Пятница"
                    self._window.evaluate_js(f"J.autotest({json.dumps(cmd, ensure_ascii=False)})")
                    time.sleep(2.0)
            threading.Thread(target=go, daemon=True).start()
        return data

    def log_js(self, msg: str) -> None:
        log.warning("js: %s", str(msg)[:500])

    def send(self, text: str, attach_screen: bool = False) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if attach_screen:
            t = "(Посмотри на мой экран через look_at_screen и учти, что там.) " + t
        self._core.submit_text(t)
        return True

    def listen(self) -> None:
        self._core.toggle_listen()

    def stop_speaking(self) -> None:
        self._core.speaker.stop()

    def new_chat(self) -> None:
        self._chat.clear()
        self._core.brain.reset()

    def get_chat(self) -> list:
        return self._chat.snapshot()

    def save(self, patch: dict) -> dict:
        patch = patch or {}
        if "hotkey" in patch:
            hk = str(patch["hotkey"] or "").strip().lower()
            if not validate_hotkey(hk):
                return {"ok": False, "error": f"Не понимаю сочетание «{hk}»"}
            patch["hotkey"] = hk
        new = normalize_settings(_deep_merge(self._core.settings, patch))
        old_top = self._core.settings.get("always_on_top")
        old_name = self._name()
        self._core.apply_settings(new)
        if self._name() != old_name:
            self._apply_name()
        if "autostart" in patch:
            set_autostart(bool(new["autostart"]))
        if self._window is not None and new.get("always_on_top") != old_top:
            try:
                self._window.on_top = bool(new["always_on_top"])
            except Exception:
                pass
        s = self._core.settings
        return {"ok": True, "settings": self._settings_payload(), "hotkey": hotkey_pretty(s["hotkey"]),
                "has_key": bool(api_key_for(s, s["provider"]))}

    def set_wake(self, on: bool) -> dict:
        r = self.save({"wake_word": bool(on)})
        r["wake"] = {"enabled": bool(self._core.wake)}
        return r

    def test_ai(self, provider: str, patch: dict | None = None) -> str:
        from .brain import Brain
        tmp = normalize_settings(_deep_merge(self._core.settings, patch or {}))
        try:
            return Brain(lambda: tmp, None).test_connection(provider or tmp["provider"])
        except Exception as e:
            from .providers import friendly_error
            return friendly_error(e)

    def test_voice(self, voice: str = "", rate: int | None = None, volume: int | None = None) -> None:
        sp = self._core.speaker
        tmp = copy.deepcopy(self._core.settings)
        if voice:
            tmp["voice"] = voice
        if rate is not None:
            tmp["tts_rate"] = int(rate)
        if volume is not None:
            tmp["tts_volume"] = int(volume)
        sp.get_settings = lambda: tmp
        user = str(self._core.settings.get("user_name") or "").strip()
        self._core.say(f"Привет{', ' + user if user else ''}. Я {self._name()}. Так звучит мой голос.", interrupt=True)

        def restore() -> None:
            time.sleep(10)
            sp.get_settings = lambda: self._core.settings
        threading.Thread(target=restore, daemon=True).start()

    # focus
    def focus_start(self, task: str, minutes=None, rounds=None, break_minutes=None) -> dict:
        if break_minutes:
            self.save({"break_minutes": int(break_minutes)})
        r = self._core.focus_start((task or "").strip() or "работа", minutes, rounds)
        return {"ok": True, "focus": self._focus_payload(), **{k: v for k, v in r.items() if k != "ok"}}

    def focus_stop(self) -> dict:
        self._core.focus_stop()
        return self._focus_payload()

    def focus_pause(self) -> dict:
        if self._core.session.active:
            self._core.focus_toggle_pause()
        return self._focus_payload()

    def focus_skip(self) -> dict:
        if self._core.session.active:
            self._core.focus_skip()
        return self._focus_payload()

    def focus_state(self) -> dict:
        return self._focus_payload()

    def stats(self) -> dict:
        return self._stats_payload()

    # misc
    def open_data_folder(self) -> None:
        try:
            ensure_dir(DATA_DIR)
            if sys.platform == "win32":
                os.startfile(DATA_DIR)  # type: ignore[attr-defined]
        except OSError:
            pass

    def open_link(self, url: str) -> None:
        if isinstance(url, str) and url.startswith("https://"):
            import webbrowser
            webbrowser.open(url)

    # versions (GitHub Releases of Sinohara1/jarvis-releases)
    def list_versions(self, force: bool = False) -> dict:
        try:
            rels = updater.list_releases(force=bool(force))
            self._releases = {r["tag"]: r for r in rels}
            return {"ok": True, "current": APP_VERSION, "releases": rels, "repo": updater.REPO_URL,
                    "frozen": bool(getattr(sys, "frozen", False))}
        except Exception as e:
            log.info("list_versions failed: %s", e)
            msg = str(e)
            if "403" in msg or "rate limit" in msg.lower():
                msg = "GitHub временно ограничил запросы — попробуй через несколько минут."
            elif "urlopen error" in msg or "timed out" in msg or "getaddrinfo" in msg:
                msg = "Нет связи с GitHub — проверь интернет."
            return {"ok": False, "current": APP_VERSION, "error": msg, "repo": updater.REPO_URL}

    def install_version(self, tag: str) -> dict:
        rel = getattr(self, "_releases", {}).get(tag)
        if not rel:
            return {"ok": False, "error": "Версия не найдена — обнови список."}
        if rel["status"] == "current":
            return {"ok": False, "error": "Эта версия уже установлена."}
        if not getattr(sys, "frozen", False):
            return {"ok": False, "error": "Установка версий работает только в Jarvis.exe."}
        if getattr(self, "_installing", False):
            return {"ok": False, "error": "Уже устанавливаю…"}
        self._installing = True

        def work():
            try:
                updater.install(rel, lambda d, t: self._emit("upd_progress", tag=tag, done=d, total=t))
                self._emit("upd_ready", tag=tag)
                time.sleep(1.2)
                self.quit()
            except Exception as e:
                log.exception("install_version failed")
                self._installing = False
                self._emit("upd_error", tag=tag, error=str(e))

        threading.Thread(target=work, name="install-version", daemon=True).start()
        return {"ok": True}

    # window
    def win_minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def win_toggle_max(self) -> bool:
        m = self._frame.toggle_maximize()
        return m

    def win_close(self) -> None:
        if self._core.settings.get("close_to_tray", True) and self._tray.ok:
            self.hide()
        else:
            self.quit()

    def win_begin_move(self) -> None:
        self._frame.begin_move(on_unmax=lambda: self._q.put_nowait(("window", {"maximized": False})))

    def win_begin_resize(self, edge: str) -> None:
        self._frame.begin_resize(str(edge or "se"))

    def hide(self) -> None:
        if self._window:
            self._window.hide()
            self._set_visible(False)
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._tray.notify("Я в трее и продолжаю следить за фокусом. Выход — через меню значка.")

    def show(self) -> None:
        if self._window:
            self._window.show()
            try:
                self._window.restore()
            except Exception:
                pass
            self._frame.foreground()
            self._set_visible(True)

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        log.info("quitting")
        self._chat.flush()
        try:
            self._core.shutdown()
        except Exception:
            log.exception("shutdown failed")
        self._tray.stop()
        try:
            if self._window:
                self._window.destroy()
        except Exception:
            pass

    # ───────────────────────── lifecycle ─────────────────────────
    def _set_visible(self, v: bool) -> None:
        self._visible = v
        try:
            self._q.put_nowait(("visible", {"visible": v}))
        except queue.Full:
            pass

    def _on_shown(self) -> None:
        hwnd = 0
        try:
            hwnd = int(self._window.native.Handle.ToInt64())
        except Exception:
            pass
        if hwnd:
            self._frame.attach(hwnd)
        else:
            self._frame.find(self._name())
        log.info("window shown (hwnd=%s)", self._frame.hwnd)
        self._fix_size()
        threading.Thread(target=self._probe, daemon=True).start()

    def _fix_size(self) -> None:
        """WinForms shrinks a frameless window by the border size; restore the intended size, centred."""
        try:
            if not self._frame.hwnd:
                return
            x, y, w, h = self._frame.rect()
            if abs(w - WIN_W) > 4 or abs(h - WIN_H) > 4:
                wx, wy, ww, wh = self._frame.work_area()
                nx, ny = wx + (ww - WIN_W) // 2, wy + (wh - WIN_H) // 2
                self._frame._set(nx, ny, WIN_W, WIN_H)
        except Exception as e:
            log.debug("fix size failed: %s", e)

    def _probe(self) -> None:
        time.sleep(6)
        if self._ready.is_set():
            return
        try:
            r = self._window.evaluate_js("document.readyState + '|' + location.href + '|' + (typeof window.J) + '|' + (window.pywebview ? Object.keys(window.pywebview.api || {}).length : -1)")
            log.warning("UI not connected after 6 s; page state: %s", r)
        except Exception as e:
            log.warning("UI probe failed: %s", e)

    def _on_closing(self):
        if self._quitting:
            return True
        # Alt+F4 / taskbar close → tray (if enabled)
        threading.Thread(target=self.win_close, daemon=True).start()
        return False

    def _after_start(self) -> None:
        self._core.start()
        threading.Thread(target=self._pump, name="ui-pump", daemon=True).start()
        threading.Thread(target=self._ticker, name="ui-ticker", daemon=True).start()
        threading.Thread(target=self._update_check, name="update-check", daemon=True).start()
        log.info("UI ready (v%s, webview)", APP_VERSION)

    def _update_check(self) -> None:
        time.sleep(5)
        rel = updater.check_latest(self._core.settings)
        if rel:
            self._emit("update", tag=rel["tag"])


def run(start_minimized: bool = False, on_instance=None) -> None:
    import webview

    pl = logging.getLogger("pywebview")
    for h in log.handlers:
        pl.addHandler(h)
    pl.setLevel(logging.INFO)
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
    bridge = Bridge(start_minimized)
    if on_instance is not None:
        on_instance(bridge.show)
    bridge._tray.start()
    hidden = bool(start_minimized and bridge._tray.ok)
    bridge._visible = not hidden
    win = webview.create_window(
        persona.assistant_name(bridge._core.settings), url=os.path.join(WEB_DIR, "index.html"), js_api=bridge,
        width=WIN_W, height=WIN_H, min_size=(MIN_W, MIN_H), frameless=True, easy_drag=False,
        background_color="#161616", hidden=hidden, on_top=bool(bridge._core.settings.get("always_on_top")),
        text_select=False,
    )
    bridge._window = win
    win.events.shown += bridge._on_shown
    win.events.closing += bridge._on_closing
    win.events.minimized += lambda: bridge._set_visible(False)
    win.events.restored += lambda: bridge._set_visible(True)
    storage = os.path.join(DATA_DIR, "webview")
    ensure_dir(storage)
    debug = os.environ.get("JARVIS_DEVTOOLS") == "1"
    webview.start(bridge._after_start, gui="edgechromium", debug=debug, private_mode=True,
                  storage_path=storage)
    if not bridge._quitting:
        bridge.quit()
