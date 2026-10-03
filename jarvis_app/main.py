"""Entry point: logging, single instance, pywebview window."""

from __future__ import annotations

import logging
import sys
import threading


def _message_box(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "Джарвис", 0x10)
    except Exception:
        pass


def main() -> None:
    from . import APP_VERSION
    from .config import LOG_PATH, setup_logging
    log = setup_logging(LOG_PATH)
    log.info("=== Jarvis %s starting (frozen=%s, py=%s) ===", APP_VERSION, getattr(sys, "frozen", False),
             sys.version.split()[0])

    def excepthook(t, v, tb):
        log.error("uncaught", exc_info=(t, v, tb))

    sys.excepthook = excepthook
    threading.excepthook = lambda a: log.error("thread %s crashed", a.thread.name if a.thread else "?",
                                              exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

    if "--selftest" in sys.argv:
        from .selftest import run
        sys.exit(run())

    from .winutil import SingleInstance

    holder: dict = {}
    inst = SingleInstance()
    if not inst.acquire(lambda: holder.get("show") and holder["show"]()):
        log.info("another instance is running; asked it to show itself")
        return
    try:
        from .webui import run
        run(start_minimized="--minimized" in sys.argv,
            on_instance=lambda show: holder.__setitem__("show", show))
    except Exception:
        logging.getLogger("jarvis").exception("fatal startup error")
        _message_box(f"Ошибка запуска. Подробности: {LOG_PATH}\n\n"
                     "Если не хватает Microsoft Edge WebView2 Runtime — установи его с сайта Microsoft.")
        raise
    finally:
        inst.release()
        log.info("=== Jarvis stopped ===")
