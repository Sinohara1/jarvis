"""Spotify «play X» — search a track and actually start it (Windows desktop app).

Order of attempts:
1. Optional Spotify Web API (only if the user configured credentials; never required):
   settings ``spotify_client_id`` / ``spotify_client_secret`` / ``spotify_refresh_token``
   (or env JARVIS_SPOTIFY_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN). Search → play on the active
   device (needs Premium + a running Spotify). Tokens are never logged.
2. UI automation of the desktop app: find/open Spotify → focus → Ctrl+K (quick search) → paste query
   → Enter, which plays the top result. Verified by the window title (Spotify shows «Artist - Song»
   while playing).
3. Fallback: ``spotify:search:QUERY`` URI (opens the results page) + Enter. If playback still can't be
   confirmed we report ok with ``verified: false`` so the assistant can say «открыл поиск».
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import pc_power as pp

log = logging.getLogger("jarvis")

SPOTIFY_EXE = ["spotify.exe"]
IDLE_TITLES = {"spotify", "spotify premium", "spotify free", ""}

# «в спотике», «на спотифае», «on spotify», «in Spotify» …
_APP_WORDS = (r"(?:spotify|спотифа[йяюеи]\w*|спотифи\w*|спотик\w*|споти\w*|спотт\w*)")
_TAIL_RE = re.compile(r"\s*(?:\b(?:в|во|на|через|по|in|on|from|via|using|with)\s+)?" + _APP_WORDS + r"\s*[.!?]*\s*$",
                      re.IGNORECASE)
_HEAD_APP_RE = re.compile(r"^\s*(?:(?:в|на|in|on)\s+)?" + _APP_WORDS + r"[,:]?\s+", re.IGNORECASE)
_VERB_RE = re.compile(
    r"^\s*(?:(?:джарвис|jarvis|пожалуйста|please|hey|эй)[,\s]+)*"
    r"(?:включи(?:ть)?|вруби(?:ть)?|поставь|постав(?:ить)?|запусти(?:ть)?|сыграй|играй|проиграй|воспроизведи|"
    r"найди и включи|найди|хочу послушать|давай|play|put on|start playing|start|listen to|find and play|queue)\b[,\s]*",
    re.IGNORECASE)
_KIND_RE = re.compile(
    r"^\s*(?:(?:мне|me)\s+)?(?:песню|песня|песенку|трек|трэк|композицию|альбом|плейлист|музыку|song|track|the song|"
    r"the track|music|some)\b[,\s]*", re.IGNORECASE)
_POLITE_TAIL_RE = re.compile(r"[,\s]*(?:пожалуйста|please|плиз)\s*[.!?]*\s*$", re.IGNORECASE)
_GENERIC = {"", "музыку", "музыка", "music", "something", "что-нибудь", "что нибудь", "чтонибудь", "песню",
            "трек", "song", "track", "some music", "мою музыку", "my music"}


def clean_query(text: str) -> str:
    """Strip «включи … в спотике» / «play … on Spotify» wrappers, keep the track/artist words."""
    q = (text or "").strip().strip("«»\"'“”„")
    for _ in range(3):
        before = q
        q = _POLITE_TAIL_RE.sub("", q)
        q = _TAIL_RE.sub("", q)
        q = _VERB_RE.sub("", q)
        q = _HEAD_APP_RE.sub("", q)
        q = _KIND_RE.sub("", q)
        q = q.strip(" ,.:;!?-—").strip("«»\"'“”„")
        if q == before:
            break
    return re.sub(r"\s+", " ", q).strip()


def is_generic(query: str) -> bool:
    return (query or "").strip().lower() in _GENERIC


_INTENT_VERB = re.compile(r"\b(включи|вруби|поставь|запусти|сыграй|играй|воспроизведи|play|put on)\b", re.IGNORECASE)
_INTENT_APP = re.compile(_APP_WORDS, re.IGNORECASE)


def spotify_play_intent(text: str) -> str | None:
    """Return the track query when text clearly says «play <X> on Spotify», else None."""
    t = (text or "").strip()
    if not t or len(t) > 160:
        return None
    if not (_INTENT_VERB.search(t) and _INTENT_APP.search(t)):
        return None
    q = clean_query(t)
    if is_generic(q) or _INTENT_APP.fullmatch(q or "x"):
        return None
    return q


def search_uri(query: str) -> str:
    return "spotify:search:" + urllib.parse.quote(query.strip(), safe="")


# ─── optional Web API ────────────────────────────────────────────────────────

_token_cache: dict = {"token": "", "exp": 0.0, "key": ""}


def api_credentials(settings: dict | None) -> tuple[str, str, str] | None:
    s = settings or {}
    cid = str(s.get("spotify_client_id") or os.environ.get("JARVIS_SPOTIFY_CLIENT_ID") or "").strip()
    sec = str(s.get("spotify_client_secret") or os.environ.get("JARVIS_SPOTIFY_CLIENT_SECRET") or "").strip()
    ref = str(s.get("spotify_refresh_token") or os.environ.get("JARVIS_SPOTIFY_REFRESH_TOKEN") or "").strip()
    return (cid, sec, ref) if cid and sec and ref else None


def _http(method: str, url: str, *, headers: dict, data: bytes | None = None, timeout: float = 6.0):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        return e.code, body


def _access_token(creds: tuple[str, str, str]) -> str:
    cid, sec, ref = creds
    key = cid + ":" + ref[-6:]
    if _token_cache["token"] and _token_cache["key"] == key and time.time() < _token_cache["exp"] - 30:
        return _token_cache["token"]
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": ref}).encode()
    st, body = _http("POST", "https://accounts.spotify.com/api/token", data=data,
                     headers={"Authorization": "Basic " + basic,
                              "Content-Type": "application/x-www-form-urlencoded"})
    tok = str((body or {}).get("access_token") or "")
    if st != 200 or not tok:
        log.info("spotify api: token refresh failed status=%s", st)  # no secrets in log
        return ""
    _token_cache.update(token=tok, exp=time.time() + float(body.get("expires_in") or 3600), key=key)
    return tok


def webapi_play(query: str, creds: tuple[str, str, str]) -> dict:
    tok = _access_token(creds)
    if not tok:
        return {"ok": False, "error": "spotify api: нет токена"}
    h = {"Authorization": "Bearer " + tok}
    st, body = _http("GET", "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
        {"q": query, "type": "track", "limit": 1, "market": "from_token"}), headers=h)
    items = (((body or {}).get("tracks") or {}).get("items") or []) if st == 200 else []
    if not items:
        return {"ok": False, "error": f"spotify api: поиск не дал результатов (status {st})"}
    tr = items[0]
    uri = tr.get("uri") or ""
    title = f"{', '.join(a.get('name', '') for a in tr.get('artists') or [])} - {tr.get('name', '')}".strip(" -")
    hj = dict(h, **{"Content-Type": "application/json"})
    payload = json.dumps({"uris": [uri]}).encode()
    st, body = _http("PUT", "https://api.spotify.com/v1/me/player/play", headers=hj, data=payload)
    if st == 404:  # no active device → pick one
        st2, devs = _http("GET", "https://api.spotify.com/v1/me/player/devices", headers=h)
        dlist = (devs or {}).get("devices") or [] if st2 == 200 else []
        dev = next((d for d in dlist if d.get("type") == "Computer"), dlist[0] if dlist else None)
        if dev and dev.get("id"):
            st, body = _http("PUT", "https://api.spotify.com/v1/me/player/play?device_id="
                             + urllib.parse.quote(dev["id"]), headers=hj, data=payload)
    if st in (200, 202, 204):
        return {"ok": True, "via": "web_api", "track": title, "uri": uri, "verified": True}
    reason = str(((body or {}).get("error") or {}).get("reason") or ((body or {}).get("error") or {}).get("message") or "")
    return {"ok": False, "error": f"spotify api: play status {st} {reason[:80]}".strip(), "track": title,
            "uri": uri}


# ─── desktop UI automation ───────────────────────────────────────────────────

def _wait_playing(hwnd: int, before: str, timeout: float = 4.0) -> str:
    """Poll the Spotify window title; return «Artist - Song» once it shows a (new) track, else ''."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        time.sleep(0.3)
        h = pp.find_window_by_process(SPOTIFY_EXE) or hwnd
        title = pp.window_title(h).strip()
        if title.lower() not in IDLE_TITLES and title != before:
            return title
    return ""


def ui_play(query: str, *, ensure_open) -> dict:
    if not pp.IS_WIN:
        return {"ok": False, "error": "управление Spotify доступно только в Windows"}
    hwnd = pp.find_window_by_process(SPOTIFY_EXE)
    opened = False
    if not hwnd:
        opened = pp.open_spotify_protocol()
        if not opened and ensure_open is not None:
            r = ensure_open("Spotify")
            opened = bool(isinstance(r, dict) and r.get("ok"))
        if not opened:
            return {"ok": False, "error": "Spotify не найден — установи или открой вручную"}
        hwnd = pp.wait_for_app_window(SPOTIFY_EXE, "", timeout=15.0)
        if not hwnd:
            return {"ok": False, "error": "Spotify не открылся вовремя — попробуй ещё раз", "opened": True}
        time.sleep(2.0)  # cold start: wait until the UI accepts shortcuts
    before = pp.window_title(hwnd).strip()
    if pp.focus_hwnd(hwnd):
        time.sleep(0.35)
        pp._key_combo([pp.VK_CONTROL], pp.VK_K)        # quick search
        time.sleep(0.6)
        if pp._paste_text(query, clear_field=True):
            time.sleep(1.3)                            # results load
            pp._tap(pp.VK_RETURN)                      # play top result
            now = _wait_playing(hwnd, before)
            if now:
                log.info("spotify: playing via quick_search query_len=%d", len(query))
                return {"ok": True, "via": "quick_search", "now_playing": now, "verified": True, "opened": opened}
            pp._tap(pp.VK_ESCAPE)                      # close the quick-search dialog if still open
    # Fallback: search URI page + Enter (best effort), then check the title again.
    try:
        os.startfile(search_uri(query))  # type: ignore[attr-defined]
    except OSError as e:
        return {"ok": False, "error": f"не удалось открыть поиск Spotify: {e}"[:200]}
    time.sleep(1.8)
    h2 = pp.find_window_by_process(SPOTIFY_EXE) or hwnd
    if pp.focus_hwnd(h2):
        time.sleep(0.3)
        pp._tap(pp.VK_RETURN)
    now = _wait_playing(h2, before, timeout=2.5)
    log.info("spotify: search_uri fallback verified=%s query_len=%d", bool(now), len(query))
    if now:
        return {"ok": True, "via": "search_uri", "now_playing": now, "verified": True, "opened": opened}
    return {"ok": True, "via": "search_uri", "verified": False, "opened": opened,
            "note": "Открыл поиск в Spotify, но не уверен, что трек заиграл — скажи пользователю нажать Play "
                    "на первом результате или уточнить название."}


def play_query(query: str, *, ensure_open=None, settings: dict | None = None) -> dict:
    q = clean_query(query)
    if not q or is_generic(q):
        return {"ok": False, "error": "не понял, какой трек включить", "hint": "передай название трека/исполнителя в query"}
    creds = api_credentials(settings)
    api_err = ""
    if creds:
        try:
            r = webapi_play(q, creds)
            if r.get("ok"):
                r.update(app="Spotify", query=q)
                return r
            api_err = str(r.get("error") or "")
            if r.get("track"):
                q = r["track"].replace(" - ", " ")  # exact «artist title» for the UI search
        except Exception as e:
            api_err = f"spotify api: {type(e).__name__}"
    r = ui_play(q, ensure_open=ensure_open)
    r.update(app="Spotify", query=q)
    if api_err:
        r["api_error"] = api_err[:120]
    return r
