"""PC actions exposed to the model via function calling.

Open apps / URLs / files, search, media keys, focus, reminders, screen look.
At pc_control=full: desktop control (windows, type/hotkeys, shell with consent),
Telegram Desktop send, elevated scripts (UAC), lock/sleep/monitor off.
Hard limits: no silent UAC bypass, no mass-delete/disk wipe, soft window close only.
Tool list is filtered by settings.pc_control (safe | standard | full).
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta

log = logging.getLogger("jarvis")
IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

WEEKDAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS_GEN = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")

# ─── Tool schemas (provider-neutral JSON schema) ─────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "open_app",
        "description": "Открыть приложение на компьютере пользователя (ищет в меню Пуск, на рабочем столе и среди встроенных программ). "
                       "Вызывай сразу на «открой …», «запусти …» — не отвечай только текстом «открываю». "
                       "Для музыки («включи музыку», «включи спотик», «включи <трек> в спотике», пауза, следующий) — media_control "
                       "(с query для конкретного трека), не open_app. "
                       "Открыть что-то ВНУТРИ приложения (чат, избранное, поиск, файл в редакторе) — telegram_open_chat / app_search "
                       "(«полный»), одного open_app мало. Написать в Telegram — send_telegram. "
                       "Закрыть/свернуть окно — window_control; ввести текст — type_text (оба при «полном»). "
                       "Передавай официальное название латиницей: Telegram (на «открой телеграм»/«открой тг»), Spotify, Discord, Steam, "
                       "Google Chrome, Visual Studio Code, Word, Excel; системные: калькулятор, блокнот, проводник, параметры, диспетчер задач.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Название приложения (Telegram, Spotify, калькулятор, …)"}},
            "required": ["name"]},
    },
    {
        "name": "open_url",
        "description": "Открыть сайт или ссылку в браузере по умолчанию.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Адрес, например youtube.com или https://..."}},
            "required": ["url"]},
    },
    {
        "name": "web_search",
        "description": "Открыть поиск в браузере (Google по умолчанию, или YouTube / Википедия).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "engine": {"type": "string", "enum": ["google", "youtube", "wikipedia", "yandex"]}},
            "required": ["query"]},
    },
    {
        "name": "find_files",
        "description": "Найти файлы или папки по части имени в папках пользователя (Рабочий стол, Документы, Загрузки, Изображения, Видео, Музыка, OneDrive). "
                       "Возвращает до 10 путей, новые первыми.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Часть имени файла, например «физика» или «.pdf» или «отчёт 2026»"},
            "open_first": {"type": "boolean", "description": "Сразу открыть самый подходящий найденный файл"}},
            "required": ["query"]},
    },
    {
        "name": "open_path",
        "description": "Открыть файл или папку по полному пути (обычно путь из find_files).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "start_focus",
        "description": "Начать фокус-сессию: пользователь сказал, над чем работает и сколько времени. "
                       "Ассистент будет следить за экраном и возвращать к задаче.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "Короткое описание задачи, например «домашка по физике»"},
            "minutes": {"type": "integer", "description": "Длительность одного блока фокуса в минутах (по умолчанию из настроек)"},
            "rounds": {"type": "integer", "description": "Сколько блоков фокуса с перерывами между ними (по умолчанию 1)"}},
            "required": ["task"]},
    },
    {
        "name": "stop_focus",
        "description": "Завершить текущую фокус-сессию.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "pause_focus",
        "description": "Поставить фокус-сессию на паузу или продолжить её.",
        "parameters": {"type": "object", "properties": {
            "resume": {"type": "boolean", "description": "true — продолжить, false — пауза"}},
            "required": ["resume"]},
    },
    {
        "name": "focus_status",
        "description": "Узнать состояние фокус-сессии и статистику фокуса за сегодня.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "set_reminder",
        "description": "Поставить таймер или напоминание. Укажи либо minutes (через сколько минут), либо at_time (ЧЧ:ММ сегодня/завтра).",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "О чём напомнить"},
            "minutes": {"type": "number", "description": "Через сколько минут"},
            "at_time": {"type": "string", "description": "Время в формате ЧЧ:ММ"}},
            "required": ["text"]},
    },
    {
        "name": "list_reminders",
        "description": "Показать активные напоминания и таймеры.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminders",
        "description": "Отменить все активные напоминания и таймеры.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_time",
        "description": "Текущие дата, день недели и время.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "set_live_mode",
        "description": "Включить или выключить «живой режим»: ассистент сам периодически смотрит на экран и, если уместно, "
                       "подсказывает, спрашивает или хвалит. Включить: «следи и подсказывай», «включи живой режим», «присматривай за мной». "
                       "Выключить: «выключи живой режим», «тихо», «хватит», «помолчи», «не мешай» (про подсказки).",
        "parameters": {"type": "object", "properties": {
            "enabled": {"type": "boolean", "description": "true — включить, false — выключить"}},
            "required": ["enabled"]},
    },

    {
        "name": "media_control",
        "description": "Музыка и медиа: play / pause / next / previous, а также НАЙТИ И ВКЛЮЧИТЬ конкретный трек в Spotify. "
                       "«Включи Can't Fault Das в спотике», «play Numb by Linkin Park on Spotify», «поставь Кино Группа крови» → "
                       "action=\"play\", app=\"spotify\", query=\"<название трека и/или исполнителя>\" — Jarvis сам откроет Spotify, "
                       "найдёт и запустит первый результат. Не отказывайся «не могу выбрать песню» — просто вызови с query. "
                       "query — только название/исполнитель (без «включи», «в спотике»), английские названия пиши латиницей как в оригинале. "
                       "«Включи музыку» / «включи спотик» без названия, «пауза», «следующий трек», «предыдущий» — без query "
                       "(медиаклавиши; app=\"spotify\" откроет Spotify). Не используй open_app для музыки. "
                       "Кнопка play/«нажми плей»/«продолжи видео»/«сними с паузы» в любом плеере (YouTube, VLC, браузер, "
                       "Spotify) → action=\"play\" (или press_hotkey \"space\"/\"enter\" в окне плеера). "
                       "Это реальное нажатие медиаклавиши — не рассказывай про ограничения, просто вызови.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["play", "pause", "play_pause", "next", "previous"],
                       "description": "play — включить (с query — найти и включить трек); pause/play_pause — пауза; next/previous — трек"},
            "app": {"type": "string", "description": "spotify (по умолчанию для query) или пусто — системные медиаклавиши"},
            "query": {"type": "string", "description": "Что найти и включить: название трека, исполнитель, альбом или плейлист, "
                                                       "например «Can't Fault Das» или «Linkin Park Numb»"}},
            "required": ["action"]},
    },
    {
        "name": "adjust_volume",
        "description": "Системная громкость: громче / тише / вкл-выкл звук (медиаклавиши Volume).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["up", "down", "mute"]}},
            "required": ["action"]},
    },
    {
        "name": "send_telegram",
        "description": "Написать и отправить сообщение в Telegram Desktop от имени пользователя (не бот). "
                       "«напиши в телеграм …», «отправь в тг Маме: …», «запиши себе в избранное …» (contact=\"Избранное\"), "
                       "«Write Mom on Telegram, I'm home» (contact=\"Mom\", message=\"I'm home\"). "
                       "Нужен уровень управления ПК «полный». Сам открывает Telegram и нужный чат — не вызывай перед ним "
                       "open_app/type_text/press_hotkey. "
                       "Best-effort: Telegram Desktop должен быть установлен и залогинен; поиск чата может промахнуться — "
                       "предупреди пользователя проверить. contact — имя чата как в списке чатов, в именительном падеже "
                       "(«Мама», не «Маме»), @username или «Избранное»; message — текст.",
        "parameters": {"type": "object", "properties": {
            "contact": {"type": "string", "description": "Имя чата, контакта или @username"},
            "message": {"type": "string", "description": "Текст сообщения"}},
            "required": ["contact", "message"]},
    },
    {
        "name": "telegram_open_chat",
        "description": "Открыть конкретный чат в Telegram Desktop (без отправки): «открой избранное в телеграме», "
                       "«open Saved Messages on Telegram», «открой чат с Мамой», «перейди в тг к @durov». "
                       "Также после «открой телеграм» на «зайди в чат с …» / «найди ник …» — этот инструмент, не type_text/press_hotkey. "
                       "Saved Messages/Избранное → Ctrl+0; @username → tg://resolve; иначе Esc → Ctrl+F (поиск по чатам) → ввод → Enter "
                       "на первом результате (Ctrl+K в Telegram — это вставка ссылки, не поиск). "
                       "Нужен «полный». Ты МОЖЕШЬ открывать чаты пользователя — это его собственный Telegram, не отказывайся "
                       "и не говори про приватность или ограничения.",
        "parameters": {"type": "object", "properties": {
            "chat": {"type": "string", "description": "«Избранное» / «Saved Messages», @username (ник) или имя чата как в "
                                                      "списке чатов, в именительном падеже («Мама», не «Мамой»)"}},
            "required": ["chat"]},
    },
    {
        "name": "app_search",
        "description": "Универсально: открыть/сфокусировать любое приложение и найти/открыть что-то ВНУТРИ него — "
                       "фокус окна → горячая клавиша поиска → вставка запроса → Enter → (опц.) доп. клавиши. Нужен «полный». "
                       "Примеры: найти сервер/канал в Discord, файл в VS Code (ctrl+p), чат в WhatsApp, сайт в адресной строке "
                       "Chrome/Edge, папку в Проводнике, страницу в Notion, игру в Steam. Знает хоткеи Spotify, Telegram, Discord, "
                       "браузеров, VS Code, Проводника, Slack, Notion, Obsidian, WhatsApp, Steam; для других передай search_keys. "
                       "Для музыки лучше media_control(query), для чатов Telegram — telegram_open_chat. "
                       "При «полном» не отказывайся «не могу зайти в приложение» — используй этот инструмент "
                       "(или window_control + press_hotkey + type_text по шагам).",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string", "description": "Приложение или часть заголовка окна (Discord, Chrome, VS Code, Steam…); "
                                                     "пусто — активное окно"},
            "query": {"type": "string", "description": "Что ввести в поиск"},
            "search_keys": {"type": "string", "description": "Опционально: клавиши открытия поиска, шаги через «;» "
                                                             "(ctrl+f, ctrl+k, esc; ctrl+f). По умолчанию — из пресета приложения "
                                                             "или ctrl+f. Для Telegram не передавай (там свой сценарий)"},
            "then_keys": {"type": "string", "description": "Опционально: клавиши после Enter для навигации по результатам, "
                                                           "например «down; enter» или «tab*3; enter»"},
            "press_enter": {"type": "boolean", "description": "Нажать Enter после ввода (по умолчанию true)"}},
            "required": ["query"]},
    },
    {
        "name": "run_elevated",
        "description": "Запустить скрипт (.ps1 / .bat / .cmd / .py) с запросом прав администратора (окно UAC). "
                       "Только при уровне управления ПК «полный». Путь — существующий файл в папке пользователя (или OneDrive). "
                       "Никакого обхода UAC: пользователь должен подтвердить сам.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Полный путь к скрипту"},
            "args": {"type": "string", "description": "Опциональные аргументы командной строки"}},
            "required": ["path"]},
    },
    {
        "name": "window_control",
        "description": "Управление окном Windows: close / focus / minimize / maximize / restore. "
                       "Нужен уровень «полный». name — часть заголовка окна (пусто = активное). "
                       "close мягко шлёт WM_CLOSE (приложение может спросить про сохранение); принудительный kill процесса нет — "
                       "мягкое закрытие разрешено, делай без лишних вопросов. "
                       "«закрой хром», «сверни телеграм», «разверни спотифай», «переключись на код». "
                       "Окна ищутся и по процессу (Spotify, Telegram и др. — даже если заголовок «Исполнитель - Трек»).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["close", "focus", "minimize", "maximize", "restore"]},
            "name": {"type": "string", "description": "Часть заголовка окна; пусто — активное окно"}},
            "required": ["action"]},
    },
    {
        "name": "type_text",
        "description": "Ввести (вставить) текст в активное или указанное окно через буфер обмена. "
                       "Нужен «полный». «напиши в блокноте …», «введи пароль …» (не логируется). "
                       "Для отправки в Telegram лучше send_telegram. press_enter — нажать Enter после вставки. "
                       "Не отвечай «не могу вводить текст» — вызывай.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Текст для вставки"},
            "window": {"type": "string", "description": "Часть заголовка окна; пусто — активное"},
            "press_enter": {"type": "boolean", "description": "Нажать Enter после вставки"}},
            "required": ["text"]},
    },
    {
        "name": "press_hotkey",
        "description": "Нажать комбинацию или последовательность клавиш в активном или указанном окне. Нужен «полный». "
                       "Примеры: ctrl+c, ctrl+v, ctrl+z, alt+f4, alt+tab, win+d, win+l, ctrl+shift+esc, f5; "
                       "последовательность шагами через «;» и повтор *N: «esc; ctrl+f», «down*3; enter», «tab; space». "
                       "Вместе с type_text и window_control позволяет управлять любым приложением по шагам. "
                       "Нажать кнопку на экране: play/воспроизведение → \"space\" (или \"enter\", \"k\" в YouTube) в окне плеера "
                       "либо media_control; кнопку в фокусе → \"enter\"/\"space\"; перейти к кнопке → \"tab*N; enter\". "
                       "Это настоящие нажатия клавиш: не говори «не могу нажать/кликнуть» — вызывай. "
                       "Ctrl+Alt+Del эмулировать нельзя.",
        "parameters": {"type": "object", "properties": {
            "keys": {"type": "string", "description": "Комбинация (ctrl+s, alt+f4) или шаги через «;» (esc; ctrl+f; down*2; enter)"},
            "window": {"type": "string", "description": "Часть заголовка окна; пусто — активное"}},
            "required": ["keys"]},
    },
    {
        "name": "run_command",
        "description": "Выполнить команду в cmd или PowerShell от имени пользователя (без повышения прав). "
                       "Нужен «полный». Опасные команды (удаление, taskkill, shutdown, reg delete…) требуют "
                       "сначала спросить пользователя и вызвать снова с confirm=true. "
                       "Массовое удаление / format / diskpart — всегда отказ. Для admin-скриптов — run_elevated.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Текст команды"},
            "shell": {"type": "string", "enum": ["cmd", "powershell"], "description": "Оболочка (по умолчанию cmd)"},
            "confirm": {"type": "boolean", "description": "true только после явного согласия на опасную команду"},
            "timeout": {"type": "number", "description": "Таймаут секунд (1–120, по умолчанию 30)"}},
            "required": ["command"]},
    },
    {
        "name": "get_active_window",
        "description": "Узнать заголовок и процесс активного окна; опционально список видимых окон. "
                       "Нужен «полный». Полезно перед window_control / type_text.",
        "parameters": {"type": "object", "properties": {
            "list_windows": {"type": "boolean", "description": "true — добавить до 25 видимых окон"}},
            "required": []},
    },
    {
        "name": "lock_workstation",
        "description": "Заблокировать компьютер (как Win+L). Нужен уровень «полный».",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "system_power",
        "description": "Сон ПК (sleep) или погасить монитор (monitor_off). Нужен «полный». "
                       "Выключение/перезагрузка этим инструментом не делаются. Чётко предупреди пользователя перед сном.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["sleep", "monitor_off"]}},
            "required": ["action"]},
    },
    {
        "name": "look_at_screen",
        "description": "Посмотреть на экран пользователя (скриншот активного монитора) и ответить на вопрос о том, что там видно.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "Что нужно понять по экрану"}},
            "required": ["question"]},
    },
]


# ─── PC control gating (re-export from pc_power) ──────────────────────────────

from .pc_power import (  # noqa: E402
    PC_CONTROL_RANKS, TOOL_MIN_LEVEL, SPOTIFY_ALIASES,
    pc_control_level, tool_allowed, resolve_media_action, resolve_volume_action,
    send_media_key, MEDIA_ACTION_VK, VOLUME_ACTION_VK,
    send_telegram_desktop, run_elevated_script, open_spotify_protocol,
    telegram_open_chat, app_search,
    window_action, type_text_into, press_hotkey, run_shell_command,
    get_foreground_info, list_visible_windows, lock_workstation, system_power_action,
)


def tools_for_settings(settings: dict | None = None) -> list[dict]:
    lvl = pc_control_level(settings)
    return [t for t in TOOLS if tool_allowed(t["name"], lvl)]


# ─── Helpers ─────────────────────────────────────────────────────────────────

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

APP_ALIASES = {
    "калькулятор": "calc.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "блокнот": "notepad.exe", "notepad": "notepad.exe",
    "проводник": "explorer.exe", "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "paint": "mspaint.exe", "пэйнт": "mspaint.exe", "паинт": "mspaint.exe",
    "диспетчер задач": "taskmgr.exe", "task manager": "taskmgr.exe",
    "параметры": "ms-settings:", "настройки windows": "ms-settings:", "settings": "ms-settings:",
    "командная строка": "cmd.exe", "cmd": "cmd.exe", "powershell": "powershell.exe",
    "терминал": "wt.exe", "terminal": "wt.exe", "windows terminal": "wt.exe",
    "ножницы": "snippingtool.exe", "snipping tool": "snippingtool.exe",
    "хром": "google chrome", "гугл хром": "google chrome", "chrome": "google chrome",
    "ворд": "word", "эксель": "excel", "поверпоинт": "powerpoint", "павер поинт": "powerpoint",
    "вскод": "visual studio code", "vs code": "visual studio code", "vscode": "visual studio code",
    "телега": "telegram", "телеграм": "telegram", "телеграмм": "telegram", "тг": "telegram",
    "дискорд": "discord", "стим": "steam", "спотифай": "spotify", "обс": "obs studio",
    "яндекс браузер": "yandex", "яндекс": "yandex", "фаерфокс": "firefox", "файрфокс": "firefox",
    "эдж": "microsoft edge", "edge": "microsoft edge", "опера": "opera", "ватсап": "whatsapp",
    "зум": "zoom", "ноушен": "notion", "обсидиан": "obsidian",
    "майнкрафт": "minecraft launcher", "брейв": "brave", "фотошоп": "photoshop",
}

# Voice / Vosk mangled English brand names (shared table with stt_normalize).
try:
    from .stt_normalize import brand_alias_targets
    for _alias, _target in brand_alias_targets().items():
        APP_ALIASES.setdefault(_alias, _target)
except Exception:  # pragma: no cover — circular import during partial load
    pass


def translit(s: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in s.lower())


def norm(s: str) -> str:
    s = translit(s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def score_name(query: str, name: str) -> float:
    """0..1 fuzzy match of an app name query against a shortcut name."""
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if n.startswith(q) or q.startswith(n):
        return 0.92
    if re.search(r"\b" + re.escape(q) + r"\b", n):
        return 0.88
    if q in n:
        return 0.8
    ratio = difflib.SequenceMatcher(None, q, n).ratio()
    # also compare against each word of the name
    best_word = max((difflib.SequenceMatcher(None, q, w).ratio() for w in n.split()), default=0.0)
    return max(ratio, best_word * 0.9)


def user_folders() -> list[str]:
    home = os.path.expanduser("~")
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"]
    out = [os.path.join(home, n) for n in names]
    for env in ("OneDrive", "OneDriveConsumer"):
        od = os.environ.get(env)
        if od:
            out.append(od)
    seen, res = set(), []
    for p in out:
        rp = os.path.normcase(os.path.abspath(p))
        if rp not in seen and os.path.isdir(p):
            seen.add(rp)
            res.append(p)
    return res


SKIP_DIRS = {"node_modules", ".git", "__pycache__", "appdata", ".venv", "venv", "$recycle.bin",
             "site-packages", ".cache", "build", "dist"}


def find_files(query: str, roots: list[str] | None = None, *, limit: int = 10,
               time_budget: float = 4.0, max_depth: int = 7) -> list[str]:
    q = (query or "").strip().lower()
    if not q:
        return []
    q_alt = translit(q)
    roots = roots if roots is not None else user_folders()
    hits: list[tuple[float, str]] = []
    t0 = time.monotonic()
    for root in roots:
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() - t0 > time_budget:
                break
            depth = dirpath.count(os.sep) - base_depth
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")
                           and depth < max_depth]
            for name in dirnames + filenames:
                low = name.lower()
                if q in low or (q_alt != q and q_alt in translit(low)):
                    full = os.path.join(dirpath, name)
                    try:
                        mt = os.path.getmtime(full)
                    except OSError:
                        mt = 0.0
                    hits.append((mt, full))
    hits.sort(reverse=True)
    return [p for _mt, p in hits[:limit]]


BLOCKED_EXT = {".bat", ".cmd", ".ps1", ".vbs", ".js", ".reg", ".msi", ".scr", ".com", ".jse", ".wsf"}


def _startfile(target: str) -> None:
    if IS_WIN:
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_hidden(args: list[str], timeout: float = 20.0) -> str:
    """Run a helper process with no console window; return stdout."""
    kw: dict = {"capture_output": True, "timeout": timeout}
    if IS_WIN:
        kw["creationflags"] = NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kw["startupinfo"] = si
    r = subprocess.run(args, **kw)
    for enc in ("utf-8", "cp1251", "cp866"):
        try:
            return r.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.stdout.decode("utf-8", errors="replace")


class AppIndex:
    """Start-menu / desktop shortcuts + UWP apps (Get-StartApps), cached."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.shortcuts: dict[str, str] = {}
        self.uwp: dict[str, str] = {}
        self._built = 0.0

    def build(self, force: bool = False) -> None:
        with self._lock:
            if not force and self._built and time.time() - self._built < 600:
                return
            sc: dict[str, str] = {}
            dirs = []
            appdata = os.environ.get("APPDATA")
            progdata = os.environ.get("PROGRAMDATA")
            if appdata:
                dirs.append(os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"))
            if progdata:
                dirs.append(os.path.join(progdata, "Microsoft", "Windows", "Start Menu", "Programs"))
            dirs.append(os.path.join(os.path.expanduser("~"), "Desktop"))
            pub = os.environ.get("PUBLIC")
            if pub:
                dirs.append(os.path.join(pub, "Desktop"))
            for d in dirs:
                if not os.path.isdir(d):
                    continue
                for dp, _dn, fns in os.walk(d):
                    for fn in fns:
                        stem, ext = os.path.splitext(fn)
                        if ext.lower() in (".lnk", ".url", ".appref-ms"):
                            low = stem.lower()
                            if "uninstall" in low or "deinstall" in low or "удал" in low or "деинстал" in low:
                                continue
                            sc.setdefault(stem, os.path.join(dp, fn))
            self.shortcuts = sc
            if IS_WIN and not self.uwp:
                try:
                    out = run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                      "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                                      "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
                                     timeout=25)
                    uwp = {}
                    for line in out.splitlines():
                        if "|" in line:
                            n, aid = line.rsplit("|", 1)
                            if n.strip() and aid.strip():
                                uwp[n.strip()] = aid.strip()
                    self.uwp = uwp
                except Exception as e:
                    log.info("Get-StartApps failed: %s", e)
            self._built = time.time()
            log.info("app index: %d shortcuts, %d start apps", len(self.shortcuts), len(self.uwp))

    def best(self, query: str) -> tuple[float, str, str, str] | None:
        """(score, display_name, kind, target)"""
        self.build()
        best: tuple[float, str, str, str] | None = None
        for name, path in self.shortcuts.items():
            s = score_name(query, name)
            if best is None or s > best[0]:
                best = (s, name, "lnk", path)
        for name, aid in self.uwp.items():
            s = score_name(query, name) - 0.01  # prefer real shortcuts on ties
            if best is None or s > best[0]:
                best = (s, name, "uwp", aid)
        return best

    def suggestions(self, query: str, n: int = 5) -> list[str]:
        names = list(self.shortcuts) + list(self.uwp)
        names.sort(key=lambda x: score_name(query, x), reverse=True)
        return names[:n]


# ─── Actions ─────────────────────────────────────────────────────────────────

class Actions:
    def __init__(self, core) -> None:
        self.core = core
        self.apps = AppIndex()
        threading.Thread(target=self._prebuild, name="app-index", daemon=True).start()

    def _prebuild(self) -> None:
        try:
            self.apps.build()
        except Exception:
            log.exception("app index build failed")

    def _settings(self) -> dict:
        try:
            return self.core.settings or {}
        except Exception:
            return {}

    def execute(self, name: str, args: dict) -> dict:
        fn = getattr(self, f"do_{name}", None)
        if fn is None:
            return {"ok": False, "error": f"неизвестное действие {name}"}
        if not tool_allowed(name, settings=self._settings()):
            lvl = pc_control_level(self._settings())
            need = TOOL_MIN_LEVEL.get(name, "safe")
            return {"ok": False, "error": (
                f"действие «{name}» недоступно при уровне управления ПК «{lvl}» "
                f"(нужен «{need}» или выше). Смени в настройках ИИ → Управление компьютером."
            )}
        try:
            res = fn(**(args or {}))
        except TypeError as e:
            res = {"ok": False, "error": f"неверные параметры: {e}"}
        except Exception as e:
            log.exception("action %s failed", name)
            res = {"ok": False, "error": str(e)[:200]}
        # Never log Telegram / typed text / full shell command bodies at INFO
        if name == "send_telegram":
            safe_args = {"contact": (args or {}).get("contact"),
                         "message_len": len(str((args or {}).get("message") or ""))}
            safe_res = {k: v for k, v in (res or {}).items() if k != "message"} if isinstance(res, dict) else res
            log.info("action %s(%s) -> %s", name, _short(safe_args), _short(safe_res))
        elif name == "type_text":
            safe_args = {"text_len": len(str((args or {}).get("text") or "")),
                         "window": (args or {}).get("window"),
                         "press_enter": (args or {}).get("press_enter")}
            log.info("action %s(%s) -> %s", name, _short(safe_args), _short(res))
        elif name in ("app_search", "telegram_open_chat"):
            a = args or {}
            safe_args = {k: v for k, v in a.items() if k not in ("query", "chat", "contact")}
            safe_args["text_len"] = len(str(a.get("query") or a.get("chat") or a.get("contact") or ""))
            log.info("action %s(%s) -> %s", name, _short(safe_args), _short(res))
        elif name in ("remember_fact", "forget_fact", "recall_memory"):
            # personal facts never reach the log: only lengths / ids / counts
            a = args or {}
            safe_args = {"text_len": len(str(a.get("text") or a.get("query") or ""))}
            if a.get("category"):
                safe_args["category"] = a.get("category")
            safe_res = {k: v for k, v in res.items() if k in ("ok", "id", "category", "updated", "count", "evicted")} \
                if isinstance(res, dict) else res
            log.info("action %s(%s) -> %s", name, _short(safe_args), _short(safe_res))
        elif name == "run_command":
            cmd = str((args or {}).get("command") or "")
            safe_args = {"command_len": len(cmd), "command_preview": cmd[:80],
                         "shell": (args or {}).get("shell"), "confirm": (args or {}).get("confirm")}
            log.info("action %s(%s) -> %s", name, _short(safe_args), _short(res))
        else:
            log.info("action %s(%s) -> %s", name, _short(args), _short(res))
        return res

    # apps / web / files
    def resolve_app(self, name: str) -> tuple[str, str, str] | None:
        """(kind, display, target) without launching. kind: exe | lnk | uwp | path."""
        q = (name or "").strip()
        if not q:
            return None
        try:
            from .stt_normalize import canonical_app_name
            q = canonical_app_name(q)
        except Exception:
            pass
        alias = APP_ALIASES.get(q.lower()) or APP_ALIASES.get(norm(q))
        if alias and (alias.endswith(".exe") or alias.endswith(":")):
            return ("exe", q, alias)
        query = alias or q
        best = self.apps.best(query)
        if best and best[0] >= 0.72:
            _score, disp, kind, target = best
            return (kind, disp, target)
        if alias is None and query.lower() != norm(query):
            # try transliterated query too ("телеграм" -> "telegram")
            best = self.apps.best(norm(query))
            if best and best[0] >= 0.8:
                _score, disp, kind, target = best
                return (kind, disp, target)
        exe = query if query.lower().endswith(".exe") else query.replace(" ", "") + ".exe"
        if re.fullmatch(r"[A-Za-z0-9_.\-]+\.exe", exe):
            return ("path", exe, exe)
        return None

    def do_open_app(self, name: str) -> dict:
        q = (name or "").strip()
        if not q:
            return {"ok": False, "error": "пустое имя"}
        r = self.resolve_app(q)
        try:
            if r:
                kind, disp, target = r
                if kind == "uwp":
                    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"],
                                     creationflags=NO_WINDOW)
                else:
                    _startfile(target)
                return {"ok": True, "opened": disp}
        except OSError as e:
            log.info("open_app %r failed: %s", q, e)
        return {"ok": False, "error": "не нашёл такое приложение",
                "похожие": self.apps.suggestions(APP_ALIASES.get(q.lower()) or q)}

    def do_open_url(self, url: str) -> dict:
        u = (url or "").strip()
        if not u:
            return {"ok": False, "error": "пустой адрес"}
        if not re.match(r"^https?://", u, re.IGNORECASE):
            if re.match(r"^[a-z]+:", u, re.IGNORECASE):
                return {"ok": False, "error": "разрешены только http/https ссылки"}
            u = "https://" + u
        webbrowser.open(u)
        return {"ok": True, "opened": u}

    def do_web_search(self, query: str, engine: str = "google") -> dict:
        q = urllib.parse.quote_plus((query or "").strip())
        urls = {
            "google": f"https://www.google.com/search?q={q}",
            "youtube": f"https://www.youtube.com/results?search_query={q}",
            "wikipedia": f"https://ru.wikipedia.org/w/index.php?search={q}",
            "yandex": f"https://yandex.ru/search/?text={q}",
        }
        url = urls.get((engine or "google").lower(), urls["google"])
        webbrowser.open(url)
        return {"ok": True, "opened": url}

    def do_find_files(self, query: str, open_first: bool = False) -> dict:
        found = find_files(query)
        res: dict = {"ok": True, "count": len(found), "files": found}
        if open_first and found:
            r = self.do_open_path(found[0])
            res["opened"] = found[0] if r.get("ok") else None
        if not found:
            res["note"] = "ничего не найдено в папках пользователя"
        return res

    def do_open_path(self, path: str) -> dict:
        p = os.path.expandvars(os.path.expanduser((path or "").strip().strip('"')))
        if not p or not os.path.exists(p):
            return {"ok": False, "error": "такого пути нет"}
        if os.path.splitext(p)[1].lower() in BLOCKED_EXT:
            return {"ok": False, "error": "скрипты и установщики я не запускаю из соображений безопасности"}
        _startfile(p)
        return {"ok": True, "opened": p}

    # focus
    def do_start_focus(self, task: str, minutes: int | None = None, rounds: int | None = None) -> dict:
        return self.core.focus_start(task, minutes, rounds)

    def do_stop_focus(self) -> dict:
        return self.core.focus_stop()

    def do_pause_focus(self, resume: bool = False) -> dict:
        return self.core.focus_pause(resume=bool(resume))

    def do_focus_status(self) -> dict:
        return self.core.focus_status()

    # reminders
    def do_set_reminder(self, text: str, minutes: float | None = None, at_time: str | None = None) -> dict:
        now = datetime.now()
        due: datetime | None = None
        if minutes is not None:
            try:
                m = float(minutes)
            except (TypeError, ValueError):
                return {"ok": False, "error": "неверное число минут"}
            if m <= 0 or m > 24 * 60:
                return {"ok": False, "error": "от 1 секунды до 24 часов"}
            due = now + timedelta(minutes=m)
        elif at_time:
            mt = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", str(at_time))
            if not mt:
                return {"ok": False, "error": "время нужно в формате ЧЧ:ММ"}
            hh, mm = int(mt.group(1)), int(mt.group(2))
            if hh > 23 or mm > 59:
                return {"ok": False, "error": "неверное время"}
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
        else:
            return {"ok": False, "error": "укажи minutes или at_time"}
        return self.core.add_reminder(text or "таймер", due)

    def do_list_reminders(self) -> dict:
        return self.core.list_reminders()

    def do_cancel_reminders(self) -> dict:
        return self.core.cancel_reminders()

    def do_get_time(self) -> dict:
        return time_info()

    def do_set_live_mode(self, enabled: bool = True) -> dict:
        return self.core.set_live(bool(enabled))


    def do_media_control(self, action: str = "play", app: str = "", query: str = "", track: str = "",
                         **_extra) -> dict:
        act = resolve_media_action(action or "play")
        q = (query or track or "").strip()
        app_l = (app or "").strip().lower()
        want_spotify = app_l in SPOTIFY_ALIASES or "spotify" in app_l or "спот" in app_l
        if q and (act in (None, "play", "play_pause") or not act) and (want_spotify or not app_l):
            # «включи <трек> в спотике» → search + play the track, not just the media key.
            from .spotify import play_query
            return play_query(q, ensure_open=lambda n: self.do_open_app(n), settings=self._settings())
        if not act:
            return {"ok": False, "error": "неизвестное медиа-действие", "hint": "play/pause/next/previous"}
        opened = False
        if want_spotify or act == "play":
            # For plain «play» without app, still try media key; for Spotify ensure app is up.
            if want_spotify:
                from .pc_power import find_window_by_process
                running = bool(find_window_by_process(["spotify.exe"]))
                if running:
                    pass
                elif open_spotify_protocol():
                    opened = True
                    time.sleep(1.5)
                else:
                    r = self.do_open_app("Spotify")
                    opened = bool(r.get("ok"))
                    if opened:
                        time.sleep(1.5)
                    elif act == "play":
                        return {"ok": False, "error": "Spotify не найден — установи или открой вручную",
                                "detail": r}
        vk = MEDIA_ACTION_VK.get(act)
        if vk is None:
            return {"ok": False, "error": f"нет клавиши для {act}"}
        if not send_media_key(vk):
            if not IS_WIN:
                return {"ok": False, "error": "медиаклавиши доступны только в Windows"}
            return {"ok": False, "error": "не удалось отправить медиаклавишу"}
        out = {"ok": True, "action": act, "key_sent": True}
        if want_spotify:
            out["app"] = "Spotify"
            out["opened"] = opened
        return out

    def do_adjust_volume(self, action: str) -> dict:
        act = resolve_volume_action(action)
        if not act:
            return {"ok": False, "error": "укажи up, down или mute"}
        vk = VOLUME_ACTION_VK[act]
        if not send_media_key(vk):
            if not IS_WIN:
                return {"ok": False, "error": "громкость через медиаклавиши — только Windows"}
            return {"ok": False, "error": "не удалось изменить громкость"}
        return {"ok": True, "action": act}

    def _tg_contact(self, name: str) -> str:
        """«voice_contacts» alias («Мама=@mama_tg») / listed spelling of a declined name («Мамой» → «Мама»)."""
        try:
            from .stt_commands import resolve_contact
            return resolve_contact(name, self._settings())
        except Exception:
            return (name or "").strip()

    def do_send_telegram(self, contact: str = "", message: str = "", chat: str = "", text: str = "",
                         **_extra) -> dict:
        return send_telegram_desktop(self._tg_contact(contact or chat), message or text,
                                     ensure_open=lambda n: self.do_open_app(n))

    def do_telegram_open_chat(self, chat: str = "", contact: str = "", query: str = "", **_extra) -> dict:
        return telegram_open_chat(self._tg_contact(chat or contact or query), ensure_open=lambda n: self.do_open_app(n))

    def do_app_search(self, query: str, app: str = "", search_keys: str = "", then_keys: str = "",
                      press_enter: bool = True, **_extra) -> dict:
        a = (app or "").strip()
        rl = a.lower()
        if a and (rl in SPOTIFY_ALIASES or "spotify" in rl or "спот" in rl) and press_enter is not False \
                and not (search_keys or then_keys):
            from .spotify import play_query  # Spotify search+Enter == play; reuse the verified path
            return play_query(query, ensure_open=lambda n: self.do_open_app(n), settings=self._settings())
        if a and ("telegram" in rl or "телег" in rl or rl in ("тг", "tg")) and press_enter is not False:
            # Always the dedicated flow: focus → Esc → Ctrl+F → paste → Enter. Models pass search_keys
            # «ctrl+k» (Discord habit) which in Telegram Desktop inserts a link instead of searching.
            r = telegram_open_chat(self._tg_contact(query), ensure_open=lambda n: self.do_open_app(n))
            if (search_keys or then_keys) and isinstance(r, dict):
                r.setdefault("note", "")
                r["note"] = (r["note"] + " search_keys/then_keys для Telegram игнорируются — свой надёжный сценарий.").strip()
            return r
        return app_search(a, query, ensure_open=lambda n: self.do_open_app(n), search_keys=search_keys or "",
                          then_keys=then_keys or "", press_enter=press_enter is not False)

    def do_run_elevated(self, path: str, args: str = "") -> dict:
        return run_elevated_script(path, args or "")

    def do_window_control(self, action: str, name: str = "") -> dict:
        return window_action(action, name or "")

    def do_type_text(self, text: str, window: str = "", press_enter: bool = False) -> dict:
        return type_text_into(text, window or "", press_enter=bool(press_enter))

    def do_press_hotkey(self, keys: str, window: str = "") -> dict:
        return press_hotkey(keys, window or "")

    def do_run_command(self, command: str, shell: str = "cmd", confirm: bool = False,
                       timeout: float = 30.0) -> dict:
        return run_shell_command(command, shell=shell or "cmd", confirm=bool(confirm),
                                 timeout=float(timeout or 30))

    def do_get_active_window(self, list_windows: bool = False) -> dict:
        info = get_foreground_info()
        if list_windows and info.get("ok"):
            info["windows"] = list_visible_windows(25)
        return info

    def do_lock_workstation(self) -> dict:
        return lock_workstation()

    def do_system_power(self, action: str) -> dict:
        return system_power_action(action)

    def do_look_at_screen(self, question: str = "Что на экране?") -> dict:
        return self.core.look_at_screen(question)

    # companion memory (memory.py); schemas are added by Brain.tool_list only while memory is on
    def _memory(self):
        mem = getattr(self.core, "memory", None)
        if mem is None or not mem.enabled:
            return None
        return mem

    def do_remember_fact(self, text: str = "", category: str = "", fact: str = "", **_extra) -> dict:
        mem = self._memory()
        if mem is None:
            return {"ok": False, "error": "долгая память выключена (ИИ → Память)"}
        return mem.add(text or fact, category or None, source="model")

    def do_forget_fact(self, query: str = "", text: str = "", id: str = "", **_extra) -> dict:
        mem = self._memory()
        if mem is None:
            return {"ok": False, "error": "долгая память выключена (ИИ → Память)"}
        return mem.forget(id or query or text)

    def do_recall_memory(self, query: str = "", **_extra) -> dict:
        mem = self._memory()
        if mem is None:
            return {"ok": False, "error": "долгая память выключена (ИИ → Память)"}
        return mem.recall(query or "")


def time_info(now: datetime | None = None) -> dict:
    n = now or datetime.now()
    return {
        "time": n.strftime("%H:%M"),
        "date": f"{n.day} {MONTHS_GEN[n.month]} {n.year}",
        "weekday": WEEKDAYS_FULL[n.weekday()],
    }


def _short(x: object, n: int = 300) -> str:
    s = str(x)
    return s if len(s) <= n else s[:n] + "…"
