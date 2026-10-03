"""Version manager over public GitHub Releases (Sinohara1/jarvis).

No token needed (unauthenticated public API, 60 requests/hour per IP). Every
release that has a Jarvis.exe asset can be installed: newer (update) or older
(rollback). The swap is done by a small update.bat that waits for this process
to exit, replaces Jarvis.exe and starts it again. User data in
%LOCALAPPDATA%\\Jarvis is never touched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

from . import APP_VERSION

log = logging.getLogger("jarvis")

UPDATE_REPO = "Sinohara1/jarvis"
UPDATE_ASSET_NAME = "Jarvis.exe"
GH_API = "https://api.github.com"
GH_UA = f"Jarvis/{APP_VERSION}"
REPO_URL = f"https://github.com/{UPDATE_REPO}"

_SEMVER_RE = re.compile(r"^v?\s*(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?\s*$", re.IGNORECASE)
_cache: dict = {"t": 0.0, "data": None}


def parse_semver(v: str) -> tuple[int, int, int] | None:
    m = _SEMVER_RE.match(v.strip()) if isinstance(v, str) else None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_newer_version(remote: str, current: str = APP_VERSION) -> bool:
    a, b = parse_semver(remote), parse_semver(current)
    if a is None:
        return False
    if b is None:
        return True
    return a > b


def compare(a: str, b: str) -> int:
    pa, pb = parse_semver(a) or (0, 0, 0), parse_semver(b) or (0, 0, 0)
    return (pa > pb) - (pa < pb)


def _get_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": GH_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _short_notes(body: str, limit: int = 280) -> str:
    lines = []
    for ln in (body or "").splitlines():
        ln = ln.strip().lstrip("#").strip()
        if not ln:
            continue
        ln = re.sub(r"^[-*•]\s*", "• ", ln)
        ln = re.sub(r"[*_`]", "", ln)
        lines.append(ln)
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def list_releases(force: bool = False, current: str = APP_VERSION) -> list[dict]:
    """All installable releases, newest first. Raises on network errors."""
    if not force and _cache["data"] is not None and time.time() - _cache["t"] < 120:
        raw = _cache["data"]
    else:
        raw = _get_json(f"{GH_API}/repos/{UPDATE_REPO}/releases?per_page=50")
        _cache.update(t=time.time(), data=raw)
    out = []
    for rel in raw if isinstance(raw, list) else []:
        if rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "")
        if parse_semver(tag) is None:
            continue
        asset = next((a for a in rel.get("assets") or [] if a.get("name") == UPDATE_ASSET_NAME), None)
        if not asset:
            continue
        date = ""
        try:
            dt = datetime.fromisoformat(str(rel.get("published_at") or "").replace("Z", "+00:00"))
            date = dt.astimezone().strftime("%d.%m.%Y")
        except ValueError:
            pass
        c = compare(tag, current)
        out.append({
            "tag": tag, "version": tag.lstrip("vV"), "name": rel.get("name") or tag, "date": date,
            "notes": _short_notes(rel.get("body") or ""), "url": asset.get("browser_download_url"),
            "size": int(asset.get("size") or 0), "digest": str(asset.get("digest") or ""), "prerelease": bool(rel.get("prerelease")),
            "status": "current" if c == 0 else ("newer" if c > 0 else "older"), "page": rel.get("html_url"),
        })
    out.sort(key=lambda r: parse_semver(r["tag"]) or (0, 0, 0), reverse=True)
    return out


def check_latest(settings: dict | None = None) -> dict | None:
    """Newest non-prerelease release newer than the running one, else None (quiet offline)."""
    try:
        for r in list_releases(force=True):
            if not r["prerelease"]:
                return r if r["status"] == "newer" else None
    except Exception as e:
        log.info("update check skipped: %s", e)
    return None


def download(release: dict, on_progress=None) -> str:
    """Download the release's Jarvis.exe to a temp folder; returns the file path."""
    url = release["url"]
    req = urllib.request.Request(url, headers={"User-Agent": GH_UA, "Accept": "application/octet-stream"})
    tmp = tempfile.mkdtemp(prefix="jarvis_v_")
    path = os.path.join(tmp, UPDATE_ASSET_NAME)
    total = int(release.get("size") or 0)
    done = 0
    last = 0.0
    sha = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        total = total or int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            sha.update(chunk)
            done += len(chunk)
            if on_progress and (time.monotonic() - last > 0.15 or done == total):
                last = time.monotonic()
                on_progress(done, total)
    if total and done != total:
        raise RuntimeError(f"файл скачался не полностью ({done} из {total} байт)")
    digest = str(release.get("digest") or "")
    if digest.startswith("sha256:") and digest[7:].lower() != sha.hexdigest():
        raise RuntimeError("контрольная сумма не совпала — файл повреждён, попробуй ещё раз")
    with open(path, "rb") as f:
        if f.read(2) != b"MZ":
            raise RuntimeError("скачанный файл не похож на программу")
    return path


# PyInstaller onefile passes these to its child process. If they leak into the relaunched exe, its
# bootloader thinks it is that child and loads python3xx.dll from the OLD _MEI folder, which the old
# process has just deleted → "Failed to load Python DLL …\_MEIxxxx\python313.dll".
_PYI_ENV_PREFIXES = ("_PYI_", "_MEIPASS", "JARVIS_")


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_PYI_ENV_PREFIXES)}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # PyInstaller ≥ 6.9: start as an independent instance
    return env


def write_swap_bat(new_exe: str, target: str, pids: list[int], args: list[str] | None = None,
                   log_path: str | None = None) -> str:
    """Batch file: wait (max ~40 s) for our processes to exit, replace target (retry while locked),
    verify the size, start it only after a successful copy, delete itself. Steps go to update.log."""
    bat = os.path.join(os.path.dirname(new_exe), "jarvis_swap.bat")
    size = os.path.getsize(new_exe) if os.path.exists(new_exe) else 0
    log = log_path or os.path.join(os.path.dirname(new_exe), "update.log")
    L = f' >>"{log}" 2>&1'
    checks = "".join(f'tasklist /FI "PID eq {p}" 2>nul | find " {p} " >nul && set alive=1\r\n' for p in pids)
    kills = "".join(f"  taskkill /F /PID {p} >nul 2>&1\r\n" for p in pids)
    argstr = " ".join(f'"{a}"' for a in (args or []))
    content = (
        "@echo off\r\nchcp 65001 >nul\r\nsetlocal EnableDelayedExpansion\r\n"
        "set PYINSTALLER_RESET_ENVIRONMENT=1\r\nset _PYI_APPLICATION_HOME_DIR=\r\nset _PYI_ARCHIVE_FILE=\r\n"
        "set _PYI_PARENT_PROCESS_LEVEL=\r\nset _PYI_SPLASH_IPC=\r\nset _MEIPASS2=\r\n"
        f"echo [%date% %time%] swap start: {target}{L}\r\n"
        "set n=0\r\n:wait\r\nset alive=0\r\n" + checks +
        "if !alive!==1 (\r\n  set /a n+=1\r\n  if !n! lss 40 (\r\n    ping -n 2 127.0.0.1 >nul\r\n    goto wait\r\n  )\r\n"
        f"  echo [%time%] old process did not exit, killing{L}\r\n" + kills +
        "  ping -n 3 127.0.0.1 >nul\r\n)\r\n"
        f"echo [%time%] old process exited{L}\r\n"
        "set c=0\r\n:copy\r\n"
        f'copy /Y /B "{new_exe}" "{target}" >nul 2>&1\r\n'
        "if errorlevel 1 (\r\n  set /a c+=1\r\n  if !c! lss 30 (\r\n    ping -n 2 127.0.0.1 >nul\r\n    goto copy\r\n  )\r\n"
        f"  echo [%time%] copy FAILED, starting the old version{L}\r\n  goto run\r\n)\r\n"
        f'for %%A in ("{target}") do set sz=%%~zA\r\n'
        f"if not \"!sz!\"==\"{size}\" (\r\n  echo [%time%] size mismatch !sz! vs {size}, retrying{L}\r\n"
        "  set /a c+=1\r\n  if !c! lss 30 (\r\n    ping -n 2 127.0.0.1 >nul\r\n    goto copy\r\n  )\r\n)\r\n"
        f"echo [%time%] copied OK (!sz! bytes){L}\r\n"
        ":run\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        f'start "" "{target}" {argstr}\r\n'
        f"echo [%time%] started{L}\r\n"
        f'del /F /Q "{new_exe}" >nul 2>&1\r\n'
        '(goto) 2>nul & del /F /Q "%~f0"\r\n'
    )
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return bat


def _process_image(pid: int) -> str:
    if sys.platform != "win32":
        return ""
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.c_ulong(1024)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return ""
    finally:
        k.CloseHandle(h)


def launch_swap(bat: str) -> None:
    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP — survives our exit, no console flash
    flags = (0x08000000 | 0x00000200) if sys.platform == "win32" else 0
    subprocess.Popen(["cmd.exe", "/c", bat], cwd=os.path.dirname(bat), creationflags=flags, close_fds=True, env=clean_env())


def install(release: dict, on_progress=None) -> str:
    """Download + schedule the swap. Caller must quit the app right after. Returns bat path."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("установка версий работает только в Jarvis.exe")
    new_exe = download(release, on_progress)
    target = os.path.abspath(sys.executable)
    pids = [os.getpid()]
    try:
        # PyInstaller onefile: the parent bootloader is the same Jarvis.exe — wait for it too,
        # but only if it really is our exe (never touch any other process).
        ppid = os.getppid()
        if ppid and ppid != os.getpid() and os.path.normcase(_process_image(ppid)) == os.path.normcase(target):
            pids.append(ppid)
    except Exception:
        pass
    try:
        from .config import DATA_DIR, ensure_dir
        ensure_dir(DATA_DIR)
        log_path = os.path.join(DATA_DIR, "update.log")
    except Exception:
        log_path = None
    args = [a for a in sys.argv[1:] if a == "--minimized"]  # relaunch the same way (tray or window)
    bat = write_swap_bat(new_exe, target, pids, args, log_path)
    launch_swap(bat)
    log.info("version switch scheduled: %s -> %s (%s)", APP_VERSION, release.get("tag"), target)
    return bat
