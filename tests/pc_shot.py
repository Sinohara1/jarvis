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
hwnd = u.FindWindowW(None, sys.argv[2] if len(sys.argv) > 2 else "Джарвис")
if not hwnd:
    print("window not found"); sys.exit(1)
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
