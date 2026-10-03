"""Capture the Jarvis window even if it is covered (PrintWindow + PW_RENDERFULLCONTENT).
usage: python pc_shot.py out.png [window title]"""
import ctypes, sys
from ctypes import wintypes as W
from PIL import Image
u, g = ctypes.windll.user32, ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
def find_main():
    """Largest visible top-level window owned by a Jarvis.exe process (title-independent)."""
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Jarvis.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    pids = {int(l.split('","')[1]) for l in out.splitlines() if l.startswith('"Jarvis.exe"')}
    best = [0, 0]
    cls = ctypes.create_unicode_buffer(200)
    def cb(h, l):
        pid = W.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(h) and u.GetClassNameW(h, cls, 200) and cls.value.startswith('WindowsForms'):
            r = W.RECT(); u.GetWindowRect(h, ctypes.byref(r))
            a = (r.right - r.left) * (r.bottom - r.top)
            if a > best[1]:
                best[:] = [h, a]
        return True
    u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)(cb), 0)
    return best[0]


hwnd = u.FindWindowW(None, sys.argv[2]) if len(sys.argv) > 2 else find_main()
if not hwnd:
    print("window not found"); sys.exit(1)
if u.IsIconic(hwnd):  # minimized → restore without stealing focus, PrintWindow can't render icons
    import time
    u.ShowWindow(hwnd, 4); time.sleep(1.2)
r = W.RECT(); u.GetWindowRect(hwnd, ctypes.byref(r))
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
img.save(sys.argv[1] if len(sys.argv) > 1 else "screenshot.png")
print("saved", img.size, "printwindow ok" if ok else "printwindow failed")
