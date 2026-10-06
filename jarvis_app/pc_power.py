"""PC control power helpers: media keys, Telegram Desktop send, elevated scripts,
and full-tier desktop control (windows, typing, hotkeys, shell, lock/sleep).

Gated by settings.pc_control (safe | standard | full). Best-effort Win32 only —
no silent UAC bypass, no Bot API, no mass-delete/disk wipe tools.
Message bodies and typed text are never logged at INFO.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time

log = logging.getLogger("jarvis")
IS_WIN = sys.platform == "win32"

PC_CONTROL_RANKS = {"safe": 0, "standard": 1, "full": 2}

# Minimum pc_control level to expose / run a tool. Unlisted tools → safe.
TOOL_MIN_LEVEL: dict[str, str] = {
    "media_control": "standard",
    "adjust_volume": "safe",
    "send_telegram": "full",
    "run_elevated": "full",
    "window_control": "full",
    "type_text": "full",
    "press_hotkey": "full",
    "run_command": "full",
    "get_active_window": "full",
    "lock_workstation": "full",
    "system_power": "full",
    "app_search": "full",
    "telegram_open_chat": "full",
}

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_0 = 0x30
VK_A = 0x41
VK_F = 0x46
VK_K = 0x4B
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002

MEDIA_ACTION_VK = {
    "play": VK_MEDIA_PLAY_PAUSE,
    "pause": VK_MEDIA_PLAY_PAUSE,
    "play_pause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK,
    "previous": VK_MEDIA_PREV_TRACK,
    "stop": VK_MEDIA_STOP,
}
VOLUME_ACTION_VK = {
    "up": VK_VOLUME_UP,
    "down": VK_VOLUME_DOWN,
    "mute": VK_VOLUME_MUTE,
}

SPOTIFY_ALIASES = frozenset({
    "spotify", "спотифай", "спотифи", "спотик", "споти", "spoti", "spot", "спотифае", "спотифаи",
})

ELEVATED_EXTS = frozenset({".ps1", ".bat", ".cmd", ".py"})


def pc_control_level(settings: dict | None) -> str:
    lvl = str((settings or {}).get("pc_control") or "standard").strip().lower()
    return lvl if lvl in PC_CONTROL_RANKS else "standard"


def tool_allowed(name: str, level: str | None = None, settings: dict | None = None) -> bool:
    lvl = level if level is not None else pc_control_level(settings)
    need = TOOL_MIN_LEVEL.get(name, "safe")
    return PC_CONTROL_RANKS.get(lvl, 1) >= PC_CONTROL_RANKS.get(need, 0)


def resolve_media_action(action: str) -> str | None:
    a = (action or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "play": "play", "resume": "play", "start": "play", "играть": "play", "воспроизвести": "play",
        "pause": "pause", "пауза": "pause",
        "play_pause": "play_pause", "toggle": "play_pause", "playpause": "play_pause",
        "next": "next", "следующий": "next", "вперёд": "next", "вперед": "next",
        "previous": "previous", "prev": "previous", "предыдущий": "previous", "назад": "previous",
        "stop": "stop", "стоп": "stop",
    }
    return aliases.get(a)


def resolve_volume_action(action: str) -> str | None:
    a = (action or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "up": "up", "louder": "up", "громче": "up", "volume_up": "up",
        "down": "down", "quieter": "down", "тише": "down", "volume_down": "down",
        "mute": "mute", "unmute": "mute", "мьют": "mute", "мут": "mute", "беззвучный": "mute",
    }
    return aliases.get(a)


def send_media_key(vk: int) -> bool:
    if not IS_WIN:
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        u.keybd_event(int(vk), 0, 0, 0)
        u.keybd_event(int(vk), 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as e:
        log.info("media key vk=%s failed: %s", vk, e)
        return False


# Keys that need KEYEVENTF_EXTENDEDKEY so apps see the real (non-numpad) key.
_EXTENDED_VK = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D})
KEYEVENTF_EXTENDEDKEY = 0x0001
VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x12, 0x5B, 0x5C


def _key(vk: int, up: bool = False) -> None:
    """One synthetic key event *with* the hardware scan code. keybd_event(vk, 0, …) leaves the scan
    code empty, which some Qt/Chromium apps (Telegram Desktop, Discord) treat as a different key or
    drop for shortcuts; MapVirtualKey gives them what a real keyboard would send."""
    import ctypes
    u = ctypes.windll.user32
    vk = int(vk)
    scan = int(u.MapVirtualKeyW(vk, 0) or 0) & 0xFF
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VK else 0)
    u.keybd_event(vk, scan, flags, 0)


def release_modifiers() -> None:
    """Send key-up for Ctrl/Shift/Alt/Win that Windows thinks are still held (push-to-talk hotkey
    released a few ms ago, a stuck Alt…). Otherwise our «Ctrl+F» may arrive as «Ctrl+Alt+F»."""
    if not IS_WIN:
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        for vk in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN, 0xA2, 0xA3, 0xA0, 0xA1, 0xA4, 0xA5):
            if u.GetAsyncKeyState(vk) & 0x8000:
                _key(vk, up=True)
    except Exception as e:
        log.info("release_modifiers failed: %s", e)


def _key_combo(modifiers: list[int], vk: int) -> None:
    for m in modifiers:
        _key(m)
    time.sleep(0.01)
    _key(vk)
    _key(vk, up=True)
    time.sleep(0.01)
    for m in reversed(modifiers):
        _key(m, up=True)


def _set_clipboard_text(text: str) -> bool:
    """CF_UNICODETEXT via Win32. Returns False off Windows / on failure."""
    if not IS_WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        data = (text or "").encode("utf-16-le") + b"\x00\x00"
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        opened = False
        for _ in range(20):  # clipboard managers / Win+V history hold the clipboard for a few ms
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.025)
        if not opened:
            log.info("clipboard busy (OpenClipboard failed 20x)")
            return False
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                return False
            ptr = kernel32.GlobalLock(h)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                kernel32.GlobalFree(h)
                return False
            return True
        finally:
            user32.CloseClipboard()
    except Exception as e:
        log.info("clipboard set failed: %s", e)
        return False


def _paste_and_enter(*, press_enter: bool = True) -> None:
    _key_combo([VK_CONTROL], VK_V)
    time.sleep(0.12)
    if press_enter:
        _tap(VK_RETURN)


def find_window_hwnd(title_substr: str) -> int:
    """First top-level window whose title contains title_substr (case-insensitive). 0 if none."""
    if not IS_WIN:
        return 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    needle = (title_substr or "").lower()
    found = ctypes.c_void_p(0)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @WNDENUMPROC
    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if needle and needle in (buf.value or "").lower():
            found.value = hwnd
            return False
        return True

    user32.EnumWindows(_enum, 0)
    return int(found.value or 0)


def is_foreground(hwnd: int) -> bool:
    """Is hwnd (or one of its owned/child windows) the current foreground window?"""
    if not (IS_WIN and hwnd):
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        fg = int(user32.GetForegroundWindow() or 0)
        if not fg:
            return False
        if fg == int(hwnd) or int(user32.GetAncestor(fg, 2) or 0) == int(hwnd):  # GA_ROOT
            return True
        return int(user32.GetWindow(fg, 4) or 0) == int(hwnd)  # GW_OWNER (popups of the same app)
    except Exception:
        return False


def focus_hwnd(hwnd: int, *, aggressive: bool = False) -> bool:
    """Bring a top-level window to the foreground, fighting Windows' foreground lock.

    Jarvis is usually a *background* process when a voice command runs, and Windows only lets the
    process that received the last input event call SetForegroundWindow. Escalation:
      1. restore/show + SetForegroundWindow;
      2. AttachThreadInput to the current foreground thread + BringWindowToTop;
      3. (aggressive) SwitchToThisWindow, then the classic Alt-tap (Alt counts as user input and
         releases the lock; only for apps without a classic menu bar such as Telegram/Discord/Spotify,
         where a lone Alt does nothing);
      4. (aggressive) minimize + restore — visible flicker, but Windows always activates a window the
         user «restores».
    """
    if not (IS_WIN and hwnd):
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        else:
            user32.ShowWindow(hwnd, 5)  # SW_SHOW (keep maximized state)

        def _ok(wait: float = 0.06) -> bool:
            t0 = time.monotonic()
            while True:
                if is_foreground(hwnd):
                    return True
                if time.monotonic() - t0 >= wait:
                    return False
                time.sleep(0.02)

        user32.SetForegroundWindow(hwnd)
        if _ok():
            return True
        # 2. Foreground lock: attach our input queue to the foreground thread so the call is allowed.
        kernel32 = ctypes.windll.kernel32
        fg = int(user32.GetForegroundWindow() or 0)
        fg_tid = int(user32.GetWindowThreadProcessId(fg, None) or 0) if fg else 0
        me = int(kernel32.GetCurrentThreadId())
        attached = bool(fg_tid and fg_tid != me and user32.AttachThreadInput(me, fg_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_tid, False)
        if _ok(0.12):
            return True
        if aggressive:
            try:
                user32.SwitchToThisWindow(hwnd, True)
            except Exception:
                pass
            if _ok(0.15):
                return True
            _key(VK_MENU)                      # 3. Alt down → we «received input» → lock released
            try:
                user32.SetForegroundWindow(hwnd)
            finally:
                _key(VK_MENU, up=True)
            if _ok(0.15):
                return True
            user32.ShowWindow(hwnd, 6)         # 4. SW_MINIMIZE → SW_RESTORE
            time.sleep(0.15)
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            if _ok(0.4):
                return True
        log.info("focus_hwnd: window did not come to foreground (aggressive=%s)", aggressive)
        return False
    except Exception as e:
        log.info("focus_hwnd failed: %s", e)
        return False


def open_spotify_protocol() -> bool:
    """Wake Spotify via shell protocol. Best-effort."""
    if not IS_WIN:
        return False
    try:
        os.startfile("spotify:")  # type: ignore[attr-defined]
        return True
    except OSError as e:
        log.info("spotify: protocol failed: %s", e)
        return False


def elevated_path_allowed(path: str) -> tuple[bool, str]:
    """Allow only existing scripts under the user profile (or OneDrive). Reject .. and system dirs."""
    raw = (path or "").strip().strip('"')
    if not raw:
        return False, "пустой путь"
    p = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
    if ".." in raw.replace("\\", "/").split("/"):
        # abspath already resolves; still reject odd encodings
        pass
    if not os.path.isfile(p):
        return False, "файл не найден"
    ext = os.path.splitext(p)[1].lower()
    if ext not in ELEVATED_EXTS:
        return False, "разрешены только .ps1, .bat, .cmd, .py"
    home = os.path.abspath(os.path.expanduser("~"))
    allowed_roots = [home]
    for env in ("OneDrive", "OneDriveConsumer", "USERPROFILE"):
        v = os.environ.get(env)
        if v:
            allowed_roots.append(os.path.abspath(v))
    # Also allow Desktop/Documents via public if somehow outside — stick to home/onedrive
    norm_p = os.path.normcase(p)
    if not any(norm_p.startswith(os.path.normcase(r) + os.sep) or norm_p == os.path.normcase(r)
               for r in allowed_roots):
        return False, "скрипт должен лежать в вашей папке пользователя (или OneDrive)"
    # Block obvious system locations even if somehow under a weird junction
    low = norm_p.lower()
    for bad in ("\\windows\\", "\\system32\\", "\\syswow64\\", "\\program files\\"):
        if bad in low:
            return False, "системные папки запрещены"
    return True, p


def run_elevated_script(path: str, args: str = "") -> dict:
    """Launch script with UAC consent (ShellExecute runas). No silent bypass."""
    ok, info = elevated_path_allowed(path)
    if not ok:
        return {"ok": False, "error": info}
    p = info
    if not IS_WIN:
        return {"ok": False, "error": "запуск от имени администратора доступен только в Windows"}
    ext = os.path.splitext(p)[1].lower()
    extra = (args or "").strip()
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        if ext == ".ps1":
            file = "powershell.exe"
            params = f'-NoProfile -ExecutionPolicy Bypass -File "{p}"' + (f" {extra}" if extra else "")
        elif ext == ".py":
            file = sys.executable or "python.exe"
            params = f'"{p}"' + (f" {extra}" if extra else "")
        else:
            file = p
            params = extra
        # ShellExecuteW: return value > 32 means success (UAC may still be cancelled by user)
        rc = int(shell32.ShellExecuteW(None, "runas", file, params or None, os.path.dirname(p), 1))
        if rc <= 32:
            return {"ok": False, "error": f"не удалось запросить повышение прав (код {rc}). "
                                         "Возможно, UAC отклонён или путь недоступен.",
                    "path": p}
        return {"ok": True, "path": p, "elevated": True,
                "note": "Запрос UAC отправлен. Подтверди в окне Windows, если оно появилось."}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _tap(vk: int) -> None:
    _key(vk)
    _key(vk, up=True)


def window_title(hwnd: int) -> str:
    if not (IS_WIN and hwnd):
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value or ""
    except Exception:
        return ""


def _process_basename(pid: int) -> str:
    """Lower-case exe basename for pid ('' on failure)."""
    if not (IS_WIN and pid):
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            size = wintypes.DWORD(1024)
            pbuf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                return os.path.basename(pbuf.value or "").lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def find_window_by_process(exe_names) -> int:
    """First visible, titled top-level window owned by one of exe_names (e.g. 'spotify.exe'). 0 if none.

    Process match is more robust than title: Spotify's title becomes «Artist - Song» while playing,
    Telegram's becomes «Telegram (3)».
    """
    if not IS_WIN:
        return 0
    wanted = {str(n).lower() for n in (exe_names or ()) if n}
    if not wanted:
        return 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = ctypes.c_void_p(0)
    cache: dict[int, str] = {}

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @WNDENUMPROC
    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        p = int(pid.value)
        if p not in cache:
            cache[p] = _process_basename(p)
        if cache[p] in wanted:
            found.value = hwnd
            return False
        return True

    try:
        user32.EnumWindows(_enum, 0)
    except Exception as e:
        log.info("find_window_by_process failed: %s", e)
    return int(found.value or 0)


def find_main_window_by_process(exe_names, prefer_title: str = "") -> int:
    """The *main* window of an app: visible, titled, not a tool window (notifications, tray popups),
    preferring a title containing ``prefer_title`` and then the largest area. Telegram has several
    top-level windows (main, media viewer, notification toasts) — the first one EnumWindows returns
    is not always the chat list."""
    if not IS_WIN:
        return 0
    wanted = {str(n).lower() for n in (exe_names or ()) if n}
    if not wanted:
        return 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    cache: dict[int, str] = {}
    cands: list[tuple[int, int, int]] = []   # (title_match, area, hwnd)
    needle = (prefer_title or "").lower()
    GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x00000080

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @WNDENUMPROC
    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        p = int(pid.value)
        if p not in cache:
            cache[p] = _process_basename(p)
        if cache[p] not in wanted:
            return True
        try:
            if int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)) & WS_EX_TOOLWINDOW:
                return True
        except Exception:
            pass
        r = wintypes.RECT()
        area = 0
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        cands.append((1 if needle and needle in (buf.value or "").lower() else 0, area, int(hwnd)))
        return True

    try:
        user32.EnumWindows(_enum, 0)
    except Exception as e:
        log.info("find_main_window_by_process failed: %s", e)
    if not cands:
        return 0
    cands.sort(reverse=True)
    return cands[0][2]


def wait_for_app_window(exe_names=(), title: str = "", timeout: float = 10.0) -> int:
    t0 = time.monotonic()
    while True:
        hwnd = find_window_by_process(exe_names) if exe_names else 0
        if not hwnd and title:
            hwnd = find_window_hwnd(title)
        if hwnd or time.monotonic() - t0 >= timeout:
            return hwnd
        time.sleep(0.25)


def parse_key_sequence(spec: str) -> list[tuple[list[int], int]] | None:
    """'esc; esc; ctrl+f' / 'down*2; enter' → [(mods, vk), …].

    Steps are ';' separated (also ' then ' / '>'). Each step is a hotkey like ctrl+shift+s; optional
    '*N' repeats the step (1..10). Returns None if any step is invalid (or > 30 steps).
    """
    raw = (spec or "").strip().lower()
    if not raw:
        return []
    raw = raw.replace(" then ", ";").replace(">", ";").replace("\n", ";")
    steps: list[tuple[list[int], int]] = []
    for part in [p.strip() for p in raw.split(";") if p.strip()]:
        rep = 1
        m = re.fullmatch(r"(.+?)\s*\*\s*(\d{1,2})", part)
        if m:
            part, rep = m.group(1).strip(), max(1, min(10, int(m.group(2))))
        parsed = parse_hotkey(part)
        if not parsed:
            return None
        steps.extend([parsed] * rep)
    return steps if len(steps) <= 30 else None


def send_key_sequence(steps: list[tuple[list[int], int]], gap: float = 0.12) -> None:
    for mods, key in steps:
        _key_combo(mods, key)
        time.sleep(gap)


def _paste_text(text: str, *, clear_field: bool = True) -> bool:
    if not _set_clipboard_text(text):
        return False
    if clear_field:
        _key_combo([VK_CONTROL], VK_A)
        time.sleep(0.05)
    _key_combo([VK_CONTROL], VK_V)
    return True


# Known in-app search flows: focus app → hotkey → paste query → Enter. Used by app_search and helpers.
# exe: process names (window lookup); open: name for open_app when not running; search: key sequence.
APP_SEARCH_PRESETS: dict[str, dict] = {
    "spotify": {"exe": ["spotify.exe"], "title": "Spotify", "open": "Spotify", "search": "ctrl+k", "alt_ok": True,
                "note": "Ctrl+K — быстрый поиск Spotify; Enter включает первый результат"},
    "telegram": {"exe": ["telegram.exe", "ayugram.exe", "kotatogram.exe", "64gram.exe"],
                 "title": "Telegram", "open": "Telegram", "search": "esc; esc; ctrl+f", "alt_ok": True,
                 "note": "Esc закрывает чат, Ctrl+F — поиск по чатам; Enter открывает первый. "
                         "Ctrl+K в Telegram Desktop — вставка ссылки, НЕ поиск"},
    "discord": {"exe": ["discord.exe"], "title": "Discord", "open": "Discord", "search": "ctrl+k", "alt_ok": True},
    "chrome": {"exe": ["chrome.exe"], "title": "Chrome", "open": "Google Chrome", "search": "ctrl+l"},
    "edge": {"exe": ["msedge.exe"], "title": "Edge", "open": "Microsoft Edge", "search": "ctrl+l"},
    "firefox": {"exe": ["firefox.exe"], "title": "Firefox", "open": "Firefox", "search": "ctrl+l"},
    "yandex": {"exe": ["browser.exe"], "title": "Яндекс", "open": "Yandex", "search": "ctrl+l"},
    "opera": {"exe": ["opera.exe"], "title": "Opera", "open": "Opera", "search": "ctrl+l"},
    "brave": {"exe": ["brave.exe"], "title": "Brave", "open": "Brave", "search": "ctrl+l"},
    "vscode": {"exe": ["code.exe"], "title": "Visual Studio Code", "open": "Visual Studio Code", "search": "ctrl+p"},
    "explorer": {"exe": ["explorer.exe"], "title": "", "open": "explorer", "search": "ctrl+e"},
    "slack": {"exe": ["slack.exe"], "title": "Slack", "open": "Slack", "search": "ctrl+k"},
    "notion": {"exe": ["notion.exe"], "title": "Notion", "open": "Notion", "search": "ctrl+p"},
    "obsidian": {"exe": ["obsidian.exe"], "title": "Obsidian", "open": "Obsidian", "search": "ctrl+o"},
    "whatsapp": {"exe": ["whatsapp.exe", "whatsapp.root.exe"], "title": "WhatsApp", "open": "WhatsApp",
                 "search": "ctrl+f"},
    "steam": {"exe": ["steamwebhelper.exe", "steam.exe"], "title": "Steam", "open": "Steam", "search": "ctrl+f"},
}
APP_PRESET_ALIASES = {
    "spotify": "spotify", "спотифай": "spotify", "спотик": "spotify", "споти": "spotify",
    "telegram": "telegram", "телеграм": "telegram", "телеграмм": "telegram", "телега": "telegram", "тг": "telegram",
    "tg": "telegram", "discord": "discord", "дискорд": "discord",
    "chrome": "chrome", "google chrome": "chrome", "хром": "chrome", "гугл хром": "chrome",
    "edge": "edge", "microsoft edge": "edge", "эдж": "edge", "firefox": "firefox", "фаерфокс": "firefox",
    "файрфокс": "firefox", "yandex": "yandex", "яндекс": "yandex", "яндекс браузер": "yandex",
    "opera": "opera", "опера": "opera", "brave": "brave", "брейв": "brave",
    "vscode": "vscode", "vs code": "vscode", "visual studio code": "vscode", "code": "vscode", "вскод": "vscode",
    "explorer": "explorer", "проводник": "explorer", "file explorer": "explorer",
    "slack": "slack", "слак": "slack", "notion": "notion", "ноушен": "notion", "obsidian": "obsidian",
    "обсидиан": "obsidian", "whatsapp": "whatsapp", "ватсап": "whatsapp", "steam": "steam", "стим": "steam",
}


def resolve_app_preset(app: str) -> tuple[str, dict] | None:
    a = (app or "").strip().lower()
    if not a:
        return None
    key = APP_PRESET_ALIASES.get(a)
    if key is None and a in SPOTIFY_ALIASES:
        key = "spotify"
    if key is None:
        for alias, k in APP_PRESET_ALIASES.items():
            if len(alias) >= 4 and alias in a:
                key = k
                break
    return (key, APP_SEARCH_PRESETS[key]) if key else None


def effective_search_keys(app: str, search_keys: str = "") -> str:
    """Search hotkey sequence for app_search: explicit keys, else the preset's, else ctrl+f. Telegram
    always gets its own «esc; esc; ctrl+f» — models like to pass ctrl+k (Discord/Slack habit), which in
    Telegram Desktop opens «insert link» in the message field instead of searching."""
    rp = resolve_app_preset(app)
    key, preset = rp if rp else ("", None)
    spec = (search_keys or "").strip() or (preset or {}).get("search") or "ctrl+f"
    if key == "telegram" and "ctrl+f" not in spec.lower().replace(" ", ""):
        spec = APP_SEARCH_PRESETS["telegram"]["search"]
    return spec


def focus_app(app: str, *, ensure_open=None, preset: dict | None = None,
              timeout: float = 10.0) -> tuple[int, bool, str]:
    """Find (or open) and focus an app window. Returns (hwnd, opened, error)."""
    if not IS_WIN:
        return 0, False, "только Windows"
    pr = preset or {}
    exe = pr.get("exe") or []
    title = pr.get("title") if pr else (app or "")
    hwnd = find_main_window_by_process(exe, title or "") if exe else 0
    if not hwnd and title:
        hwnd = find_window_hwnd(title)
    opened = False
    if not hwnd and ensure_open is not None:
        try:
            r = ensure_open(pr.get("open") or app)
            opened = bool(not isinstance(r, dict) or r.get("ok", True))
        except Exception as e:
            log.info("focus_app open failed: %s", e)
        if opened:
            hwnd = wait_for_app_window(exe, title or "", timeout)
            if hwnd:
                time.sleep(1.2)  # fresh window: let the UI finish loading before keys
    if not hwnd:
        return 0, opened, f"не вижу окно «{app}» — открой приложение"
    if exe:  # the window that appeared first may be a splash/toast; re-pick the main one
        hwnd = find_main_window_by_process(exe, title or "") or hwnd
    if not focus_hwnd(hwnd, aggressive=bool(pr.get("alt_ok"))):
        return hwnd, opened, "не удалось вывести окно на передний план"
    time.sleep(0.35)
    return hwnd, opened, ""


def app_search(app: str, query: str, *, ensure_open=None, search_keys: str = "", then_keys: str = "",
               press_enter: bool = True, clear_field: bool = True, delay: float = 0.6) -> dict:
    """Generic in-app search: focus app (open if needed) → search hotkey → paste query → Enter → extra keys.

    Works for any app: known presets (Spotify, Telegram, browsers, Discord, VS Code, …) know their
    search hotkey; for other apps pass search_keys (default ctrl+f). Empty app = foreground window.
    Never logs the query text.
    """
    q = "" if query is None else str(query)
    if not q.strip():
        return {"ok": False, "error": "пустой запрос"}
    if len(q) > 500:
        return {"ok": False, "error": "слишком длинный запрос"}
    if not IS_WIN:
        return {"ok": False, "error": "управление приложениями доступно только в Windows"}
    rp = resolve_app_preset(app)
    key, preset = rp if rp else ("", None)
    spec = effective_search_keys(app, search_keys)
    steps = parse_key_sequence(spec)
    if steps is None:
        return {"ok": False, "error": f"не понял search_keys «{spec}»", "hint": "например: ctrl+k или esc; ctrl+f"}
    after = parse_key_sequence(then_keys) if then_keys else []
    if after is None:
        return {"ok": False, "error": f"не понял then_keys «{then_keys}»", "hint": "например: down; enter"}
    opened = False
    if (app or "").strip():
        hwnd, opened, err = focus_app(app, ensure_open=ensure_open, preset=preset)
        if err:
            return {"ok": False, "error": err}
    else:
        import ctypes
        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    try:
        release_modifiers()
        send_key_sequence(steps)
        time.sleep(0.3)
        if hwnd and (app or "").strip() and not is_foreground(hwnd):
            focus_hwnd(hwnd, aggressive=bool((preset or {}).get("alt_ok")))
        if not _paste_text(q, clear_field=clear_field):
            return {"ok": False, "error": "не удалось положить запрос в буфер обмена"}
        if press_enter:
            time.sleep(max(0.1, min(float(delay or 0.6), 3.0)))
            if hwnd and (app or "").strip() and not is_foreground(hwnd):
                focus_hwnd(hwnd, aggressive=bool((preset or {}).get("alt_ok")))
            _tap(VK_RETURN)
        if after:
            time.sleep(0.6)
            send_key_sequence(after)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    log.info("app_search app=%r preset=%s query_len=%d keys=%r enter=%s then=%r opened=%s",
             app, key or "-", len(q), spec, bool(press_enter), then_keys or "", opened)
    return {"ok": True, "app": app or "(активное окно)", "preset": key or None, "search_keys": spec,
            "query_len": len(q), "pressed_enter": bool(press_enter), "then_keys": then_keys or None,
            "opened_app": opened, "window": window_title(hwnd)[:120],
            "note": "Best-effort UI-автоматизация: если результат не тот — уточни запрос или then_keys (down; enter)."}


# ─── Telegram Desktop ─────────────────────────────────────────────────────────
#
# Flow (v1.5.x, hardened):
#   focus the MAIN Telegram Desktop window (aggressive foreground: attach-thread → Alt-tap → restore)
#   → Saved Messages:  Ctrl+0  (Command::ChatSelf in tdesktop)
#   → @username:       tg://resolve?domain=<name>  if the tg: protocol belongs to Telegram Desktop,
#                      else the search flow with «@name»
#   → display name:    Esc, Esc (leave the open chat / clear old search) → Ctrl+F (chat search in the
#                      chat list; Ctrl+K in TDesktop is «insert link», NOT search) → paste → wait for
#                      results → Enter (opens the first result and focuses the message field)
# Before every key/paste step the foreground is re-checked and Telegram re-focused if something
# (Jarvis' own window, a toast, the Start menu) stole it.
# Fragile points are documented in README («Telegram: хрупкие места»).

TELEGRAM_SAVED_ALIASES = frozenset({
    "saved messages", "saved", "избранное", "избранные", "сохранённые", "сохраненные",
    "сохранённые сообщения", "сохраненные сообщения", "мои заметки", "заметки", "себе", "мне", "self", "me",
    "saved message", "избранном", "избранного", "в избранное", "мое избранное", "моё избранное",
    "обране", "збережені", "notes to self",
})
TELEGRAM_EXE = ("telegram.exe", "ayugram.exe", "kotatogram.exe", "64gram.exe")

# Seconds. Telegram's local chat filter is instant, the server (global @username) search lands after
# ~0.6–1.5 s on a normal connection; Enter before the list updates opens nothing / the wrong row.
TG_TIMING = {
    "after_focus": 0.35,      # focus settled → keys
    "esc_gap": 0.15,          # between the Esc presses
    "after_search_key": 0.4,  # Ctrl+F → search field focused
    "results_name": 1.1,      # local chats/contacts by name
    "results_username": 1.8,  # @username / latin nick → may need the server search
    "after_enter": 0.5,       # chat opened → message field focused
    "after_saved": 0.5,
    "after_uri": 1.3,         # tg://resolve → chat open
    "fresh_window": 2.2,      # Telegram was just started: wait until the UI accepts shortcuts
}

_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_TG_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:@)?([A-Za-z][A-Za-z0-9_]{3,31})/?$",
                         re.IGNORECASE)
_TG_RESOLVE_RE = re.compile(r"^tg://resolve\?domain=([A-Za-z][A-Za-z0-9_]{3,31})", re.IGNORECASE)
# voice: «собака durov», «at durov», «эт durov»
_SPOKEN_AT_RE = re.compile(r"^(?:собака|собачка|at|эт|эт-|ат)\s*([A-Za-z][A-Za-z0-9_]{3,31})$", re.IGNORECASE)
_CHAT_PREFIX_RE = re.compile(r"^(?:чат|переписк[ауи]|диалог|группу|группа|канал|chat|dialog|group|channel)\s+"
                             r"(?:с|со|with)?\s*", re.IGNORECASE)


def is_saved_messages(target: str) -> bool:
    t = (target or "").strip().lower().strip("«»\"' .!").replace("ё", "е")
    return t in {a.replace("ё", "е") for a in TELEGRAM_SAVED_ALIASES}


def telegram_target(target: str) -> tuple[str, str]:
    """Classify what to open: ('saved', '') | ('username', 'durov') | ('name', 'Мама') | ('', '').

    Accepts «@durov», «t.me/durov», «tg://resolve?domain=durov», spoken «собака durov», «чат с Мамой»
    (the «чат с» prefix is dropped; the name itself is kept — Telegram search is prefix based)."""
    t = re.sub(r"\s+", " ", str(target or "")).strip().strip("«»\"'“”„.,!?;:")
    if not t:
        return "", ""
    if is_saved_messages(t):
        return "saved", ""
    m = _TG_LINK_RE.match(t) or _TG_RESOLVE_RE.match(t) or _SPOKEN_AT_RE.match(t)
    if m:
        return "username", m.group(1)
    if t.startswith("@"):
        u = t[1:].strip()
        if _USERNAME_RE.fullmatch(u):
            return "username", u
        return ("name", u) if u else ("", "")
    t2 = _CHAT_PREFIX_RE.sub("", t).strip()
    if t2 and is_saved_messages(t2):
        return "saved", ""
    return "name", (t2 or t)


def telegram_plan(target: str, *, uri_ok: bool = True, fresh: bool = False) -> list[tuple]:
    """Pure step list for opening a chat (unit-tested; executed by run_telegram_plan).

    Steps: ("focus",) · ("keys", spec) · ("paste", text[, clear_field]) · ("sleep", sec) · ("uri", url).
    ``uri_ok`` — the tg: protocol is handled by Telegram Desktop; ``fresh`` — Telegram was just started.
    """
    kind, value = telegram_target(target)
    if not kind:
        return []
    T = TG_TIMING
    plan: list[tuple] = [("focus",)]
    if fresh:
        plan.append(("sleep", T["fresh_window"]))
    if kind == "saved":
        return plan + [("keys", "ctrl+0"), ("sleep", T["after_saved"])]
    if kind == "username" and uri_ok:
        return plan + [("uri", f"tg://resolve?domain={value}"), ("sleep", T["after_uri"]), ("focus",)]
    query = "@" + value if kind == "username" else value
    latin_nick = kind == "username" or bool(_USERNAME_RE.fullmatch(value) and ("_" in value or any(c.isdigit() for c in value)))
    return plan + [
        ("keys", "esc"), ("sleep", T["esc_gap"]), ("keys", "esc"), ("sleep", T["esc_gap"]),
        ("keys", "ctrl+f"), ("sleep", T["after_search_key"]),
        ("paste", query),
        ("sleep", T["results_username"] if latin_nick else T["results_name"]),
        ("keys", "enter"), ("sleep", T["after_enter"]),
    ]


class _WinTelegramDriver:
    """Real Win32 side of run_telegram_plan (mocked in tests)."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd

    def refind(self) -> int:
        h = find_main_window_by_process(TELEGRAM_EXE, "telegram")
        if h:
            self.hwnd = h
        return self.hwnd

    def focus(self) -> bool:
        self.refind()
        return focus_hwnd(self.hwnd, aggressive=True)

    def is_foreground(self) -> bool:
        return is_foreground(self.hwnd)

    def keys(self, spec: str) -> None:
        release_modifiers()
        send_key_sequence(parse_key_sequence(spec) or [], gap=0.08)

    def paste(self, text: str, clear: bool = True) -> bool:
        return _paste_text(text, clear_field=clear)

    def sleep(self, sec: float) -> None:
        time.sleep(sec)

    def open_uri(self, uri: str) -> bool:
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
            return True
        except OSError as e:
            log.info("telegram: tg uri failed: %s", e)
            return False


def run_telegram_plan(plan: list[tuple], drv) -> dict:
    """Execute telegram_plan steps; re-focus Telegram before keys/paste if it lost the foreground."""
    for step in plan:
        op = step[0]
        if op == "sleep":
            drv.sleep(float(step[1]))
            continue
        if op == "focus":
            if not drv.focus():
                return {"ok": False, "error": "не удалось вывести Telegram на передний план",
                        "hint": "Разверни окно Telegram и повтори — Windows не дала переключить окно."}
            continue
        if op == "uri":
            if not drv.open_uri(step[1]):
                return {"ok": False, "error": "uri_failed"}
            continue
        if op in ("keys", "paste") and not drv.is_foreground():
            log.info("telegram: lost foreground before %s — refocusing", op)
            if not drv.focus():
                return {"ok": False, "error": "Telegram потерял фокус (другое окно перехватило ввод)"}
            drv.sleep(0.2)
        if op == "keys":
            drv.keys(step[1])
        elif op == "paste":
            if not drv.paste(step[1], step[2] if len(step) > 2 else True):
                return {"ok": False, "error": "не удалось положить имя чата в буфер обмена"}
    return {"ok": True}


def _tg_protocol_is_desktop() -> bool:
    """Is tg:// registered to Telegram Desktop (not a browser / Telegram Web / nothing)?"""
    if not IS_WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        # AssocQueryStringW(ASSOCF_NONE, ASSOCSTR_EXECUTABLE=2, "tg", "open", out, &size)
        hr = ctypes.windll.shlwapi.AssocQueryStringW(0, 2, "tg", "open", buf, ctypes.byref(size))
        exe = os.path.basename(buf.value or "").lower() if hr == 0 else ""
        if exe:
            return exe in TELEGRAM_EXE or "telegram" in exe
    except Exception as e:
        log.info("tg protocol check failed: %s", e)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"tg\shell\open\command") as k:
            cmd = str(winreg.QueryValueEx(k, "")[0] or "").lower()
        return any(x in cmd for x in TELEGRAM_EXE) or "telegram" in cmd
    except Exception:
        return False


def _telegram_exe_candidates() -> list[str]:
    out = []
    for env, rel in (("APPDATA", r"Telegram Desktop\Telegram.exe"), ("LOCALAPPDATA", r"Telegram Desktop\Telegram.exe"),
                     ("ProgramFiles", r"Telegram Desktop\Telegram.exe"), ("APPDATA", r"AyuGram Desktop\AyuGram.exe")):
        base = os.environ.get(env)
        if base:
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                out.append(p)
    return out


def telegram_saved_intent(text: str) -> bool:
    """«открой избранное в телеграме» / «open Saved Messages on Telegram» — clear, short command."""
    t = (text or "").strip()
    if not t or len(t) > 120:
        return False
    return bool(_TG_OPEN_RE.search(t) and _TG_APP_RE.search(t) and _TG_SAVED_RE.search(t))


_TG_OPEN_RE = re.compile(r"\b(открой|открыть|покажи|зайди|перейди|open|show|go to)\b", re.IGNORECASE)
_TG_APP_RE = re.compile(r"(телеграм|телеге|телегу|\bтг\b|telegram|\btg\b)", re.IGNORECASE)
_TG_SAVED_RE = re.compile(r"(избранн|сохран[её]нн\w* сообщ|saved messages|saved)", re.IGNORECASE)
_TG_CHAT_INTENT_RE = re.compile(
    r"^(?:(?:эй|слушай|пожалуйста|please|hey)[, ]+)?"
    r"(?:открой|зайди|перейди|переключись|open|go to|switch to)\s+"
    r"(?:(?:в\s+)?(?:чат|переписку|диалог)\s+(?:с|со)\s+|(?:в\s+)?(?:чат|переписку|диалог)\s+|(?:the\s+)?chat\s+with\s+|к\s+)"
    r"(?P<who>.+?)"
    r"(?:\s+(?:в|во|на|on|in)\s+(?:телеграме?|телеграмме|телеге|тг|telegram|tg))?[.!?]*$",
    re.IGNORECASE)


def telegram_chat_intent(text: str, *, telegram_context: bool = False) -> str:
    """«открой чат с Nehto в телеграме» / «open chat with @durov on Telegram» → the chat name, else ''.
    Without an explicit Telegram mention only when ``telegram_context`` (Telegram is the active window
    or was just opened)."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or len(t) > 120:
        return ""
    m = _TG_CHAT_INTENT_RE.match(t)
    if not m:
        return ""
    if not (_TG_APP_RE.search(t) or telegram_context):
        return ""
    who = m.group("who").strip(" ,.«»\"'")
    who = _TG_APP_RE.sub("", who).strip(" ,.")
    if re.search(r"(?:^|\s)(?:с|со)\s+$", t[:m.start("who")], re.IGNORECASE):
        who = " ".join(ru_instrumental_to_nominative(w) for w in who.split())  # «чат с Мамой» → «Мама»
    return who if 0 < len(who) <= 64 else ""


def ru_instrumental_to_nominative(word: str) -> str:
    """«Мамой» → «Мама», «Сашей» → «Саша», «Олей» → «Оля», «Сергеем» → «Сергей», «Олегом» → «Олег».
    Unsure forms are cut to the stem («Игорем» → «Игор»): Telegram search is prefix based, so the stem
    still finds «Игорь». Latin / short words are returned unchanged."""
    w = word
    if len(w) < 4 or not re.fullmatch(r"[А-Яа-яЁё]+", w):
        return w
    low = w.lower()
    if low.endswith(("ой", "ою")):
        return w[:-2] + "а"
    if low.endswith(("ей", "ею")):
        return w[:-2] + ("а" if low[-3] in "жшчщц" else "я")
    if low.endswith("ем"):
        return w[:-2] + "й" if low[-3] in "еи" else w[:-2]
    if low.endswith(("ом", "ём")):
        return w[:-2]
    return w


def _telegram_window(ensure_open) -> tuple[int, bool, str]:
    preset = APP_SEARCH_PRESETS["telegram"]
    hwnd, opened, err = focus_app("Telegram", ensure_open=ensure_open, preset=preset, timeout=15.0)
    if not hwnd and not opened:
        # No Start-menu shortcut (portable install) or hidden in the tray: start the exe directly —
        # a second Telegram.exe just activates the running instance and shows its window.
        for exe in _telegram_exe_candidates():
            try:
                os.startfile(exe)  # type: ignore[attr-defined]
            except OSError:
                continue
            hwnd = wait_for_app_window(TELEGRAM_EXE, "", 15.0)
            if hwnd:
                opened = True
                time.sleep(1.2)
                hwnd = find_main_window_by_process(TELEGRAM_EXE, "telegram") or hwnd
                err = "" if focus_hwnd(hwnd, aggressive=True) else "не удалось вывести окно на передний план"
                break
    if not hwnd:
        return 0, opened, "не вижу окно Telegram Desktop — открой и войди в аккаунт"
    return hwnd, opened, err


def telegram_open_chat(target: str, *, ensure_open, driver=None) -> dict:
    """Open a chat in Telegram Desktop: Saved Messages (Ctrl+0), @username (tg://resolve) or by search
    (Esc, Esc → Ctrl+F → paste → wait → Enter on the first result). ``driver`` is for tests."""
    t = (target or "").strip()
    kind, value = telegram_target(t)
    if not kind:
        return {"ok": False, "error": "не указан чат"}
    if not IS_WIN and driver is None:
        return {"ok": False, "error": "Telegram Desktop доступен только в Windows"}
    opened = False
    if driver is None:
        hwnd, opened, err = _telegram_window(ensure_open)
        if not hwnd:
            return {"ok": False, "error": err, "hint": "Нужен установленный Telegram Desktop, не веб-версия."}
        driver = _WinTelegramDriver(hwnd)
        uri_ok = kind == "username" and _tg_protocol_is_desktop()
    else:
        uri_ok = bool(getattr(driver, "uri_ok", True))
    plan = telegram_plan(t, uri_ok=uri_ok, fresh=opened)
    try:
        res = run_telegram_plan(plan, driver)
        if not res.get("ok") and res.get("error") == "uri_failed":
            plan = telegram_plan(t, uri_ok=False)
            res = run_telegram_plan(plan, driver)
            uri_ok = False
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    if not res.get("ok"):
        return dict(res, opened_app=opened)
    via = "ctrl+0" if kind == "saved" else ("tg_protocol" if kind == "username" and uri_ok else "search")
    log.info("telegram_open_chat kind=%s via=%s len=%d opened=%s", kind, via, len(t), opened)
    out = {"ok": True, "chat": "Saved Messages / Избранное" if kind == "saved" else
           ("@" + value if kind == "username" else value), "kind": kind, "via": via, "opened_app": opened}
    if via == "search":
        out["note"] = ("Открыл первый результат поиска по чатам — если чат не тот, уточни имя "
                       "(как оно записано в Telegram, в именительном падеже) или дай @username.")
    return out


def send_telegram_desktop(contact: str, message: str, *, ensure_open, driver=None) -> dict:
    """Best-effort: open chat (Saved Messages / @username / search), paste message, Enter.

    ``ensure_open`` is a callable(name) that opens Telegram if needed (Actions.do_open_app).
    Does not log message body. Needs Telegram Desktop installed and logged in.
    """
    contact = (contact or "").strip()
    message = message if message is not None else ""
    if not contact:
        return {"ok": False, "error": "не указан контакт или чат"}
    if not str(message).strip():
        return {"ok": False, "error": "пустое сообщение"}
    if not IS_WIN and driver is None:
        return {"ok": False, "error": "отправка через Telegram Desktop доступна только в Windows"}
    opened = telegram_open_chat(contact, ensure_open=ensure_open, driver=driver)
    if not opened.get("ok"):
        return opened
    drv = driver
    if drv is None:
        drv = _WinTelegramDriver(find_main_window_by_process(TELEGRAM_EXE, "telegram"))
    # message field: paste WITHOUT Ctrl+A (would wipe a draft the user typed), then Enter
    steps = [("sleep", 0.25), ("paste", str(message), False), ("sleep", 0.15), ("keys", "enter"), ("sleep", 0.15)]
    res = run_telegram_plan(steps, drv)
    if not res.get("ok"):
        err = res.get("error") or ""
        return {"ok": False, "error": "не удалось положить текст в буфер обмена" if "буфер" in err else err,
                "chat_opened": True}
    # Log without message body
    log.info("send_telegram contact_len=%d msg_len=%d via=%s", len(contact), len(str(message)), opened.get("via"))
    return {
        "ok": True,
        "contact": opened.get("chat") or contact,
        "message_len": len(str(message)),
        "opened_app": bool(opened.get("opened_app")),
        "via": opened.get("via"),
        "note": "Best-effort: проверь, что сообщение ушло нужному человеку — поиск чата может промахнуться.",
    }


# ─── Full-tier desktop control (Windows, gated by pc_control=full) ────────────

SW_HIDE, SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE = 0, 6, 3, 9
WM_CLOSE = 0x0010
HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170

VK_MAP = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "ctrl": VK_CONTROL, "control": VK_CONTROL, "alt": 0x12, "menu": 0x12,
    "shift": 0x10, "capslock": 0x14,
}
for _i in range(1, 13):
    VK_MAP[f"f{_i}"] = 0x70 + _i - 1
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK_MAP[_c.lower()] = ord(_c)
for _d in "0123456789":
    VK_MAP[_d] = ord(_d)

# Commands that must NEVER run (even with confirm) — mass wipe / disk destroy.
REFUSE_COMMAND_RE = re.compile(
    r"("
    r"format\s+[a-z]:"
    r"|diskpart\b"
    r"|cipher\s+/w"
    r"|Remove-Item\s+[^\n]*-(Recurse|r)\b"
    r"|rm\s+(-[a-zA-Z]*[rR]|--recursive)"
    r"|(rmdir|rd)\s+/s"
    r"|del\s+/[a-zA-Z]*s"
    r"|Erase-Disk\b"
    r"|Clear-Disk\b"
    r"|Initialize-Disk\b"
    r")",
    re.IGNORECASE,
)

# Dangerous but allow with explicit confirm=true after user consent.
DANGEROUS_COMMAND_RE = re.compile(
    r"("
    r"shutdown\b|Restart-Computer\b|Stop-Computer\b"
    r"|reg\s+delete\b|Remove-ItemProperty\b"
    r"|net\s+user\b|net\s+localgroup\b"
    r"|takeown\b|icacls\b"
    r"|Remove-Item\b|del\s+|erase\s+|rmdir\b|\brd\s+"
    r"|taskkill\b|Stop-Process\b"
    r"|Start-Process\s+[^\n]*-Verb\s+RunAs"
    r"|msiexec\b|bcdedit\b"
    r")",
    re.IGNORECASE,
)

WINDOW_ACTIONS = frozenset({"close", "focus", "minimize", "maximize", "restore"})


def list_visible_windows(limit: int = 25) -> list[dict]:
    """Top-level visible windows with a non-empty title. Best-effort."""
    if not IS_WIN:
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out: list[dict] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @WNDENUMPROC
    def _enum(hwnd, _lparam):
        if len(out) >= max(1, int(limit)):
            return False
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = (buf.value or "").strip()
        if not title:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({"title": title, "hwnd": int(hwnd), "pid": int(pid.value)})
        return True

    try:
        user32.EnumWindows(_enum, 0)
    except Exception as e:
        log.info("list_visible_windows failed: %s", e)
    return out


def get_foreground_info() -> dict:
    """Active window title / process / pid (uses watcher when available)."""
    if not IS_WIN:
        return {"ok": False, "error": "только Windows"}
    try:
        from .watcher import get_foreground
        fg = get_foreground()
        return {
            "ok": True,
            "title": fg.title or "",
            "process": fg.process or "",
            "pid": fg.pid or 0,
            "hwnd": fg.hwnd or 0,
        }
    except Exception as e:
        # Fallback without watcher
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return {"ok": True, "title": "", "process": "", "pid": 0, "hwnd": 0}
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return {"ok": True, "title": buf.value or "", "process": "", "pid": int(pid.value),
                    "hwnd": int(hwnd)}
        except Exception as e2:
            return {"ok": False, "error": str(e2)[:200]}


def _is_own_window(hwnd: int, title: str = "") -> bool:
    """Avoid closing Jarvis itself by title / process heuristics."""
    t = (title or "").lower()
    if "джарвис" in t or "jarvis" in t:
        return True
    if not IS_WIN or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) == os.getpid():
            return True
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return False
        try:
            size = wintypes.DWORD(1024)
            pbuf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                base = os.path.basename(pbuf.value or "").lower()
                return base in ("jarvis.exe", "jarvis.pyw", "pythonw.exe") and "jarvis" in (pbuf.value or "").lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return False
    return False


def find_app_window(name: str) -> int:
    """Window by title substring, else by known app process (Spotify title is «Artist - Song»), else name.exe."""
    n = (name or "").strip()
    if not n:
        return 0
    hwnd = find_window_hwnd(n)
    if hwnd:
        return hwnd
    rp = resolve_app_preset(n)
    if rp:
        hwnd = find_window_by_process(rp[1]["exe"])
    if not hwnd and re.fullmatch(r"[A-Za-z0-9_.\-]{2,40}", n):
        hwnd = find_window_by_process([n.lower() if n.lower().endswith(".exe") else n.lower() + ".exe"])
    return hwnd


def window_action(action: str, name: str = "") -> dict:
    """close | focus | minimize | maximize | restore a top-level window by title substring."""
    act = (action or "").strip().lower()
    if act not in WINDOW_ACTIONS:
        return {"ok": False, "error": "действие: close, focus, minimize, maximize, restore"}
    if not IS_WIN:
        return {"ok": False, "error": "управление окнами доступно только в Windows"}
    needle = (name or "").strip()
    import ctypes
    user32 = ctypes.windll.user32
    if needle:
        hwnd = find_app_window(needle)
        if not hwnd:
            return {"ok": False, "error": f"не вижу окно «{needle}»",
                    "hint": "Передай часть заголовка окна. Список: get_active_window с list_windows=true."}
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value or needle
    else:
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return {"ok": False, "error": "нет активного окна"}
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value or ""

    if act == "close" and _is_own_window(hwnd, title):
        return {"ok": False, "error": "не закрываю своё окно (Jarvis)"}

    try:
        if act == "focus":
            ok = focus_hwnd(hwnd)
            return {"ok": ok, "action": act, "title": title} if ok else {
                "ok": False, "error": "не удалось вывести окно на передний план", "title": title}
        if act == "minimize":
            user32.ShowWindow(hwnd, SW_MINIMIZE)
        elif act == "maximize":
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
        elif act == "restore":
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
        elif act == "close":
            # Soft close — app may prompt to save. No TerminateProcess.
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return {"ok": True, "action": act, "title": title}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def type_text_into(text: str, window: str = "", *, press_enter: bool = False) -> dict:
    """Paste Unicode text into foreground or named window (clipboard + Ctrl+V). Never logs body."""
    if text is None or str(text) == "":
        return {"ok": False, "error": "пустой текст"}
    if not IS_WIN:
        return {"ok": False, "error": "ввод текста доступен только в Windows"}
    body = str(text)
    if len(body) > 8000:
        return {"ok": False, "error": "слишком длинный текст (макс. 8000 символов)"}
    win = (window or "").strip()
    if win:
        hwnd = find_app_window(win)
        if not hwnd:
            return {"ok": False, "error": f"не вижу окно «{win}»"}
        if not focus_hwnd(hwnd):
            return {"ok": False, "error": "не удалось сфокусировать окно"}
        time.sleep(0.2)
    if not _set_clipboard_text(body):
        return {"ok": False, "error": "не удалось положить текст в буфер обмена"}
    try:
        _paste_and_enter(press_enter=bool(press_enter))
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    log.info("type_text len=%d window=%r enter=%s", len(body), win or "(fg)", bool(press_enter))
    return {"ok": True, "typed_len": len(body), "window": win or None, "press_enter": bool(press_enter),
            "via": "clipboard_paste"}


def parse_hotkey(spec: str) -> tuple[list[int], int] | None:
    """'ctrl+shift+s' → ([VK_CONTROL, VK_SHIFT], ord('S')). Last token is the key."""
    raw = (spec or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    parts = [p for p in re.split(r"[+,\-]", raw) if p]
    if not parts:
        return None
    # Block secure-attention sequence
    joined = "+".join(parts)
    if joined in ("ctrl+alt+del", "ctrl+alt+delete", "control+alt+delete"):
        return None
    *mods_s, key_s = parts
    mods: list[int] = []
    for m in mods_s:
        vk = VK_MAP.get(m)
        if vk is None or m not in ("ctrl", "control", "alt", "menu", "shift", "win", "lwin", "rwin"):
            return None
        if vk not in mods:
            mods.append(vk)
    key = VK_MAP.get(key_s)
    if key is None:
        return None
    return mods, key


def press_hotkey(spec: str, window: str = "") -> dict:
    """Send a hotkey combo (or a ';'-separated sequence: «esc; ctrl+f», «down*3; enter») to a window."""
    if ";" in (spec or "") or "*" in (spec or ""):
        seq = parse_key_sequence(spec)
        if not seq:
            return {"ok": False, "error": "не понял последовательность клавиш",
                    "hint": "шаги через «;», повтор через *N: esc; ctrl+f или down*3; enter"}
        if not IS_WIN:
            return {"ok": False, "error": "горячие клавиши доступны только в Windows"}
        win = (window or "").strip()
        if win:
            hwnd = find_app_window(win)
            if not hwnd or not focus_hwnd(hwnd):
                return {"ok": False, "error": f"не вижу окно «{win}»"}
            time.sleep(0.15)
        try:
            send_key_sequence(seq)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
        return {"ok": True, "hotkey": spec.strip().lower(), "steps": len(seq), "window": win or None}
    parsed = parse_hotkey(spec)
    if not parsed:
        low = (spec or "").strip().lower().replace(" ", "")
        if "ctrl+alt+del" in low or "ctrl+alt+delete" in low:
            return {"ok": False, "error": "Ctrl+Alt+Del нельзя эмулировать (защита Windows)"}
        return {"ok": False, "error": "не понял комбинацию", "hint": "например: ctrl+c, alt+f4, win+d, ctrl+shift+esc"}
    if not IS_WIN:
        return {"ok": False, "error": "горячие клавиши доступны только в Windows"}
    win = (window or "").strip()
    if win:
        hwnd = find_app_window(win)
        if not hwnd:
            return {"ok": False, "error": f"не вижу окно «{win}»"}
        if not focus_hwnd(hwnd):
            return {"ok": False, "error": "не удалось сфокусировать окно"}
        time.sleep(0.15)
    mods, key = parsed
    try:
        _key_combo(mods, key)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "hotkey": spec.strip().lower(), "window": win or None}


def command_risk(command: str) -> str:
    """'refuse' | 'confirm' | 'ok'"""
    c = command or ""
    if REFUSE_COMMAND_RE.search(c):
        return "refuse"
    if DANGEROUS_COMMAND_RE.search(c):
        return "confirm"
    return "ok"


def run_shell_command(command: str, *, shell: str = "cmd", confirm: bool = False,
                      timeout: float = 30.0) -> dict:
    """Run a non-elevated shell command. Dangerous ops need confirm=True; wipe/format refused."""
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "пустая команда"}
    if len(cmd) > 4000:
        return {"ok": False, "error": "слишком длинная команда"}
    risk = command_risk(cmd)
    if risk == "refuse":
        return {"ok": False, "error": "отказ: массовое удаление / форматирование диска запрещены "
                "(нет инструмента «стереть всё»). Укажи точечное действие или сделай вручную."}
    if risk == "confirm" and not confirm:
        return {
            "ok": False,
            "needs_confirm": True,
            "error": "опасная команда — нужно явное согласие пользователя",
            "hint": "Спроси пользователя. Если сказал «да» — вызови снова с confirm=true.",
            "command_preview": cmd[:200],
        }
    sh = (shell or "cmd").strip().lower()
    if sh not in ("cmd", "powershell", "pwsh"):
        return {"ok": False, "error": "shell: cmd или powershell"}
    if not IS_WIN:
        return {"ok": False, "error": "shell-команды доступны только в Windows"}
    try:
        import subprocess
        if sh == "cmd":
            args = ["cmd.exe", "/d", "/c", cmd]
        else:
            exe = "pwsh.exe" if sh == "pwsh" else "powershell.exe"
            args = [exe, "-NoProfile", "-NonInteractive", "-Command", cmd]
        kw: dict = {"capture_output": True, "timeout": max(1.0, min(float(timeout), 120.0)),
                    "cwd": os.path.expanduser("~")}
        # CREATE_NO_WINDOW
        kw["creationflags"] = 0x08000000
        r = subprocess.run(args, **kw)
        def _dec(b: bytes) -> str:
            for enc in ("utf-8", "cp866", "cp1251"):
                try:
                    return b.decode(enc)
                except UnicodeDecodeError:
                    continue
            return b.decode("utf-8", errors="replace")
        out = _dec(r.stdout or b"")[:4000]
        err = _dec(r.stderr or b"")[:1500]
        log.info("run_command shell=%s risk=%s len=%d rc=%s", sh, risk, len(cmd), r.returncode)
        return {
            "ok": r.returncode == 0,
            "returncode": int(r.returncode),
            "stdout": out,
            "stderr": err,
            "shell": sh,
            "confirmed": bool(confirm) if risk == "confirm" else False,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def lock_workstation() -> dict:
    if not IS_WIN:
        return {"ok": False, "error": "блокировка только в Windows"}
    try:
        import ctypes
        ok = bool(ctypes.windll.user32.LockWorkStation())
        return {"ok": ok} if ok else {"ok": False, "error": "LockWorkStation вернул отказ"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def system_power_action(action: str) -> dict:
    """sleep | monitor_off. Clearly labeled; no forced reboot/shutdown here."""
    act = (action or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "sleep": "sleep", "suspend": "sleep", "сон": "sleep", "спящий": "sleep",
        "monitor_off": "monitor_off", "monitor": "monitor_off", "экран_выкл": "monitor_off",
        "выключить_экран": "monitor_off", "гасить_экран": "monitor_off",
    }
    act = aliases.get(act, act)
    if act not in ("sleep", "monitor_off"):
        return {"ok": False, "error": "action: sleep или monitor_off (перезагрузка/выключение ПК — не через этот инструмент)"}
    if not IS_WIN:
        return {"ok": False, "error": "только Windows"}
    try:
        import ctypes
        if act == "monitor_off":
            # HWND_BROADCAST + SC_MONITORPOWER + 2 (off)
            ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)
            return {"ok": True, "action": "monitor_off",
                    "note": "Монитор погашен. Движение мыши или клавиша разбудит."}
        # SetSuspendState(Hibernate=False, ForceCritical=True, DisableWakeEvent=False)
        ok = bool(ctypes.windll.powrprof.SetSuspendState(False, True, False))
        return {"ok": ok, "action": "sleep",
                "note": "Запрос сна отправлен. Если политика питания запрещает — не сработает."} if ok else {
            "ok": False, "error": "не удалось уйти в сон (проверь схему электропитания)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
