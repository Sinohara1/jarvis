"""Conversation brain: system prompt, history, tool loop, transcription,
screen classification, and free-tier-friendly fallback/backoff."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime

from . import providers as P
from .actions import TOOLS, time_info, tools_for_settings
from .pc_power import pc_control_level, tool_allowed
from . import persona
from . import lang as L
from . import memory as M
from . import refusal_guard as RG
from .config import PROVIDERS, api_key_for

log = logging.getLogger("jarvis")

MAX_TOOL_ROUNDS = 5
MAX_HISTORY = 30
LOCAL_FAST_PATH = False  # local model: always declare tools (keeps Ollama's prompt cache warm)

SYSTEM_PROMPT = """Ты — {name}, тёплый ИИ-компаньон {user_gen} на компьютере с Windows. Тебя зовут {name}, так и представляйся.
Твой характер: {character}
Обращайся на «ты». Держи этот характер в каждом ответе, но оставайся полезным.
Ты не сторонний наблюдатель: делаешь дела вместе с пользователем и за него — открываешь, включаешь, ставишь, ищешь, а при уровне «полный» ещё управляешь окнами, вводишь текст, жмёшь клавиши и выполняешь команды. Когда просят действие — сразу вызывай инструмент, а не только комментируй.
Главная миссия: помогать {user_dat} реально делать то, что нужно, а не залипать в TikTok и шортсы. Возвращай к делу в своём стиле.

Правила ответа:
- Твои ответы озвучиваются голосом. Отвечай коротко и разговорно: обычно 1–2 предложения, максимум 3–4, если просят объяснить.
- Никакого markdown, списков, эмодзи и ссылок в тексте. Числа и время пиши так, как их удобно произносить.
- {lang_rule}
- Если нужно действие на компьютере — вызывай инструменты, а не описывай, как это сделать. После действия коротко подтверди.
- Когда пользователь говорит, чем сейчас занимается и сколько времени («делаю домашку по физике 40 минут») — сразу вызывай start_focus.
- Музыка («включи музыку», «включи спотик», «пауза», «следующий») — media_control, не open_app. Конкретный трек («включи Can't Fault Das в спотике», «play Numb on Spotify») — media_control с action="play", app="spotify", query="<трек/исполнитель>": Jarvis сам найдёт и запустит его.
- При «полном» управлении: окна → window_control; ввод → type_text; клавиши → press_hotkey; кнопка play/«нажми плей»/продолжить видео → media_control (action="play") или press_hotkey ("space"/"enter" в нужном окне); shell → run_command; телеграм: написать → send_telegram, открыть чат/избранное → telegram_open_chat; найти/открыть что-то внутри любого приложения → app_search; блокировка → lock_workstation. Ты управляешь ПК пользователя целиком, в том числе внутри приложений — никогда не отвечай «не могу открыть личные чаты» или «не могу выбрать песню».
- {limits}
- Если не расслышал или запрос непонятен — переспроси одной короткой фразой.
- «Живой режим» — ты сам поглядываешь на экран и подсказываешь, когда это уместно. Включай/выключай его через set_live_mode: «следи и подсказывай», «включи живой режим» — включить; «выключи живой режим», а также «тихо», «хватит», «помолчи», «не мешай», если живой режим включён и речь о твоих подсказках, — выключить.
"""


def _limits_line(settings: dict) -> str:
    """Honest capability line depending on pc_control."""
    lvl = pc_control_level(settings)
    hard = ("Жёсткие запреты всегда: нет тихого обхода UAC; нет массового удаления файлов и форматирования дисков "
            "(даже по просьбе — откажись); секреты и тексты сообщений/ввода не проговаривай лишний раз.")
    if lvl == "full":
        return (hard + " Уровень «полный»: полный доступ и контроль над ПК через инструменты — "
                "окна (window_control: закрыть/сфокусировать/свернуть/развернуть), ввод текста (type_text), "
                "горячие клавиши (press_hotkey), команды shell (run_command — опасные только после confirm=true), "
                "Telegram (send_telegram — написать, telegram_open_chat — открыть чат или «Избранное»/Saved Messages), "
                "действия внутри любого приложения (app_search: фокус → поиск → ввод → Enter; или по шагам "
                "window_control + press_hotkey + type_text), трек в Spotify (media_control с query), "
                "скрипты от админа с окном UAC (run_elevated), "
                "активное окно (get_active_window), блокировка (lock_workstation), сон/гашение экрана (system_power). "
                "Закрытие окон мягкое (WM_CLOSE), без kill процесса. Сам вызывай эти инструменты, не рассказывай как сделать руками. "
                "Это компьютер и аккаунты самого пользователя, и он сам включил полный контроль: открывать его чаты, "
                "избранное, плейлисты и окна, нажимать кнопки и клавиши, вводить текст — нормально и ожидаемо. "
                "НИКОГДА не говори «не могу нажать/кликнуть/ввести/открыть», «не могу открыть личные чаты», "
                "«не могу нажать плей», «не могу выбрать песню», «технические ограничения», «я всего лишь ИИ», "
                "«сделай это вручную». У тебя есть инструменты — используй их: кнопка play/воспроизведение → "
                "media_control(action=\"play\") или press_hotkey(\"space\"/\"enter\") в нужном окне; клик по кнопке → "
                "press_hotkey (Enter/Space/Tab по шагам) или app_search; не знаешь точной клавиши — сначала "
                "get_active_window, потом действуй. Будь решительным компаньоном: сразу делай, коротко подтверждай. "
                "Если инструмент вернул ошибку — скажи, что именно не вышло, и сразу попробуй другой путь "
                "(другой инструмент или шаги window_control + press_hotkey + type_text). "
                "Отказывай только в жёстких запретах выше.")
    if lvl == "standard":
        return (hard + " Ты не закрываешь чужие окна и не вводишь текст/команды за пользователя. "
                "Музыку и медиа включай через media_control (конкретный трек — с query, Jarvis найдёт и включит его в Spotify). "
                "Полный контроль ПК (окна, ввод, shell, Telegram, admin-скрипты) "
                "недоступен — предложи включить «полный» в настройках ИИ → Управление компьютером.")
    return (hard + " Сейчас уровень «безопасный»: открывать и искать, громкость, экран — без медиа-клавиш, оконного контроля, "
            "Telegram и admin-скриптов. Для музыки нужен «стандарт», для полного контроля ПК — «полный».")


def build_system_prompt(settings: dict) -> str:
    user = str(settings.get("user_name") or "").strip()
    name = persona.assistant_name(settings)
    head = SYSTEM_PROMPT.format(
        name=name, character=persona.character_text(settings), lang_rule=L.prompt_rule(settings),
        limits=_limits_line(settings),
        user_gen=f"пользователя по имени {user}" if user else "пользователя",
        user_dat=f"пользователю ({user})" if user else "пользователю")
    if user:
        head += f"Пользователя зовут {user}.\n"
    return head

SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "on_task": {"type": "boolean"},
        "confidence": {"type": "number"},
        "activity": {"type": "string"},
    },
    "required": ["on_task", "confidence", "activity"],
}


VOICE_HINT = ("\nЭто голосовая реплика: запрос распознан локально и может содержать мелкие ошибки распознавания "
              "(окончания, слитные слова) — понимай по смыслу и не переспрашивай из-за них. Ответ сразу озвучивается: "
              "одно короткое предложение, максимум два (до 25 слов), сразу по сути, без вступлений, меток вроде «Короткий ответ:» и повторения вопроса.\n"
              "Если нужен инструмент: в том же ответе сначала скажи одну очень короткую фразу о том, что делаешь "
              "строго на языке ответа (например «Открываю ютуб.»), и сразу вызови инструмент. После успешного выполнения ничего "
              "не повторяй (ответь пустой строкой); говори, только если результат важен: нашёл файлы, ошибка, вопрос.\n")

# Extra block for Ollama/gemma: small models drift to English, call themselves Jarvis, or say «открываю» without a tool call.
def local_model_hint(settings: dict, lang_code: str) -> str:
    name = persona.assistant_name(settings)
    code = L.norm(lang_code)
    rule = L.LANGS[code]["rule"]
    native = (L.LANGS[code].get("native_rule") or "").strip()
    lang_line = f"Язык ответа: только {rule}."
    if native:
        lang_line += " " + native
    if code == "ru":
        lang_line += " Не переходи на английский, даже если вопрос короткий."
    else:
        lang_line += " Весь ответ целиком на этом языке."
    not_jarvis = ""
    if not persona.is_default_name(name):
        not_jarvis = (f" Не называй себя Jarvis, Джарвис или другим именем — сравнение характера с «Джарвисом» "
                      f"в описании это только стиль, не твоё имя.")
    media = ""
    if tool_allowed("media_control", settings=settings):
        media = (" «Включи музыку» / «включи спотик» / «пауза» / «следующий» → media_control "
                 "(app=\"spotify\" для Спотифая), не open_app. «Включи <трек> в спотике» / «play <song> on Spotify» → "
                 "media_control(action=\"play\", app=\"spotify\", query=\"<трек>\") — найдёт и запустит трек; "
                 "не отвечай, что не можешь выбрать песню.")
    full = ""
    if tool_allowed("window_control", settings=settings):
        full = (
            " Полный контроль ПК: «закрой/сверни/разверни …» → window_control; "
            "«введи/напиши в окне …» → type_text; «нажми ctrl+…» → press_hotkey; "
            "«выполни команду …» → run_command; «что за окно» → get_active_window; "
            "«заблокируй комп» → lock_workstation; «усни/погаси экран» → system_power; "
            "«напиши в телеграм …» / «write <кому> on Telegram, <текст>» → send_telegram; «открой избранное / Saved Messages / чат с … в телеграме» → "
            "telegram_open_chat (и после «открой телеграм» на «зайди в чат с …» / «найди ник …» — тоже telegram_open_chat, имя в именительном падеже); «найди/открой … в <приложении>» (Discord, Chrome, VS Code, Steam, Проводник…) → app_search; "
            "«запусти скрипт от админа» → run_elevated. "
            "«нажми плей» / «продолжи видео» / «пауза» → media_control (action play/pause); «нажми пробел/энтер/…» → "
            "press_hotkey (space/enter/…); «кликни по …» → press_hotkey или app_search. "
            "Ты управляешь всем ПК, включая содержимое приложений, пользователь сам дал полный контроль. "
            "НИКОГДА не говори «не могу нажать», «не могу кликнуть», «не могу открыть личные чаты», «технические "
            "ограничения», «я всего лишь ИИ», «сделай вручную» — у тебя есть инструменты, вызывай их. "
            "Не объясняй как сделать руками — сразу инструмент, коротко и уверенно."
        )
    elif tool_allowed("send_telegram", settings=settings):
        full = " «Напиши в телеграм …» / «write <кому> on Telegram, <текст>» → send_telegram (contact + message). Скрипт от админа → run_elevated."
    return (
        f"\nЛокальная модель — жёсткие правила этого хода:\n"
        f"- Тебя зовут {name}. Если спрашивают «как тебя зовут» / «who are you» — отвечай «{name}».{not_jarvis}\n"
        f"- {lang_line}\n"
        f"- Ты компаньон: делай действия инструментами, не только описывай.\n"
        f"- Команды «открой …», «запусти …» (телеграм/тг, калькулятор, ютуб, сайт…): "
        f"обязательно вызови инструмент (open_app / open_url / web_search), не ограничивайся словами «открываю» без вызова. "
        f"Для приложений в name передавай латинское имя: Telegram, Spotify, Discord, Chrome, Steam, Calculator."
        f"{media}{full}\n"
    )
MEMORY_LOCAL_HINT = ("- Долгая память: «запомни, что …» → remember_fact (если в промпте сказано, что уже сохранено, — "
                     "просто подтверди); «забудь, что …» → forget_fact; «что ты обо мне знаешь?» → recall_memory.\n")
NO_TOOLS_HINT = ("\nСейчас инструменты недоступны. Если для ответа нужно действие на компьютере (открыть программу, сайт "
                 "или файл, поиск, музыка/медиа, нажать кнопку/клавишу/play, окна, ввод, команды, напоминание, фокус, экран, живой режим, телеграм, скрипт) — "
                 "ответь ровно: [[TOOLS]]\n")
ESCAPE = "[[TOOLS]]"
# v1.6 conversation mode: a phrase heard without the name may not be meant for her → the model answers this token
SILENT = "[[SILENT]]"
OVERHEARD_PREFIX = "(услышано без обращения) "
OVERHEARD_HINT = {
    "follow": ("\nРежим разговора: эта фраза услышана микрофоном сразу после твоего ответа, БЕЗ обращения по имени. "
               "Обычно это продолжение разговора с тобой — отвечай. Но если фраза явно не тебе (он говорит с другим "
               "человеком или по телефону, это звук видео/игры, мысли вслух, обрывок без смысла) — ответь ровно "
               "[[SILENT]] и больше ничего. Если сомневаешься — отвечай как обычно.\n"),
    "always": ("\nПостоянное прослушивание: микрофон слышит всё в комнате, эта фраза услышана БЕЗ обращения по имени. "
               "Отвечай, только если она явно адресована тебе (вопрос или просьба к ассистенту, продолжение вашего "
               "разговора). Разговоры с другими людьми, звонки, звук видео/игр/музыки, мысли вслух, обрывки — "
               "ответь ровно [[SILENT]] и больше ничего, не комментируй. Если сомневаешься — [[SILENT]].\n"),
}

# Voice questions that clearly need no PC action are answered without tool declarations: measured
# first token 0.48 s vs 0.70 s with tools (gemini-3.5-flash-lite). Anything that smells like an action
# goes straight to the tool path; if the model still wants a tool it answers [[TOOLS]] and we retry.
_ACTION_RE = re.compile(
    r"(откр|запус|включ|выключ|отключ|найд|найт|поищ|ищи|загугл|погугл|поставь|постав|напом|таймер|засек|будильн|"
    r"фокус|помодоро|сесси|пауз|останов|продолж|отмен|экран|посмотри|глянь|что у меня|живой|следи|подсказ|"
    r"тихо|хватит|помолчи|не меша|сайт|ютуб|youtube|браузер|файл|папк|документ|скача|музык|видео|телеграм|"
    r"дискорд|стим|steam|chrome|хром|спотиф|спотик|календар|делаю|занимаюсь|работаю|учу|домашк|готовлюсь|минут|\bчас|полчаса|"
    r"напиш|отправ|громч|тише|мьют|мут|скрипт|админ|powershell|\.ps1|\.bat|"
    r"сверн|разверн|максим|миним|закр|введ|напечат|хоткей|горяч|заблокир|заблок|усн|спящ|погас|монитор|"
    r"команд|\bcmd\b|shell|\bplay\b|\bopen\b|\bsearch\b|\bfind\b|spotify|telegram|discord|избранн|saved|"
    r"трек|песн|чат|переписк|запомн|забуд|забыть|remember|forget|"
    r"нажм|жми|кликн|клик|клацн|тыкн|ткни|щёлкн|щелкн|плей|плэй|пробел|энтер|кнопк|вкладк|"
    r"\bpress\b|\bclick\b|\btap\b|\btype\b|\bpause\b|\bresume\b)",
    re.IGNORECASE)


def needs_tools(text: str) -> bool:
    t = (text or "").lower()
    if len(t.split()) <= 2:  # «да», «давай», «нет» — may confirm a pending action
        return True
    return bool(_ACTION_RE.search(t)) or RG.is_action_request(t)


class _NeedTools(Exception):
    pass


class TurnCancelled(Exception):
    """The user interrupted (new question / stop) while the reply was streaming."""


class _SilentGate:
    """Holds the first characters of a streamed reply until it is clear whether it starts with [[SILENT]];
    a silent reply is swallowed completely (never shown, never spoken)."""

    def __init__(self, on_text) -> None:
        self.on_text = on_text
        self.buf = ""
        self.passing = False
        self.silent = False

    def feed(self, delta: str) -> None:
        if self.silent:
            return
        if self.passing:
            d = delta.replace(SILENT, "")
            if d:
                self.on_text(d)
            return
        self.buf += delta
        acc = self.buf.lstrip()
        if acc.startswith(SILENT):
            self.silent = True
            return
        if not acc or SILENT.startswith(acc):
            return
        self.passing = True
        self.on_text(acc.replace(SILENT, ""))

    def flush(self) -> None:
        """End of reply: text held back that never became [[SILENT]] (e.g. «[[») is released."""
        if not self.passing and not self.silent and self.buf.strip():
            self.passing = True
            self.on_text(self.buf.lstrip())


def is_silent_reply(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith(SILENT) or t in ("[SILENT]", "SILENT", "[[SILENT]")


class Brain:
    def __init__(self, get_settings, actions, get_context=None) -> None:
        self.get_settings = get_settings
        self.actions = actions
        self.get_context = get_context or (lambda: "")
        self.lock = threading.Lock()
        self.history: list = []
        self._hist_provider: str | None = None
        self._providers: dict[tuple, P.Provider] = {}
        self.cooldown_until = 0.0  # chat model rate-limit cooldown (monotonic)
        self.lite_cooldown_until = 0.0
        self.last_model = ""
        self.current_model = ""
        self._lang = "ru"  # language of the current reply (for the app's own fallback phrases)
        # v1.5: plain-text transcript, provider-neutral — rebuilt into a provider's native history when the
        # answering provider changes (local model ↔ Gemini fallback keeps the conversation)
        self.transcript: list[tuple[str, str]] = []
        self.local = None          # LocalController (set by the app); None → cloud only
        self.last_provider = ""
        self.last_fallback = ""    # "ollama→gemini: reason" of the last turn, for logs/UI
        # companion memory (MemoryStore, set by the app); None → no long-term memory (tests, test_ai)
        self.memory = None
        self._query = ""           # current user text: picks relevant memories for the system prompt
        self._mem_note = ""        # «запомни, что …» already saved this turn → tell the model
        self._extract_turns = 0
        self._extracting = False
        self._overheard = ""       # v1.6: "follow" / "always" while answering a phrase heard without the name
        self.last_silent = False   # the last ask() decided the phrase was not addressed to her

    # ── provider plumbing ──
    def provider(self, name: str | None = None) -> P.Provider:
        s = self.get_settings()
        name = name or s["provider"]
        if name == "ollama":
            from .local_ai import OllamaProvider, DEFAULT_URL
            base = s["base_urls"].get("ollama") or DEFAULT_URL
            prov = self._providers.get(("ollama", base))
            if prov is None:
                prov = OllamaProvider(base_url=base)
                self._providers[("ollama", base)] = prov
            if self.local is not None:
                prov.keep_alive_fn = self.local.keep_alive
            return prov
        key = api_key_for(s, name)
        base = s["base_urls"].get(name) or PROVIDERS[name]["base_url"]
        if not key:
            raise P.NoKeyError("Нет API-ключа", provider=name)
        ck = (name, key, base)
        prov = self._providers.get(ck)
        if prov is None:
            prov = P.make_provider(name, key, base)
            self._providers[ck] = prov
        return prov

    def models(self, name: str | None = None) -> tuple[str, str]:
        s = self.get_settings()
        name = name or s["provider"]
        m = s["models"].get(name) or {}
        if name == "ollama":  # one local model does everything
            chat = m.get("chat") or PROVIDERS[name]["chat"]
            return chat, chat
        return m.get("chat") or PROVIDERS[name]["chat"], m.get("lite") or PROVIDERS[name]["lite"]

    def route(self, kind: str = "chat") -> list[str]:
        """Providers to try, in order: local model and/or the cloud provider (see LocalController.route)."""
        s = self.get_settings()
        cloud = s["provider"]
        if self.local is None:
            return [cloud]
        return self.local.route(kind, cloud, bool(api_key_for(s, cloud)))

    def _use_history(self, name: str, prov: P.Provider) -> None:
        if self._hist_provider == name:
            return
        self.history = [prov.user_message(t) if r == "user" else prov.assistant_message(t)
                        for r, t in self.transcript[-MAX_HISTORY:] if t]
        self._hist_provider = name

    def _note_local(self, name: str, ok: bool, err: str = "") -> None:
        if name == "ollama" and self.local is not None:
            try:
                self.local.note_result(ok, err)
            except Exception:
                pass

    def reset(self) -> None:
        with self.lock:
            self.history = []
            self.transcript = []

    def _system(self, voice: bool = False) -> str:
        ti = time_info()
        ctx = self.get_context() or ""
        st = self.get_settings()
        extra = ""
        wishes = str(st.get("persona") or "").strip()
        if wishes:
            extra += f"\nДополнительные пожелания пользователя к твоему поведению (соблюдай их):\n{wishes[:2000]}\n"
        if st.get("confirm_actions"):
            extra += ("\nРежим подтверждения включён: прежде чем открыть программу, файл или сайт, коротко спроси "
                      "«Открыть …?» и вызывай инструмент только после явного «да».\n")
        extra += self._memory_block()
        return (build_system_prompt(st) + extra
                + f"\nСейчас: {ti['weekday']}, {ti['date']}, {ti['time']}.\n"
                + (ctx + "\n" if ctx else "") + (VOICE_HINT if voice else "")
                + OVERHEARD_HINT.get(self._overheard, "") + L.turn_rule(st, self._lang))

    def _ok_text(self) -> str:
        """Filler for an empty reply. A phrase heard without the name + an empty reply = she stays silent."""
        return "" if self._overheard else L.text("ok", self._lang)

    # ── companion memory ──
    def _memory_on(self) -> bool:
        try:
            return self.memory is not None and self.memory.enabled
        except Exception:
            return False

    def _memory_block(self) -> str:
        if not self._memory_on():
            return ""
        try:
            block = self.memory.prompt_block(self._query)
        except Exception:
            log.debug("memory prompt failed", exc_info=True)
            block = ""
        return block + self._mem_note

    def _capture_explicit(self, text: str) -> str:
        """«Запомни, что …» → save without the model; returns a note for the system prompt."""
        if not self._memory_on():
            return ""
        try:
            if not self.memory.settings()["explicit"]:
                return ""
            fact = M.explicit_fact(text)
            if not fact:
                return ""
            r = self.memory.add(fact, source="explicit")
        except Exception:
            log.exception("memory: explicit capture failed")
            return ""
        if r.get("ok"):
            return (f"\nТолько что по просьбе пользователя сохранено в долгую память: «{fact}». "
                    "remember_fact для этого не вызывай — просто коротко подтверди, что запомнил.\n")
        return ("\nПользователь попросил что-то запомнить, но это не сохранено: "
                f"{r.get('error') or 'не получилось'}. Коротко и вежливо скажи об этом.\n")

    def _maybe_extract(self) -> None:
        """Every few turns (if enabled) let the model pick stable facts from his recent messages, in the background."""
        if not self._memory_on() or self._extracting:
            return
        try:
            if not self.memory.settings()["auto_extract"]:
                self._extract_turns = 0
                return
        except Exception:
            return
        self._extract_turns += 1
        if self._extract_turns < M.EXTRACT_EVERY:
            return
        self._extract_turns = 0
        lines = [t for r, t in self.transcript[-2 * M.EXTRACT_EVERY:]
                 if r == "user" and t and not t.startswith("(Живой режим") and not t.startswith(OVERHEARD_PREFIX)]
        if not lines:
            return
        self._extracting = True
        threading.Thread(target=self._extract_worker, args=(lines,), name="memory-extract", daemon=True).start()

    def _extract_worker(self, lines: list[str], delay: float = 12.0) -> None:
        try:
            # wait for a quiet moment: never compete with a live turn for the (local) model
            t_end = time.monotonic() + 120
            time.sleep(delay)
            while self.lock.locked() and time.monotonic() < t_end:
                time.sleep(2.0)
            prompt = M.extract_prompt(lines, self.memory.all())
            raw = self.complete(M.EXTRACT_SYSTEM, [{"role": "user", "content": prompt}], temperature=0.2)
            facts = M.parse_extracted(raw)
            added = 0
            for f in facts:
                if self.memory.add(f["text"], f["category"], source="auto").get("ok"):
                    added += 1
            log.info("memory: авто-извлечение — %d кандидатов, сохранено %d", len(facts), added)
        except Exception as e:
            log.info("memory: авто-извлечение не удалось (%s)", type(e).__name__)
        finally:
            self._extracting = False

    # ── speech to text ──
    def transcribe(self, wav: bytes, lang: str = "ru") -> str:
        """Gemini (inline audio) when a Gemini key exists, else OpenAI Whisper."""
        s = self.get_settings()
        plan: list[tuple[str, list[str]]] = []
        if api_key_for(s, "gemini"):
            if s["provider"] == "gemini":
                chat_m, lite_m = self.models("gemini")
            else:
                chat_m, lite_m = PROVIDERS["gemini"]["chat"], PROVIDERS["gemini"]["lite"]
            plan.append(("gemini", [m for m in dict.fromkeys([lite_m, chat_m, "gemini-flash-lite-latest"]) if m]))
        if api_key_for(s, "openai"):
            plan.append(("openai", ["whisper-1"]))
        if not plan:
            raise P.NoKeyError("Для голоса нужен ключ Gemini или OpenAI")
        last: Exception | None = None
        for name, models in plan:
            prov = self.provider(name)
            for model in models:
                try:
                    t0 = time.monotonic()
                    text = prov.transcribe(model, wav, timeout=30, lang=lang)
                    log.info("transcribed via %s/%s in %.1fs: %r", name, model,
                             time.monotonic() - t0, text[:120])
                    return text
                except (P.RateLimitError, P.OverloadedError, P.ModelNotFoundError) as e:
                    last = e
                    continue
        raise last or P.ProviderError("Не удалось распознать речь")

    # ── chat with tools ──
    def ask(self, text: str, on_tool=None, on_text=None, *, fast: bool = False, voice: bool = False,
            lang: str | None = None, overheard: str = "") -> str:
        """One user turn. on_text(delta) streams the visible reply as it arrives (all tool rounds).
        fast=True → lite model first (voice turns with «Быстрые ответы»), chat model as fallback.
        v1.5: tries the route in order (local model, then the cloud) — a failed local attempt that has not
        said or done anything yet falls through to the cloud with the same conversation.
        v1.6: overheard="follow"/"always" — the phrase came without the name (conversation mode); the model may
        answer [[SILENT]] → returns "" and sets last_silent (nothing is shown or spoken)."""
        with self.lock:
            self._overheard = overheard if overheard in OVERHEARD_HINT else ""
            self.last_silent = False
            sgate = _SilentGate(on_text) if (self._overheard and on_text is not None) else None
            if sgate is not None:
                on_text = sgate.feed
            try:
                return self._ask_locked(text, on_tool, on_text, fast=fast, voice=voice, lang=lang, sgate=sgate)
            finally:
                self._overheard = ""

    def _ask_locked(self, text: str, on_tool, on_text, *, fast: bool, voice: bool, lang: str | None,
                    sgate: "_SilentGate | None") -> str:
        if True:
            s = self.get_settings()
            self._lang = lang or L.reply_lang(s, L.detect(text, prefer=L.speech_lang(s)))
            self._query = text
            self._mem_note = self._capture_explicit(text)
            names = self.route("voice" if voice else "chat")
            self.last_fallback = ""
            last: Exception | None = None
            for i, name in enumerate(names):
                try:
                    prov = self.provider(name)
                except P.NoKeyError as e:
                    last = e
                    continue
                chat_m, lite_m = self.models(name)
                self._use_history(name, prov)
                self.history.append(prov.user_message(text))
                start_len = len(self.history) - 1
                progress = {"text": False, "tool": False}

                def _on_text(d, _p=progress):
                    _p["text"] = True
                    if on_text:
                        on_text(d)

                def _on_tool(n, a, _p=progress):
                    _p["tool"] = True
                    if on_tool:
                        on_tool(n, a)

                route = (voice and on_text is not None and not s.get("confirm_actions") and not needs_tools(text)
                         and (name != "ollama" or LOCAL_FAST_PATH))
                try:
                    t0 = time.monotonic()
                    reply = self._loop(prov, chat_m, lite_m, _on_tool, _on_text if on_text else None,
                                       fast=fast, voice=voice, route=route)
                except Exception as e:
                    if isinstance(e, P.ProviderError) and not getattr(e, "provider", ""):
                        e.provider = name
                    del self.history[start_len:]  # keep history consistent
                    fallback_ok = (i < len(names) - 1 and isinstance(e, P.ProviderError)
                                   and not progress["text"] and not progress["tool"])
                    self._note_local(name, False, str(e))
                    if fallback_ok:
                        self.last_fallback = f"{name}→{names[i + 1]}: {e}"
                        log.info("%s failed (%s: %s) → %s", name, type(e).__name__, getattr(e, "detail", "") or e,
                                 names[i + 1])
                        last = e
                        continue
                    raise
                self._note_local(name, True)
                self.last_provider = name
                log.info("turn answered by %s in %.2fs", name, time.monotonic() - t0)
                if self._overheard and ((sgate is not None and sgate.silent) or is_silent_reply(reply)
                                        or not (reply or "").strip()) and not progress["tool"]:
                    self.last_silent = True
                    log.info("overheard phrase: model stayed silent (not addressed to her)")
                    self.transcript += [("user", OVERHEARD_PREFIX + text), ("assistant", SILENT)]
                    self.transcript = self.transcript[-MAX_HISTORY:]
                    self._trim()
                    self._mem_note = ""
                    return ""
                if sgate is not None:
                    sgate.flush()
                reply = (reply or "").replace(SILENT, "").strip() if self._overheard else reply
                self.transcript += [("user", text), ("assistant", reply)]
                self.transcript = self.transcript[-MAX_HISTORY:]
                self._trim()
                self._mem_note = ""
                self._maybe_extract()
                return reply
            raise last or P.ProviderError("Нет ответа")

    def _plan(self, chat_m: str, lite_m: str, fast: bool) -> list[str]:
        now = time.monotonic()
        chat_ok = now >= self.cooldown_until
        lite_ok = now >= self.lite_cooldown_until
        if fast:
            order = [lite_m, chat_m] if lite_ok or not chat_ok else [chat_m, lite_m]
        else:
            order = [chat_m, lite_m] if chat_ok else [lite_m, chat_m]
        return [m for m in dict.fromkeys(order) if m]

    def _cool(self, model: str, chat_m: str, e: P.RateLimitError) -> None:
        # a per-day quota will not come back in a minute: stop knocking for hours, not 5 min
        sec = min(6 * 3600.0, e.retry_after or 3600.0) if e.daily else min(300.0, e.retry_after or 60.0)
        until = time.monotonic() + sec
        if model == chat_m:
            self.cooldown_until = until
        else:
            self.lite_cooldown_until = until
        log.info("rate limit on %s: %s, skip it for %.0f s", model, "daily quota" if e.daily else "per-minute", sec)

    def tool_list(self) -> list:
        base = tools_for_settings(self.get_settings())
        if self._memory_on():
            base = base + list(M.MEMORY_TOOLS)
        return base

    def complete(self, system: str, messages: list[dict], temperature: float = 0.9) -> str:
        """Отдельный одноразовый диалог (авто-извлечение памяти). Не трогает историю главного чата и не даёт инструменты."""
        last: Exception | None = None
        for name in self.route("chat"):
            try:
                prov = self.provider(name)
            except P.NoKeyError as e:
                last = e
                continue
            chat_m, _lite = self.models(name)
            native = []
            for m in messages:
                text = str(m.get("content") or "")
                if not text:
                    continue
                native.append(prov.assistant_message(text) if m.get("role") == "assistant"
                              else prov.user_message(text))
            if not native:
                native = [prov.user_message("Продолжи сцену.")]
            try:
                res = prov.chat(chat_m, system, native, tools=None, timeout=70, temperature=temperature)
                self._note_local(name, True)
                return (res.text or "").strip()
            except Exception as e:
                last = e
                self._note_local(name, False, str(e))
                continue
        raise last or P.ProviderError("Нет ответа")

    def _call(self, prov: P.Provider, chat_m: str, lite_m: str, on_text=None, *, fast: bool = False,
              voice: bool = False, tools: list | None = TOOLS, extra: str = "") -> P.LLMResult:
        """Chat call with fallback between chat/lite models on overload / rate limit."""
        if tools is TOOLS:
            tools = self.tool_list()
        system = self._system(voice) + extra
        if prov.name == "ollama":
            system += local_model_hint(self.get_settings(), self._lang)
            if tools and self._memory_on():
                system += MEMORY_LOCAL_HINT
        models = self._plan(chat_m, lite_m, fast)
        last: Exception | None = None
        streamed = [False]

        def emit(delta: str) -> None:
            streamed[0] = True
            if on_text:
                on_text(delta)

        max_tokens = 600 if voice else 2048
        for i, model in enumerate(models):
            first_timeout = 12 if (i == 0 and len(models) > 1) else 30
            try:
                t0 = time.monotonic()
                self.current_model = model
                if on_text is not None:
                    res = prov.chat_stream(model, system, self.history, tools, on_text=emit,
                                           timeout=first_timeout, max_tokens=max_tokens)
                else:
                    res = prov.chat(model, system, self.history, tools, timeout=first_timeout)
                self.last_model = res.model or model
                log.info("chat %s %.1fs tools=%s%s", res.model or model, time.monotonic() - t0,
                         [c.name for c in res.tool_calls], " (stream)" if on_text else "")
                return res
            except P.RateLimitError as e:
                last = e
                if streamed[0]:
                    raise
                self._cool(model, chat_m, e)
                if e.retry_after and e.retry_after <= 6 and i == len(models) - 1 and not e.daily:
                    time.sleep(e.retry_after + 0.3)
                    if on_text is not None:
                        return prov.chat_stream(model, system, self.history, tools, on_text=emit,
                                                timeout=30, max_tokens=max_tokens)
                    return prov.chat(model, system, self.history, tools, timeout=30)
                continue
            except (P.OverloadedError, P.ModelNotFoundError) as e:
                last = e
                if streamed[0]:
                    raise
                continue
        raise last or P.ProviderError("Нет ответа")

    def _loop(self, prov: P.Provider, chat_m: str, lite_m: str, on_tool, on_text=None, *,
              fast: bool = False, voice: bool = False, route: bool = False) -> str:
        said: list[str] = []
        if route:
            res = self._try_without_tools(prov, chat_m, lite_m, on_text, fast=fast)
            if res is not None:
                if res.raw_message is not None:
                    self.history.append(res.raw_message)
                return res.text.replace(ESCAPE, "").strip() or self._ok_text()
        # Safety net: on a clear action request the model must not answer «не могу / технические ограничения»
        # — at pc_control=full it has the tools. Such a reply is held back (never spoken) and replaced by the
        # matching tool call (refusal_guard), or the model is retried once with a forcing hint.
        user_text = self._query
        st = self.get_settings()
        guard = RG.guard_active(st, user_text)
        forced = False
        used_tool = False
        for _round in range(MAX_TOOL_ROUNDS):
            gate = _RefusalGate(on_text) if (guard and not used_tool and on_text is not None) else None
            res = self._call(prov, chat_m, lite_m, gate.feed if gate else on_text, fast=fast, voice=voice,
                             extra=RG.FORCE_TOOL_HINT if forced else "")
            if _round == 0 and prov.name == "ollama" and not res.text and not res.tool_calls:
                raise P.OverloadedError("Локальная модель ответила пустотой")
            if gate is not None:
                gate.close()
            if guard and not used_tool and not res.tool_calls and (
                    RG.is_refusal(res.text) or (gate is not None and gate.dropped) or (forced and not res.text.strip())):
                log.info("refusal guard: model replied with a limitation and no tool (%r)", (res.text or "")[:120])
                out = self._refusal_override(prov, user_text, on_tool, on_text)
                if out is not None:
                    return out
                if not forced and pc_control_level(st) == "full":
                    forced = True  # the refusal is not kept in history: retry clean, with the forcing hint
                    continue
                reply = RG.CLARIFY["en" if str(self._lang).startswith("en") else "ru"]
                if on_text:
                    on_text(reply)
                self.history.append(prov.assistant_message(reply))
                return reply
            quiet = gate is not None and gate.dropped  # «не могу…» + a tool call: run the tool, don't say it
            if res.raw_message is not None:
                self.history.append(res.raw_message)
            if res.text and not quiet:
                said.append(res.text)
            if not res.tool_calls:
                return " ".join(said).strip() or self._ok_text()
            used_tool = True
            results = []
            for c in res.tool_calls:
                if on_tool:
                    try:
                        on_tool(c.name, c.args)
                    except Exception:
                        pass
                results.append(self.actions.execute(c.name, c.args))
            self.history.extend(prov.tool_result_messages(res.tool_calls, results))
            if prov.name == "ollama" and res.text and _nothing_to_add(res.tool_calls, results):
                # local models say «Открываю Telegram» together with the call and then repeat it after the
                # result; a plain «ok» adds nothing → skip the second round (no repeated phrase, ~0.5 s faster)
                return " ".join(said).strip() or (L.text("ok", self._lang) if quiet else "")
        return " ".join(said).strip() or L.text("did", self._lang)

    def _refusal_override(self, prov: P.Provider, user_text: str, on_tool, on_text) -> str | None:
        """Run the tool the model should have called; returns the short reply, or None when nothing maps."""
        if self.actions is None:
            return None
        st = self.get_settings()
        try:
            tg_ctx = "telegram" in (self.get_context() or "").lower()
        except Exception:
            tg_ctx = False
        call = RG.override_call(user_text, st, telegram_context=tg_ctx)
        if call is None:
            return None
        name, args = call
        log.info("refusal guard: forcing tool %s instead of the refusal", name)
        if on_tool:
            try:
                on_tool(name, args)
            except TurnCancelled:
                raise
            except Exception:
                pass
        try:
            result = self.actions.execute(name, args)
        except Exception as e:  # never let the safety net crash the turn
            log.exception("refusal guard: forced %s failed", name)
            result = {"ok": False, "error": str(e)[:200]}
        reply = RG.done_phrase(name, args, result, self._lang)
        failed = isinstance(result, dict) and (result.get("ok") is False or bool(result.get("error")))
        if on_text and failed:
            on_text(reply)  # errors are worth saying; success is covered by the ack / final reply
        self.history.append(prov.assistant_message(reply))
        return reply

    def _try_without_tools(self, prov, chat_m, lite_m, on_text, *, fast: bool) -> P.LLMResult | None:
        """Fast path for plain questions. Returns None if the model asked for tools ([[TOOLS]])."""
        buf: list[str] = []
        passing = [False]

        def gate(delta: str) -> None:
            if passing[0]:
                d = delta.replace(ESCAPE, "")
                if d:
                    on_text(d)
                return
            buf.append(delta)
            acc = "".join(buf).lstrip()
            if acc.startswith(ESCAPE):
                raise _NeedTools()
            if ESCAPE.startswith(acc):
                return  # could still become the escape token: hold it
            passing[0] = True
            on_text(acc.replace(ESCAPE, ""))

        t0 = time.monotonic()
        try:
            res = self._call(prov, chat_m, lite_m, gate, fast=fast, voice=True, tools=None, extra=NO_TOOLS_HINT)
        except _NeedTools:
            log.info("fast path: model wants tools (%.2fs) → retry with tools", time.monotonic() - t0)
            return None
        except P.ProviderError as e:
            if passing[0]:
                raise
            log.info("fast path failed (%s) → normal path", e)
            return None
        if res.text.strip().startswith(ESCAPE) or res.tool_calls:
            return None
        if not passing[0] and buf:
            on_text("".join(buf))
        log.info("fast path (no tools): answered in %.2fs", time.monotonic() - t0)
        return res

    def _trim(self) -> None:
        if len(self.history) <= MAX_HISTORY:
            return
        # cut at a plain user text message so tool call/response pairs stay intact
        cut = len(self.history) - MAX_HISTORY
        for i in range(cut, len(self.history)):
            if _is_plain_user(self.history[i]):
                self.history = self.history[i:]
                return
        self.history = self.history[-4:]

    # ── one-shot helpers ──
    def _one_shot(self, kind: str, fn):
        """Run fn(name, prov, chat_m, lite_m) over the route; local failures fall through to the cloud.
        Rate limits of the cloud propagate (callers back off)."""
        last: Exception | None = None
        names = self.route(kind)
        for i, name in enumerate(names):
            try:
                prov = self.provider(name)
            except P.NoKeyError as e:
                last = e
                continue
            chat_m, lite_m = self.models(name)
            try:
                out = fn(name, prov, chat_m, lite_m)
                self._note_local(name, True)
                return out
            except P.RateLimitError as e:
                e.provider = e.provider or name
                raise
            except P.ProviderError as e:
                e.provider = e.provider or name
                last = e
                self._note_local(name, False, str(e))
                if name == "ollama" and i < len(names) - 1:
                    log.info("%s: local model failed (%s) → %s", kind, e, names[i + 1])
                    continue
                if isinstance(e, (P.OverloadedError, P.ModelNotFoundError)):
                    continue
                raise
        raise last or P.ProviderError("Нет ответа")

    def describe_screen(self, jpeg: bytes, question: str) -> str:
        s = self.get_settings()
        prompt = (f"Это скриншот экрана пользователя. Вопрос: {question}\n"
                  f"{L.prompt_rule(s)} Ответь коротко (1–3 предложения), как для озвучки, обращаясь на «ты».")

        def run(name, prov, chat_m, lite_m):
            last: Exception | None = None
            for model in dict.fromkeys([lite_m, chat_m]):
                try:
                    return prov.generate(model, prompt, image_jpeg=jpeg, timeout=30).strip()
                except (P.RateLimitError, P.OverloadedError, P.ModelNotFoundError) as e:
                    last = e
            raise last or P.ProviderError("Нет ответа")
        return self._one_shot("describe", run)

    def classify_screen(self, jpeg: bytes, task: str, title: str = "", process: str = "") -> dict:
        """Ask the lite (or local) model whether the screen matches the stated task."""
        prompt = (
            f"Пользователь сказал, что сейчас занимается: «{task}».\n"
            f"Активное окно: «{title}» ({process}).\n"
            "Посмотри на скриншот и реши, занимается ли он этой задачей или чем-то связанным с ней "
            "(поиск информации, документы, учебные видео, нужные программы — это on_task). "
            "Off-task — это явные развлечения: короткие видео (TikTok, Shorts, Reels), ленты соцсетей, "
            "игры, стримы, мемы, видео не по теме.\n"
            "Верни JSON: on_task (bool), confidence (0..1 — уверенность в своём решении), "
            "activity (что он делает, 3–6 слов по-русски)."
        )

        def run(name, prov, chat_m, lite_m):
            raw = prov.generate(lite_m, prompt, image_jpeg=jpeg, json_schema=SCREEN_SCHEMA,
                                timeout=30, temperature=0.0, max_tokens=300)
            v = parse_screen_verdict(raw)
            if not v["valid"] and name == "ollama":
                raise P.OverloadedError("Локальная модель вернула не JSON", detail=str(raw)[:200])
            v["provider"] = name
            return v
        return self._one_shot("screen", run)

    def live_decide(self, jpeg: bytes, system: str, prompt: str) -> dict:
        """Live mode: one structured decision {speak, kind, text, reason, confidence, activity}.
        Local model first when routed (no quota), else lite model first (bigger free quota), chat model as a
        fallback on overload/unknown model. RateLimitError propagates so the caller can back off."""
        from .live import LIVE_SCHEMA, parse_decision

        def run(name, prov, chat_m, lite_m):
            last: Exception | None = None
            for model in dict.fromkeys([lite_m, chat_m]):
                try:
                    t0 = time.monotonic()
                    raw = prov.generate(model, prompt, image_jpeg=jpeg, json_schema=LIVE_SCHEMA, system=system,
                                        timeout=30 if name != "ollama" else 45, temperature=0.3, max_tokens=700)
                    d = parse_decision(raw)
                    if not d["valid"] and name == "ollama":
                        raise P.OverloadedError("Локальная модель вернула не JSON", detail=str(raw)[:200])
                    d["model"] = model
                    d["provider"] = name
                    log.info("live decision via %s/%s in %.1fs", name, model, time.monotonic() - t0)
                    return d
                except (P.OverloadedError, P.ModelNotFoundError) as e:
                    last = e
            raise last or P.ProviderError("Нет ответа")
        return self._one_shot("live", run)

    def note_remark(self, text: str) -> None:
        """Put a proactive remark into the chat history so a spoken reply has context."""
        with self.lock:
            u = "(Живой режим: ты сам посмотрел на мой экран и сказал мне следующее.)"
            self.transcript += [("user", u), ("assistant", text)]
            self.transcript = self.transcript[-MAX_HISTORY:]
            if self._hist_provider:
                try:
                    prov = self.provider(self._hist_provider)
                except P.ProviderError:
                    return
                self.history.append(prov.user_message(u))
                self.history.append(prov.assistant_message(text))
                self._trim()

    def test_connection(self, provider: str) -> str:
        prov = self.provider(provider)
        chat_m, _ = self.models(provider)
        t0 = time.monotonic()
        txt = prov.generate(chat_m, "Ответь одним словом: работает", timeout=120 if provider == "ollama" else 30,
                            max_tokens=50 if provider in ("ollama", "gemini") else 400)
        return f"{chat_m}: «{txt.strip()[:40]}» за {time.monotonic() - t0:.1f} с"


class _RefusalGate:
    """Holds the start of a streamed reply until it is clear it is not a «не могу / limitation» refusal.
    Sentence-based (TTS speaks whole sentences anyway), so a normal «Нажимаю плей.» passes almost at once."""

    MIN_DECIDE = 25   # a lone «Хорошо.» is not enough to decide — the refusal often comes next
    MAX_HOLD = 140

    def __init__(self, on_text) -> None:
        self.on_text = on_text
        self.buf: list[str] = []
        self.state = "hold"   # hold | pass | drop

    @property
    def dropped(self) -> bool:
        return self.state == "drop"

    def feed(self, delta: str) -> None:
        if self.state == "pass":
            self.on_text(delta)
            return
        self.buf.append(delta)
        if self.state == "drop":
            return
        acc = "".join(self.buf)
        size = len(acc.strip())
        if size >= self.MAX_HOLD or (size >= self.MIN_DECIDE and re.search(r"[.!?…\n]", acc)):
            self._decide(acc)

    def _decide(self, acc: str) -> None:
        if RG.is_refusal(acc):
            self.state = "drop"
        else:
            self.state = "pass"
            if acc:
                self.on_text(acc)

    def close(self) -> None:
        if self.state == "hold":
            self._decide("".join(self.buf))

    def flush(self) -> None:
        """Release dropped text (used when the model did call a tool after all)."""
        if self.state == "drop":
            acc = "".join(self.buf)
            self.state = "pass"
            if acc:
                self.on_text(acc)


INFO_TOOLS = {"find_files", "get_time", "look_at_screen", "focus_status", "list_reminders",
              "get_active_window", "recall_memory"}


def _nothing_to_add(calls, results) -> bool:
    """True when every tool just succeeded and returned nothing the model should talk about."""
    for c, r in zip(calls, results):
        if c.name in INFO_TOOLS:
            return False
        if not isinstance(r, dict) or r.get("ok") is False or r.get("error") or r.get("needs_confirm"):
            return False
    return True


def _is_plain_user(msg: object) -> bool:
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    if "parts" in msg:  # gemini
        return any("text" in p for p in msg.get("parts") or []) and not any(
            "functionResponse" in p for p in msg.get("parts") or [])
    return True


def parse_screen_verdict(raw: str) -> dict:
    data: dict = {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except ValueError:
                data = {}
    if not isinstance(data, dict):
        data = {}
    on = data.get("on_task")
    if isinstance(on, str):
        on = on.strip().lower() in ("true", "yes", "1", "да")
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    if conf > 1.0:
        conf = conf / 100.0
    return {"on_task": True if on is None else bool(on), "confidence": max(0.0, min(1.0, conf)),
            "activity": str(data.get("activity", ""))[:80], "valid": on is not None}


def context_line(snapshot: dict, fg_title: str = "") -> str:
    parts = []
    if snapshot.get("phase") in ("focus", "break"):
        rem = int(snapshot.get("remaining", 0)) // 60
        ph = "фокус" if snapshot["phase"] == "focus" else "перерыв"
        pause = ", на паузе" if not snapshot.get("running") else ""
        parts.append(f"Идёт фокус-сессия: задача «{snapshot.get('task')}», сейчас {ph}{pause}, "
                     f"осталось около {rem} мин, блок {snapshot.get('round')} из {snapshot.get('rounds')}.")
    else:
        parts.append("Фокус-сессия не запущена.")
    if fg_title:
        parts.append(f"Активное окно пользователя: «{fg_title[:120]}».")
    return " ".join(parts)


def now_hhmm() -> str:
    return datetime.now().strftime("%H:%M")
