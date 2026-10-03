import ctypes, ctypes.wintypes as W
u = ctypes.windll.user32
res = []
def cb(h, l):
    n = u.GetWindowTextLengthW(h)
    if n:
        b = ctypes.create_unicode_buffer(n + 1); u.GetWindowTextW(h, b, n + 1)
        if "Джарвис" in b.value or "Jarvis" in b.value:
            r = W.RECT(); u.GetWindowRect(h, ctypes.byref(r))
            pid = W.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            cls = ctypes.create_unicode_buffer(200); u.GetClassNameW(h, cls, 200)
            res.append((h, b.value, cls.value, pid.value, bool(u.IsWindowVisible(h)), (r.left, r.top, r.right, r.bottom)))
    return True
u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)(cb), 0)
for x in res: print(x)
