"""Screen awareness: foreground window, distraction matching, screenshots,
and the nudge policy used by the focus guard.

Screenshots are kept in memory only (JPEG bytes), never written to disk.
"""

from __future__ import annotations

import io
import os
import random
import sys
from dataclasses import dataclass

IS_WIN = sys.platform == "win32"

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _user32.MonitorFromWindow.restype = wintypes.HANDLE
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    _user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]


@dataclass
class Foreground:
    title: str = ""
    process: str = ""
    pid: int = 0
    hwnd: int = 0


def get_foreground() -> Foreground:
    if not IS_WIN:
        return Foreground()
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return Foreground()
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc = ""
        h = _kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if h:
            try:
                size = wintypes.DWORD(1024)
                pbuf = ctypes.create_unicode_buffer(1024)
                if _kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                    proc = os.path.basename(pbuf.value)
            finally:
                _kernel32.CloseHandle(h)
        return Foreground(title=buf.value, process=proc, pid=int(pid.value), hwnd=int(hwnd))
    except Exception:
        return Foreground()


def match_distraction(title: str, process: str, patterns: list[str],
                      own_pid: int | None = None, pid: int = 0) -> str | None:
    """First pattern found (case-insensitive substring) in title or process name."""
    if own_pid is not None and pid == own_pid:
        return None
    hay_t = (title or "").lower()
    hay_p = (process or "").lower()
    if not hay_t and not hay_p:
        return None
    for pat in patterns:
        p = (pat or "").strip().lower()
        if not p:
            continue
        if p.startswith("proc:"):
            if p[5:].strip() and p[5:].strip() in hay_p:
                return pat
            continue
        if p in hay_t or p in hay_p:
            return pat
    return None


# ─── Screenshot ──────────────────────────────────────────────────────────────

def _monitor_rect_for_foreground() -> tuple[int, int, int, int] | None:
    """Physical-pixel rect of the monitor holding the foreground window.
    Uses a per-thread DPI context so the rest of the (DPI-unaware) Tk app is untouched."""
    if not IS_WIN:
        return None
    old = None
    try:
        try:
            _user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            _user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            old = _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception:
            old = None
        hwnd = _user32.GetForegroundWindow()
        mon = _user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        r = mi.rcMonitor
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:
        return None
    finally:
        if old:
            try:
                _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
            except Exception:
                pass


def capture_screen_jpeg(max_side: int = 1024, quality: int = 60) -> bytes | None:
    """Downscaled JPEG of the active monitor, in memory. None if unavailable."""
    try:
        from PIL import ImageGrab
    except Exception:
        return None
    try:
        rect = _monitor_rect_for_foreground()
        if rect:
            img = ImageGrab.grab(bbox=rect, all_screens=True)
        else:
            img = ImageGrab.grab()
    except Exception:
        return None
    return image_to_jpeg(img, max_side=max_side, quality=quality)


def image_to_jpeg(img, max_side: int = 1024, quality: int = 60) -> bytes:
    img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, float(max_side) / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# ─── Nudge policy ────────────────────────────────────────────────────────────

class NudgePolicy:
    """Decides when to speak a nudge. Escalates 1→3 while the user stays
    off-task; resets after `reset_after` seconds back on task.

    - grace: off-task must persist this long before the first nudge
    - cooldown: minimum gap between nudges (level 3 repeats at 2× cooldown)
    """

    def __init__(self, cooldown: float = 60.0, grace: float = 8.0, reset_after: float = 90.0) -> None:
        self.cooldown = cooldown
        self.grace = grace
        self.reset_after = reset_after
        self.level = 0
        self.offtask_since: float | None = None
        self.ontask_since: float | None = None
        self.last_nudge: float | None = None

    def reset(self) -> None:
        self.level = 0
        self.offtask_since = None
        self.ontask_since = None
        self.last_nudge = None

    def on_task(self, now: float) -> None:
        self.offtask_since = None
        if self.ontask_since is None:
            self.ontask_since = now
        if self.level and now - self.ontask_since >= self.reset_after:
            self.level = 0
            self.last_nudge = None

    def off_task(self, now: float, *, immediate: bool = False) -> int | None:
        """Register an off-task observation. Returns nudge level (1..3) or None."""
        self.ontask_since = None
        if self.offtask_since is None:
            self.offtask_since = now
        if not immediate and now - self.offtask_since < self.grace:
            return None
        if self.last_nudge is not None:
            gap = self.cooldown * (2.0 if self.level >= 3 else 1.0)
            if now - self.last_nudge < gap:
                return None
        self.level = min(3, self.level + 1)
        self.last_nudge = now
        return self.level


NUDGES = {
    1: [
        "Эй, мы же делаем {task}. Давай вернёмся.",
        "Кажется, ты отвлёкся. Возвращаемся к задаче: {task}.",
        "Небольшое напоминание: сейчас время для «{task}».",
    ],
    2: [
        "Вова, это уже второй раз. Закрывай и возвращайся к задаче: {task}.",
        "Не залипай. Осталось {left}, давай доделаем {task}.",
        "Я всё вижу. {task} само себя не сделает.",
    ],
    3: [
        "Серьёзно, хватит листать. Закрой это прямо сейчас и вернись к задаче: {task}.",
        "Стоп. Ты обещал себе {task}. Закрывай ленту, у тебя получится.",
        "Это уже третий звонок. Убери отвлечение и сделай хотя бы пять минут: {task}.",
    ],
}


def nudge_text(level: int, task: str, left: str = "", reason: str = "") -> str:
    lvl = max(1, min(3, int(level)))
    t = random.choice(NUDGES[lvl])
    return t.format(task=task or "твоя задача", left=left or "немного")
