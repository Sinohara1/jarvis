"""Refusal → tool override («safety net» for pc_control).

Small/cloud models sometimes answer a clear PC command with a soft refusal instead of a tool call:
«К сожалению, из-за технических ограничений я не могу нажать кнопку», «I can't click buttons»,
«не могу открывать личные чаты». At pc_control=full (and for media_control already at standard) the
assistant HAS the tools, so such an answer is a bug, not a limit. This module

* detects a refusal / «limitation» reply (RU + EN),
* detects that the user asked for an action (play, click/press, Telegram, open, type, window…),
* maps the request to the matching tool call deterministically (media_control, press_hotkey,
  app_search, telegram_open_chat, send_telegram, type_text, window_control, open_app…).

Brain uses it to suppress the refusal (it is never spoken) and run the tool itself; when nothing maps,
Brain retries the model once with a forcing hint.

Real hard limits stay: requests to wipe / format disks, mass-delete, or bypass UAC never activate the
override — the model's refusal there is correct and is left as is.
"""

from __future__ import annotations

import re

from .pc_power import pc_control_level, tool_allowed

# ─── refusal detection ───────────────────────────────────────────────────────

_REFUSAL_RE = re.compile(
    r"("
    # RU
    r"\bне\s+(?:могу|можу|умею|способ\w*|в\s+состоянии|получится|удастся|выйдет|имею\s+(?:возможност\w*|доступ\w*|прав\w*)|"
    r"располагаю|поддерживаю|позволено|разрешено)\b"
    r"|\bнет\s+(?:возможност\w*|доступа|прав\w*|функци\w*|такой\s+функци\w*)\b"
    r"|\bу\s+меня\s+нет\b"
    r"|\bнедоступн\w*\s+(?:для\s+меня|мне)\b"
    r"|техническ\w*\s+(?:ограничени\w*|невозможн\w*|причин\w*)"
    r"|\bограничени\w*\s+(?:системы|доступа|безопасности|модели|моих\s+возможностей)"
    r"|\bмои\s+(?:возможности|функции)\s+ограничен\w*"
    r"|\bя\s+(?:всего\s+лишь|лишь|только)\s+(?:ии|ассистент|языков\w*|текстов\w*|голосов\w*|программ\w*)"
    r"|\bкак\s+(?:ии|искусственный\s+интеллект|языковая\s+модель|ассистент)\b"
    r"|\bнажми\w*\s+(?:сам|сама|вручную)\b|\b(?:сам|сама|самостоятельно)\s+(?:нажми|кликни|открой|включи|введи)"
    r"|\bвручную\b"
    r"|\bк\s+сожалению\b"
    r"|\bневозможно\b"
    # EN
    r"|\b(?:i\s+)?(?:can't|cannot|can\s*not|am\s+unable|i'm\s+unable|am\s+not\s+able|i'm\s+not\s+able|"
    r"won't\s+be\s+able|don't\s+have\s+(?:the\s+)?(?:ability|access|permission|capabilit\w*))\b"
    r"|\bunable\s+to\b"
    r"|technical\s+limitation\w*|\blimitations?\b"
    r"|\bas\s+an?\s+(?:ai|language\s+model|assistant)\b"
    r"|\byou(?:'ll|\s+will)?\s+(?:need|have)\s+to\s+(?:click|press|open|do|type|hit|tap)"
    r"|\bmanually\b|\bunfortunately\b"
    r")",
    re.IGNORECASE)

# The model is not refusing when it is announcing an action («Нажимаю плей», «Opening Telegram»).
_ACTION_ANNOUNCE_RE = re.compile(
    r"^\s*(?:нажимаю|включаю|открываю|запускаю|ставлю|ввожу|печатаю|пишу|отправляю|закрываю|сворачиваю|"
    r"разворачиваю|переключаю|ищу|готово|сделано|пауза|продолжаю|press|pressing|opening|playing|starting|"
    r"typing|sending|closing|done)\b",
    re.IGNORECASE)


def is_refusal(reply: str) -> bool:
    """True when a reply reads like «I can't / technical limitation / do it manually»."""
    t = (reply or "").strip()
    if not t:
        return False
    if _ACTION_ANNOUNCE_RE.match(t) and not re.search(r"\b(?:не\s+могу|can't|cannot)\b", t, re.IGNORECASE):
        return False
    return bool(_REFUSAL_RE.search(t))


# ─── action request detection ────────────────────────────────────────────────

_LEAD_RE = re.compile(
    r"^\s*(?:(?:джарвис|jarvis|эй|hey|слушай|пожалуйста|please|ну|давай|так|окей|ok|okay)[,!.\s]+)+",
    re.IGNORECASE)
_POLITE_TAIL_RE = re.compile(r"[,\s]*(?:пожалуйста|please|плиз|быстро|сейчас|уже)\s*[.!?]*\s*$", re.IGNORECASE)
_CAN_YOU_RE = re.compile(
    r"^(?:а\s+)?(?:ты\s+)?(?:можешь|сможешь|мог\s+бы|could\s+you|can\s+you|would\s+you|will\s+you)\s+(?:ли\s+)?(?:мне\s+)?",
    re.IGNORECASE)

_ACTION_VERB_RE = re.compile(
    r"(?:^|[\s,])("
    r"нажм\w*|жми|нажать|кликн\w*|клацн\w*|тыкн\w*|ткни|щёлкн\w*|щелкн\w*|"
    r"включи\w*|вруби\w*|запусти\w*|открой|открыть|зайди|зайти|перейди|перейти|поставь|поставить|продолжи\w*|"
    r"возобнови\w*|воспроизведи|сыграй|напиши|написать|отправь|отправить|введи|ввести|напечатай|набери|впиши|"
    r"закрой|закрыть|сверни|свернуть|разверни|развернуть|переключи\w*|сфокусируй|найди|найти|пауз\w*|плей|плэй|"
    r"следующ\w*|предыдущ\w*|скипни\w*|пропусти|"
    r"press|click|tap|hit|play|resume|pause|open|launch|start|type|write|send|close|minimize|maximize|switch|"
    r"focus|go\s+to|find|skip"
    r")\b",
    re.IGNORECASE)

# Requests the assistant must keep refusing (hard limits) — never override these.
HARD_LIMIT_RE = re.compile(
    r"(формат\w*|format\b|diskpart|стер\w*\s+(?:вс[её]|диск)|сотри\s+(?:вс[её]|диск)|очисти\s+(?:весь\s+)?диск|"
    r"удали\w*\s+(?:вс[её]|все\s+файлы|папку\s+windows|system32)|rm\s+-rf|del\s+/[sq]|rd\s+/s|wipe|"
    r"mass[-\s]?delete|delete\s+(?:all|everything)|"
    r"обойд\w*\s+uac|обход\w*\s+uac|без\s+(?:окна\s+)?uac|отключи\w*\s+uac|bypass\s+uac|disable\s+uac|"
    r"silent\w*\s+(?:admin|elevat)|тихо\s+от\s+админ)",
    re.IGNORECASE)


def clean_request(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    for _ in range(2):
        t = _LEAD_RE.sub("", t)
        t = _POLITE_TAIL_RE.sub("", t)
        t = _CAN_YOU_RE.sub("", t)
    return t.strip(" ,.!?…")


def is_action_request(text: str) -> bool:
    t = clean_request(text)
    if not t or len(t) > 300:
        return False
    if HARD_LIMIT_RE.search(t):
        return False
    return bool(_ACTION_VERB_RE.search(" " + t))


def guard_active(settings: dict | None, text: str) -> bool:
    """Should Brain watch this turn for a soft refusal?  At «full» — every action request; below — only
    when the request maps to a tool the level allows (e.g. «нажми плей» → media_control at standard)."""
    if not is_action_request(text):
        return False
    if pc_control_level(settings) == "full":
        return True
    return override_call(text, settings) is not None


# ─── request → tool call ─────────────────────────────────────────────────────

_SPOTIFY_WORD_RE = re.compile(r"(spotify|спотиф\w*|спотик\w*|споти\b)", re.IGNORECASE)
_PRESS = r"(?:нажм\w*|жми|нажать|кликн\w*|клацн\w*|тыкн\w*|ткни|щ[её]лкн\w*|press|click|hit|tap|push)"

_PLAY_BTN_RE = re.compile(
    rf"^{_PRESS}\s+(?:(?:на|по|on)\s+)?(?:the\s+)?(?:кнопк\w*\s+|button\s+)?"
    r"[«\"']?(?:плей|плэй|play|воспроизвед\w*|старт|пуск|resume|продолж\w*)[»\"']?"
    r"(?:\s+(?:button|кнопк\w*))?(?:\s+(?:в|во|на|in|on)\s+(?P<app>.+))?$",
    re.IGNORECASE)
_RESUME_RE = re.compile(
    r"^(?:включи|вруби|продолжи|запусти|возобнови|воспроизведи|верни|сними\s+с\s+паузы|resume|unpause|continue|play|start)"
    r"(?:\s+(?:воспроизведение|видео|видос\w*|ролик|музыку|музон|музычку|трек|песню|playback|the\s+video|the\s+music|music))?"
    r"(?:\s+(?:в|во|на|in|on)\s+(?P<app>.+))?$",
    re.IGNORECASE)
_BARE_PLAY_RE = re.compile(r"^(?:плей|плэй|play|сними\s+с\s+паузы|unpause)$", re.IGNORECASE)
_PAUSE_RE = re.compile(
    rf"^(?:(?:{_PRESS}|поставь|постав\w*|сделай)\s+(?:(?:на|on)\s+)?(?:кнопк\w*\s+)?паузу?|pause|пауза|"
    r"останови\s+(?:видео|музыку|трек|песню|воспроизведение)|стоп\s+(?:музык\w*|видео)|(?:press\s+|hit\s+)?pause(?:\s+button)?)"
    r"(?:\s+(?:в|во|на|in|on)\s+(?P<app>.+))?$",
    re.IGNORECASE)
_NEXT_RE = re.compile(
    r"^(?:(?:включи|поставь|давай|переключи\s+на)\s+)?(?:следующ\w+(?:\s+(?:трек|песн\w*|видео))?|переключи\s+(?:трек|песню)|"
    r"скипни\w*|пропусти\s+(?:трек|песню)|next(?:\s+track|\s+song)?|skip(?:\s+(?:track|song|this))?)"
    r"(?:\s+(?:в|во|на|in|on)\s+(?P<app>.+))?$",
    re.IGNORECASE)
_PREV_RE = re.compile(
    r"^(?:(?:включи|поставь|давай|верни)\s+)?(?:предыдущ\w+(?:\s+(?:трек|песн\w*|видео))?|previous(?:\s+track|\s+song)?|"
    r"прошл\w+\s+(?:трек|песн\w*))(?:\s+(?:в|во|на|in|on)\s+(?P<app>.+))?$",
    re.IGNORECASE)

_KEY_WORDS = {
    "пробел": "space", "пробельчик": "space", "спейс": "space", "space": "space", "spacebar": "space",
    "энтер": "enter", "ентер": "enter", "ввод": "enter", "enter": "enter", "return": "enter",
    "эскейп": "esc", "искейп": "esc", "эскейт": "esc", "esc": "esc", "escape": "esc", "эск": "esc",
    "таб": "tab", "tab": "tab", "бэкспейс": "backspace", "backspace": "backspace",
    "делит": "delete", "delete": "delete", "del": "delete",
    "вверх": "up", "вниз": "down", "влево": "left", "вправо": "right",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "хоум": "home", "энд": "end",
    "контрол": "ctrl", "контрл": "ctrl", "ктрл": "ctrl", "ctrl": "ctrl", "control": "ctrl",
    "альт": "alt", "alt": "alt", "шифт": "shift", "shift": "shift", "вин": "win", "win": "win", "виндовс": "win",
}
_HOTKEY_RE = re.compile(
    rf"^{_PRESS}\s+(?:(?:на|по|on)\s+)?(?:клавиш\w*\s+|кнопк\w*\s+|key\s+|the\s+)?(?P<k>.+?)"
    r"(?:\s+(?:в|во|на|in|on)\s+(?P<win>[^+]+?))?$",
    re.IGNORECASE)

_WINDOW_RE = re.compile(
    r"^(?P<v>закрой|закрыть|сверни|свернуть|разверни\s+на\s+весь\s+экран|разверни|развернуть|переключись\s+(?:на|в)|"
    r"переключи\s+на|сфокусируй(?:ся\s+на)?|покажи\s+окно|close|minimi[sz]e|maximi[sz]e|switch\s+to|focus(?:\s+on)?)"
    r"(?:\s+(?P<n>.+))?$",
    re.IGNORECASE)
_WINDOW_ACTIONS = {"закр": "close", "close": "close", "свер": "minimize", "mini": "minimize",
                   "разв": "maximize", "maxi": "maximize", "пере": "focus", "сфок": "focus", "пока": "focus",
                   "swit": "focus", "focu": "focus"}
_ACTIVE_WORDS = re.compile(r"^(?:это|его|её|ее|текущее|активное|окно|это\s+окно|текущее\s+окно|активное\s+окно|"
                           r"it|this|this\s+window|the\s+window|current\s+window|window)$", re.IGNORECASE)
_STRIP_WIN_WORDS = re.compile(r"^(?:окно|приложение|программу|прогу|the\s+window|window|app)\s+", re.IGNORECASE)

_TYPE_RE = re.compile(
    r"^(?:введи|ввести|напечатай|набери|впиши|вставь\s+текст|type|enter\s+text)\s+(?:текст\s+|text\s+)?"
    r"(?:(?:в|во|in|into)\s+(?P<win>[^:,]+?)\s*[:,]\s*)?(?P<t>.+)$",
    re.IGNORECASE | re.DOTALL)
_WRITE_IN_RE = re.compile(
    r"^(?:напиши|write|type)\s+(?:в|во|in|into)\s+(?P<win>блокнот\w*|notepad|ворд\w*|word|окн\w+|пол\w+|пол[еяю]|пои?ск\w*|"
    r"строк\w+\s+поиска|адресн\w+\s+строк\w+|чат\w*|search\s+box|search|field)\s*[:,]?\s*(?P<t>.+)$",
    re.IGNORECASE | re.DOTALL)
_ENTER_TAIL_RE = re.compile(r"[,\s]+(?:и\s+)?(?:нажми\s+(?:энтер|enter|ввод)|отправь|and\s+(?:press\s+)?enter|and\s+send)\s*$",
                            re.IGNORECASE)

_TG_WORD = r"(?:телеграм\w*|телег[еуа]|телеге|тг|telegram|tg)"
_TG_SEND_RE1 = re.compile(
    rf"^(?:напиши|отправь|скинь|write|send|message)\s+(?:(?:в|во|на|on|in)\s+)?{_TG_WORD}\s+(?P<who>[^,:]+?)\s*[,:]\s*(?P<msg>.+)$",
    re.IGNORECASE | re.DOTALL)
_TG_SEND_RE2 = re.compile(
    rf"^(?:напиши|отправь|скинь|write|send|message)\s+(?P<who>[^,:]+?)\s+(?:(?:в|во|на|on|in)\s+)?{_TG_WORD}\s*[,:]?\s*(?P<msg>.+)$",
    re.IGNORECASE | re.DOTALL)

_IN_APP_RE = re.compile(
    r"^(?:открой|открыть|найди|найти|зайди\s+в|перейди\s+в|включи|поставь|запусти|open|find|search(?:\s+for)?|go\s+to|play)\s+"
    r"(?P<q>.+?)\s+(?:в|во|на|in|on)\s+(?P<app>[\w .\-]+?)$",
    re.IGNORECASE)
_YT_RE = re.compile(r"^(?:ютуб\w*|youtube|ютьюб\w*)$", re.IGNORECASE)
_OPEN_RE = re.compile(r"^(?:открой|открыть|запусти|запустить|включи|open|launch|start|run)\s+(?P<n>.+)$", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^(?:https?://)?[\w\-]+(?:\.[\w\-]+)+(?:/\S*)?$", re.IGNORECASE)
_NOT_APP_RE = re.compile(r"^(?:музыку|музон|видео|трек|песню|свет|воспроизведение|фокус\w*|таймер\w*|будильник\w*|"
                         r"напоминани\w*|живой\s+режим|режим\b|focus|timer|alarm|reminder|music|video|live\s+mode)",
                         re.IGNORECASE)


def _allowed(name: str, settings: dict | None) -> bool:
    return tool_allowed(name, settings=settings)


def _media(action: str, app: str | None, settings: dict | None) -> tuple[str, dict] | None:
    if not _allowed("media_control", settings):
        return None
    args: dict = {"action": action}
    if app and _SPOTIFY_WORD_RE.search(app):
        args["app"] = "spotify"
    return "media_control", args


def _hotkey_spec(raw: str) -> str:
    """«пробел» → space, «контрол плюс с» → ctrl+c, «alt f4» → alt+f4. '' when not a key."""
    k = (raw or "").strip().lower().strip("«»\"'.!")
    k = re.sub(r"^(?:стрелк\w*|arrow)\s+", "", k)
    k = re.sub(r"\s*(?:плюс|plus|\+)\s*", "+", k)
    toks = [t for t in re.split(r"[+\s]+", k) if t]
    if not toks or len(toks) > 4:
        return ""
    out = []
    for t in toks:
        t = _KEY_WORDS.get(t, t)
        if re.fullmatch(r"ф\d{1,2}", t):  # «ф4» — F4 typed/heard in Cyrillic
            t = "f" + t[1:]
        if re.fullmatch(r"(?:f\d{1,2}|[a-z0-9]|space|enter|esc|tab|backspace|delete|up|down|left|right|home|end|"
                        r"ctrl|alt|shift|win|pageup|pagedown)", t) is None:
            return ""
        out.append(t)
    spec = "+".join(out)
    try:
        from .pc_power import parse_hotkey
        return spec if parse_hotkey(spec) else ""
    except Exception:
        return ""


_WIN_ALIASES = {"блокнот": "notepad", "ворд": "winword", "эксель": "excel", "пэйнт": "mspaint", "паинт": "mspaint"}


def _prep_to_nom(word: str) -> str:
    """«в блокноте» → «блокнот», «в хроме» → «хром» (window lookup wants the plain name)."""
    w = (word or "").strip(" ,.«»\"'")
    low = w.lower()
    if " " not in w and len(w) >= 5 and re.fullmatch(r"[а-яё]+", low) and low.endswith("е") \
            and not low.endswith(("ае", "ее")):
        w = w[:-1]
    return _WIN_ALIASES.get(w.lower(), w)


def _window_name(raw: str) -> str:
    n = (raw or "").strip(" ,.«»\"'")
    n = _STRIP_WIN_WORDS.sub("", n)
    if not n or _ACTIVE_WORDS.match(n):
        return ""
    return n


def override_call(text: str, settings: dict | None, *, telegram_context: bool = False) -> tuple[str, dict] | None:
    """Map an action request to the tool call the model should have made, or None.
    Only tools allowed at the current pc_control level are returned."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw or len(raw) > 300 or HARD_LIMIT_RE.search(raw):
        return None
    t = clean_request(raw)
    if not t:
        return None

    # 1) a specific track on Spotify
    try:
        from .spotify import spotify_play_intent
        q = spotify_play_intent(t)
    except Exception:
        q = None
    if q and _allowed("media_control", settings):
        return "media_control", {"action": "play", "app": "spotify", "query": q}

    # 2) Telegram: send / Saved Messages / open chat
    try:
        from .stt_commands import parse_command
        pc = parse_command(t)
    except Exception:
        pc = None
    if pc and pc.get("kind") == "telegram" and pc.get("message") and _allowed("send_telegram", settings):
        return "send_telegram", {"contact": pc["contact"], "message": pc["message"]}
    for rx in (_TG_SEND_RE1, _TG_SEND_RE2):
        m = rx.match(t)
        if m and _allowed("send_telegram", settings):
            who = m.group("who").strip(" ,.«»\"'")
            who = re.sub(r"^(?:в|во|на)\s+", "", who)
            msg = m.group("msg").strip()
            if who and msg and not re.fullmatch(_TG_WORD, who, re.IGNORECASE):
                return "send_telegram", {"contact": who, "message": msg}
    from .pc_power import telegram_saved_intent, telegram_chat_intent
    if _allowed("telegram_open_chat", settings):
        if telegram_saved_intent(t):
            return "telegram_open_chat", {"chat": "Saved Messages"}
        who = telegram_chat_intent(t, telegram_context=telegram_context)
        if who:
            return "telegram_open_chat", {"chat": who}

    # 3) play / pause / next / previous («нажми плей», «продолжи видео», «пауза»)
    for rx, act in ((_PLAY_BTN_RE, "play"), (_PAUSE_RE, "pause"), (_NEXT_RE, "next"), (_PREV_RE, "previous"),
                    (_BARE_PLAY_RE, "play"), (_RESUME_RE, "play")):
        m = rx.match(t)
        if m:
            app = m.groupdict().get("app") or ""
            r = _media(act, app or t, settings)
            if r:
                return r
            break

    # 4) keys: «нажми пробел», «нажми ctrl+c», «закрой вкладку», «сверни все окна»
    if re.fullmatch(r"(?:закрой|close)\s+(?:эту\s+|текущую\s+|this\s+|the\s+)?(?:вкладку|tab)", t, re.IGNORECASE):
        if _allowed("press_hotkey", settings):
            return "press_hotkey", {"keys": "ctrl+w"}
    if re.fullmatch(r"(?:открой|open)\s+(?:новую\s+|a\s+)?(?:вкладку|new\s+tab|tab)", t, re.IGNORECASE):
        if _allowed("press_hotkey", settings):
            return "press_hotkey", {"keys": "ctrl+t"}
    if re.fullmatch(r"(?:сверни|minimi[sz]e)\s+(?:вс[её]|все\s+окна|all(?:\s+windows)?)|покажи\s+рабочий\s+стол|show\s+desktop",
                    t, re.IGNORECASE):
        if _allowed("press_hotkey", settings):
            return "press_hotkey", {"keys": "win+d"}
    m = _HOTKEY_RE.match(t)
    if m and _allowed("press_hotkey", settings):
        spec = _hotkey_spec(m.group("k"))
        if not spec and m.group("win"):
            spec = _hotkey_spec(m.group("k") + " " + m.group("win"))
            win = ""
        else:
            win = _prep_to_nom(m.group("win") or "")
        if spec:
            args = {"keys": spec}
            if win:
                args["window"] = win
            return "press_hotkey", args

    # 5) typing
    for rx in (_TYPE_RE, _WRITE_IN_RE):
        m = rx.match(t)
        if m and _allowed("type_text", settings):
            body = m.group("t").strip()
            enter = bool(_ENTER_TAIL_RE.search(body))
            body = _ENTER_TAIL_RE.sub("", body).strip().strip("«»\"")
            if body:
                args = {"text": body}
                win = (m.groupdict().get("win") or "").strip()
                if win and not re.match(r"(?:окн|пол|пои|стро|адрес|search|field|чат)", win, re.IGNORECASE):
                    args["window"] = _prep_to_nom(win)
                if enter:
                    args["press_enter"] = True
                return "type_text", args

    # 6) windows: close / minimize / maximize / focus (soft close only)
    m = _WINDOW_RE.match(t)
    if m and _allowed("window_control", settings):
        v = m.group("v").lower()
        act = _WINDOW_ACTIONS.get(v[:4])
        if act:
            return "window_control", {"action": act, "name": _window_name(m.group("n") or "")}

    # 7) something inside an app: «открой general в дискорде», «найди main.py в vs code»
    m = _IN_APP_RE.match(t)
    if m:
        app = m.group("app").strip()
        q = m.group("q").strip(" «»\"'")
        if _YT_RE.match(app):
            return "web_search", {"query": q, "engine": "youtube"}
        from .pc_power import resolve_app_preset
        if q and _allowed("app_search", settings) and not _SPOTIFY_WORD_RE.search(app) \
                and not _NOT_APP_RE.match(q) and resolve_app_preset(app):
            return "app_search", {"app": app, "query": q}

    # 8) plain «открой X»
    m = _OPEN_RE.match(t)
    if m:
        n = m.group("n").strip(" «»\"'")
        n = re.sub(r"^(?:приложение|программу|прогу|сайт|app|the\s+app|website|site)\s+", "", n, flags=re.IGNORECASE)
        # «открой X в Y» that did not map above (e.g. a chat at «standard») is not a plain app launch
        if n and not _NOT_APP_RE.match(n) and not re.search(r"\s(?:в|во|на|in|on)\s", n, re.IGNORECASE):
            if _DOMAIN_RE.match(n):
                return "open_url", {"url": n}
            if _YT_RE.match(n):
                return "open_url", {"url": "youtube.com"}
            return "open_app", {"name": n}
    return None


# ─── short confirmations (spoken instead of the suppressed refusal) ──────────

_DONE_RU = {
    "media_control": {"play": "Включил.", "pause": "Пауза.", "play_pause": "Готово.", "next": "Следующий.",
                      "previous": "Предыдущий."},
    "press_hotkey": "Нажал.", "type_text": "Ввёл.", "window_control": "Готово.", "app_search": "Нашёл.",
    "telegram_open_chat": "Открыл чат.", "send_telegram": "Отправил.", "open_app": "Открываю.",
    "open_url": "Открываю.", "web_search": "Ищу.",
}
_DONE_EN = {
    "media_control": {"play": "Playing.", "pause": "Paused.", "play_pause": "Done.", "next": "Next one.",
                      "previous": "Previous one."},
    "press_hotkey": "Pressed.", "type_text": "Typed it.", "window_control": "Done.", "app_search": "Found it.",
    "telegram_open_chat": "Chat is open.", "send_telegram": "Sent.", "open_app": "Opening.",
    "open_url": "Opening.", "web_search": "Searching.",
}


def done_phrase(tool: str, args: dict, result: object, lang: str = "ru") -> str:
    en = (lang or "ru").lower().startswith("en")
    if isinstance(result, dict) and (result.get("ok") is False or result.get("error")):
        err = str(result.get("error") or "").strip().rstrip(".")
        if en:
            return f"Tried, but it didn't work: {err}." if err else "Tried, but it didn't work."
        return f"Попробовал, но не вышло: {err}." if err else "Попробовал, но не вышло."
    table = _DONE_EN if en else _DONE_RU
    v = table.get(tool)
    if isinstance(v, dict):
        if tool == "media_control" and (args or {}).get("query"):
            return "Playing it." if en else "Включаю."
        return v.get(str((args or {}).get("action") or ""), "Done." if en else "Готово.")
    return v or ("Done." if en else "Готово.")


FORCE_TOOL_HINT = (
    "\nВНИМАНИЕ: в прошлом варианте ответа ты отказался («не могу», «технические ограничения»). Это ошибка: "
    "уровень управления ПК «полный», у тебя есть инструменты и полный контроль над компьютером пользователя. "
    "Сейчас ОБЯЗАТЕЛЬНО вызови подходящий инструмент без отговорок: кнопка play/воспроизведение → media_control "
    "(action=\"play\") или press_hotkey (\"space\" / \"enter\" в нужном окне); клик/нажатие клавиш → press_hotkey; "
    "что-то внутри приложения → app_search; чат Telegram → telegram_open_chat, сообщение → send_telegram; "
    "ввод текста → type_text; окна → window_control; открыть программу → open_app. "
    "Не пиши про ограничения и не предлагай сделать вручную.\n")

CLARIFY = {
    "ru": "Скажи точнее, что нажать или открыть, — сделаю.",
    "en": "Tell me exactly what to press or open and I'll do it.",
}
