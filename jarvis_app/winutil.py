"""Windows helpers: autostart (HKCU Run), single instance, launch command."""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading

log = logging.getLogger("jarvis")
IS_WIN = sys.platform == "win32"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Jarvis"
INSTANCE_PORT = 47653


def launch_command(minimized: bool = True) -> str:
    if getattr(sys, "frozen", False):
        cmd = f'"{os.path.abspath(sys.executable)}"'
    else:
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pyw):
            exe = pyw
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jarvis.pyw")
        cmd = f'"{exe}" "{script}"'
    return cmd + (" --minimized" if minimized else "")


def set_autostart(enabled: bool) -> bool:
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, launch_command(True))
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        log.warning("autostart change failed: %s", e)
        return False


def get_autostart() -> bool:
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


class SingleInstance:
    """First instance listens on localhost; later ones ask it to show itself."""

    def __init__(self) -> None:
        self.sock: socket.socket | None = None

    def acquire(self, on_show) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", INSTANCE_PORT))
            s.listen(2)
        except OSError:
            s.close()
            try:
                with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1.5) as c:
                    c.sendall(b"show\n")
            except OSError:
                return True  # port busy by something else: just run
            return False
        self.sock = s

        def serve() -> None:
            while True:
                try:
                    conn, _ = s.accept()
                except OSError:
                    return
                try:
                    data = conn.recv(64)
                    if data.startswith(b"show"):
                        on_show()
                except OSError:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=serve, name="single-instance", daemon=True).start()
        return True

    def release(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
