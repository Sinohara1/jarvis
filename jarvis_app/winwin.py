"""Win32 helpers for the frameless window: drag-move, edge resize, maximize to work area,
rounded corners. All no-ops off Windows."""

from __future__ import annotations

import ctypes
import sys
import threading
import time

IS_WIN = sys.platform == "win32"

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
VK_LBUTTON = 0x01


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]


class FramelessWindow:
    def __init__(self, min_w: int = 760, min_h: int = 520) -> None:
        self.hwnd = 0
        self.min_w, self.min_h = min_w, min_h
        self.maximized = False
        self._normal: tuple[int, int, int, int] | None = None
        self._busy = threading.Lock()

    # ── lookup ──
    def attach(self, hwnd: int) -> None:
        self.hwnd = int(hwnd or 0)
        if self.hwnd and IS_WIN:
            try:  # Win11 rounded corners (ignored on Win10)
                pref = ctypes.c_int(2)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(pref), 4)
            except Exception:
                pass

    def find(self, title: str) -> int:
        if IS_WIN and not self.hwnd:
            self.attach(ctypes.windll.user32.FindWindowW(None, title))
        return self.hwnd

    def rect(self) -> tuple[int, int, int, int]:
        r = RECT()
        ctypes.windll.user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def _set(self, x: int, y: int, w: int, h: int, *, move_only: bool = False) -> None:
        flags = SWP_NOZORDER | SWP_NOACTIVATE | (SWP_NOSIZE if move_only else 0)
        ctypes.windll.user32.SetWindowPos(self.hwnd, 0, int(x), int(y), int(w), int(h), flags)

    def work_area(self) -> tuple[int, int, int, int]:
        u = ctypes.windll.user32
        mon = u.MonitorFromWindow(self.hwnd, 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        u.GetMonitorInfoW(mon, ctypes.byref(mi))
        r = mi.rcWork
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    # ── maximize ──
    def toggle_maximize(self) -> bool:
        if not (IS_WIN and self.hwnd):
            return False
        if self.maximized and self._normal:
            self._set(*self._normal)
            self.maximized = False
        else:
            self._normal = self.rect()
            self._set(*self.work_area())
            self.maximized = True
        return self.maximized

    # ── mouse loops (run until the left button is released) ──
    def _cursor(self) -> tuple[int, int]:
        p = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def _button_down(self) -> bool:
        u = ctypes.windll.user32
        vk = 0x02 if u.GetSystemMetrics(23) else VK_LBUTTON  # SM_SWAPBUTTON
        return bool(u.GetAsyncKeyState(vk) & 0x8000)

    def begin_move(self, on_unmax=None) -> None:
        if not (IS_WIN and self.hwnd) or not self._busy.acquire(blocking=False):
            return
        threading.Thread(target=self._move_loop, args=(on_unmax,), daemon=True, name="win-move").start()

    def _move_loop(self, on_unmax) -> None:
        try:
            sx, sy = self._cursor()
            x0, y0, w, h = self.rect()
            unmaxed = False
            last = None
            while self._button_down():
                cx, cy = self._cursor()
                if self.maximized and not unmaxed:
                    if abs(cx - sx) + abs(cy - sy) < 6:
                        time.sleep(0.008)
                        continue
                    nw, nh = (self._normal[2], self._normal[3]) if self._normal else (w, h)
                    frac = (sx - x0) / max(1, w)
                    x0 = int(cx - nw * frac)
                    y0 = cy - (sy - y0)
                    w, h = nw, nh
                    sx, sy = cx, cy
                    self._set(x0, y0, w, h)
                    self.maximized = False
                    unmaxed = True
                    if on_unmax:
                        on_unmax()
                pos = (x0 + cx - sx, y0 + cy - sy)
                if pos != last:
                    self._set(pos[0], pos[1], 0, 0, move_only=True)
                    last = pos
                time.sleep(0.008)
        finally:
            self._busy.release()

    def begin_resize(self, edge: str) -> None:
        if not (IS_WIN and self.hwnd) or self.maximized or not self._busy.acquire(blocking=False):
            return
        threading.Thread(target=self._resize_loop, args=(edge,), daemon=True, name="win-resize").start()

    def _resize_loop(self, edge: str) -> None:
        try:
            sx, sy = self._cursor()
            x0, y0, w0, h0 = self.rect()
            last = None
            while self._button_down():
                cx, cy = self._cursor()
                dx, dy = cx - sx, cy - sy
                x, y, w, h = x0, y0, w0, h0
                if "e" in edge:
                    w = max(self.min_w, w0 + dx)
                if "s" in edge:
                    h = max(self.min_h, h0 + dy)
                if "w" in edge:
                    w = max(self.min_w, w0 - dx)
                    x = x0 + (w0 - w)
                if "n" in edge:
                    h = max(self.min_h, h0 - dy)
                    y = y0 + (h0 - h)
                r = (x, y, w, h)
                if r != last:
                    self._set(*r)
                    last = r
                time.sleep(0.012)
        finally:
            self._busy.release()

    def foreground(self) -> None:
        if IS_WIN and self.hwnd:
            u = ctypes.windll.user32
            if u.IsIconic(self.hwnd):
                u.ShowWindow(self.hwnd, 9)
            u.SetForegroundWindow(self.hwnd)
