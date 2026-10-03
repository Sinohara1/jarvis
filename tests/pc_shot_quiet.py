"""Capture the (tray-hidden) Jarvis window without activating it: show it off-screen, PrintWindow, hide again.
usage: python pc_shot_quiet.py out.png"""
import ctypes, sys, time, subprocess
from ctypes import wintypes as W
from PIL import Image
u, g = ctypes.windll.user32, ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Jarvis.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
pids = {int(l.split('","')[1]) for l in out.splitlines() if l.startswith('"Jarvis.exe"')}
found = []
cls = ctypes.create_unicode_buffer(200)
def cb(h, l):
    pid = W.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    if pid.value in pids and u.GetClassNameW(h, cls, 200) and cls.value.startswith("WindowsForms") and u.GetWindowTextLengthW(h):
        found.append(h)
    return True
u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)(cb), 0)
if not found:
    print("window not found"); sys.exit(1)
hwnd = found[0]
was_visible = u.IsWindowVisible(hwnd)
SWP = 0x0001 | 0x0004 | 0x0010  # NOSIZE | NOZORDER | NOACTIVATE
r = W.RECT(); u.GetWindowRect(hwnd, ctypes.byref(r))
if not was_visible:
    u.SetWindowPos(hwnd, 0, -5000, 50, 0, 0, SWP)
    u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 3.0)
u.GetWindowRect(hwnd, ctypes.byref(r))
w, h = r.right - r.left, r.bottom - r.top
hdc = u.GetWindowDC(hwnd); mdc = g.CreateCompatibleDC(hdc); bmp = g.CreateCompatibleBitmap(hdc, w, h)
g.SelectObject(mdc, bmp)
ok = u.PrintWindow(hwnd, mdc, 2)
class BIH(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD), ("biBitCount", W.WORD),
                ("biCompression", W.DWORD), ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG),
                ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]
bi = BIH(); bi.biSize = ctypes.sizeof(BIH); bi.biWidth = w; bi.biHeight = -h; bi.biPlanes = 1; bi.biBitCount = 32
buf = ctypes.create_string_buffer(w * h * 4)
g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
g.DeleteObject(bmp); g.DeleteDC(mdc); u.ReleaseDC(hwnd, hdc)
if not was_visible:
    u.ShowWindow(hwnd, 0)  # back to tray
img.save(sys.argv[1])
print("saved", img.size, "printwindow ok" if ok else "printwindow failed", "was_visible", bool(was_visible))
