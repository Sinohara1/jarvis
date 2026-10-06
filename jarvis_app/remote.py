"""Телефонный пульт к Джарвису на этом компьютере.

Страница и API слушают локальную сеть: телефон пишет в чат Джарвиса на ПК,
ответ (и озвучка) идут с компьютера. Без PIN и токена связи нет.
В интернет порт сам не пробрасывается — и не пробрасывай его вручную.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import DATA_DIR, bundle_dir, ensure_dir

log = logging.getLogger("jarvis.phone")
PORT = 8787
PHONE_FILE = os.path.join(DATA_DIR, "phone.json")


def lan_ips() -> list[str]:
    found: list[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        found.append(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found or ["127.0.0.1"]


def phone_dir() -> str:
    bundled = os.path.join(bundle_dir(), "phone")
    if os.path.isdir(bundled):
        return bundled
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "phone")
    return here


class PhoneServer:
    def __init__(self, core) -> None:
        self.core = core
        self.port = PORT
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.pin, self.token = self._load_pair()

    def _load_pair(self) -> tuple[str, str]:
        try:
            with open(PHONE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            pin, token = str(data.get("pin") or ""), str(data.get("token") or "")
            if len(pin) == 6 and token:
                return pin, token
        except (OSError, json.JSONDecodeError):
            pass
        pin = f"{secrets.randbelow(1000000):06d}"
        token = secrets.token_urlsafe(24)
        self._write_pair(pin, token)
        return pin, token

    def _write_pair(self, pin: str, token: str) -> None:
        ensure_dir(DATA_DIR)
        tmp = PHONE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pin": pin, "token": token}, f)
        os.replace(tmp, PHONE_FILE)

    def rotate(self) -> None:
        pin = f"{secrets.randbelow(1000000):06d}"
        token = secrets.token_urlsafe(24)
        with self._lock:
            self.pin, self.token = pin, token
        self._write_pair(pin, token)

    def info(self) -> dict:
        ips = lan_ips()
        return {
            "ok": True,
            "enabled": self._httpd is not None,
            "port": self.port,
            "pin": self.pin,
            "ips": ips,
            "url": f"http://{ips[0]}:{self.port}",
        }

    def start(self) -> None:
        if self._httpd is not None:
            return
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                log.debug("phone " + fmt, *args)

            def _json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 1_500_000:
                    return {}
                raw = self.rfile.read(n)
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return {}
                return data if isinstance(data, dict) else {}

            def _auth(self) -> bool:
                header = self.headers.get("Authorization") or ""
                return header == f"Bearer {server.token}"

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/api/info":
                    self._json(200, {"ok": True, "name": server._name(), "paired": False})
                    return
                if path == "/api/state":
                    if not self._auth():
                        self._json(401, {"ok": False, "error": "нужен PIN"})
                        return
                    self._json(200, server.snapshot())
                    return
                self._file(path)

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                data = self._body()
                if path == "/api/pair":
                    if str(data.get("pin") or "") != server.pin:
                        self._json(403, {"ok": False, "error": "неверный PIN"})
                        return
                    self._json(200, {"ok": True, "token": server.token, "name": server._name()})
                    return
                if not self._auth():
                    self._json(401, {"ok": False, "error": "нужен PIN"})
                    return
                if path == "/api/chat":
                    text = str(data.get("text") or "").strip()
                    if not text:
                        self._json(400, {"ok": False, "error": "пусто"})
                        return
                    reply = server.ask(text, speak=bool(data.get("speak")))
                    self._json(200, {"ok": True, "reply": reply})
                    return
                self._json(404, {"ok": False, "error": "нет такого пути"})

            def _file(self, path: str) -> None:
                root = os.path.abspath(phone_dir())
                rel = "index.html" if path in {"/", "/phone", "/phone/"} else path.lstrip("/")
                full = os.path.abspath(os.path.join(root, rel))
                if not full.startswith(root) or not os.path.isfile(full):
                    self._json(404, {"ok": False, "error": "нет страницы"})
                    return
                kind = "text/html"
                if full.endswith(".js"):
                    kind = "text/javascript"
                elif full.endswith(".json"):
                    kind = "application/json"
                elif full.endswith(".svg"):
                    kind = "image/svg+xml"
                elif full.endswith(".css"):
                    kind = "text/css"
                with open(full, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", f"{kind}; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        try:
            self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        except OSError as e:
            log.warning("phone port %s busy: %s", self.port, e)
            self._httpd = None
            return
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="phone", daemon=True)
        self._thread.start()
        log.info("phone ready at %s pin %s", self.info()["url"], self.pin)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None

    def _name(self) -> str:
        return str(self.core.settings.get("assistant_name") or "Джарвис")

    def snapshot(self) -> dict:
        return {"ok": True, "name": self._name()}

    def ask(self, text: str, speak: bool = False) -> str:
        box: dict = {"reply": "", "event": threading.Event(), "error": ""}
        self.core._jobs.put(("phone", (text, speak, box)))
        if not box["event"].wait(100):
            return "ПК не успел ответить."
        return box.get("error") or box.get("reply") or "…"
