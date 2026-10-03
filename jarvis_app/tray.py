"""System-tray icon (pystray) so Jarvis can live in the background."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("jarvis")


def make_icon_image(size: int = 64, color: str = "#f2f2f2"):
    """Monochrome sparkle on a dark rounded square (matches the UI)."""
    from PIL import Image, ImageDraw
    k = 4  # supersample
    S = size * k
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=S * 0.24, fill="#141414")
    c, r, w = S / 2, S * 0.36, S * 0.075
    pts = []
    import math
    for i in range(8):
        ang = math.pi / 2 * (i // 2) + (0 if i % 2 == 0 else math.pi / 4)
        rad = r if i % 2 == 0 else w
        pts.append((c + rad * math.cos(ang), c - rad * math.sin(ang)))
    d.polygon(pts, fill=color)
    return img.resize((size, size), Image.LANCZOS)


class Tray:
    def __init__(self, title: str, on_show, on_listen, on_focus_stop, on_quit) -> None:
        self.title = title
        self.on_show = on_show
        self.on_listen = on_listen
        self.on_focus_stop = on_focus_stop
        self.on_quit = on_quit
        self.icon = None
        self.ok = False

    def start(self) -> bool:
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Показать", lambda: self.on_show(), default=True),
                pystray.MenuItem("Слушать", lambda: self.on_listen()),
                pystray.MenuItem("Остановить фокус", lambda: self.on_focus_stop()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda: self.on_quit()),
            )
            self.icon = pystray.Icon("jarvis", make_icon_image(), self.title, menu)
            threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
            self.ok = True
        except Exception as e:
            log.warning("tray unavailable: %s", e)
            self.ok = False
        return self.ok

    def notify(self, text: str, title: str | None = None) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(text[:200], title or self.title)
            except Exception:
                pass

    def set_tooltip(self, text: str) -> None:
        if self.icon is not None:
            try:
                self.icon.title = text[:120]
                self.title = text[:120]
            except Exception:
                pass

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
