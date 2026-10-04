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
from .actions import TOOLS, time_info
from . import persona
from .config import PROVIDERS, api_key_for

log = logging.getLogger("jarvis")

MAX_TOOL_ROUNDS = 5
MAX_HISTORY = 30

SYSTEM_PROMPT = """Ты — {name}, личный голосовой ИИ-ассистент {user_gen} на компьютере с Windows. Тебя зовут {name}, так и представляйся.
Твой характер: {character}
Обращайся на «ты». Держи этот характер в каждом ответе, но оставайся полезным.
Главная миссия: помогать {user_dat} реально делать то, что нужно, а не залипать в TikTok и шортсы. Возвращай к делу в своём стиле.

Правила ответа:
- Твои ответы озвучиваются голосом. Отвечай коротко и разговорно: обычно 1–2 предложения, максимум 3–4, если просят объяснить.
- Никакого markdown, списков, эмодзи и ссылок в тексте. Числа и время пиши так, как их удобно произносить.
- Отвечай по-русски, если не попросили иначе.
- Если нужно действие на компьютере — вызывай инструменты, а не описывай, как это сделать. После действия коротко подтверди.
- Когда пользователь говорит, чем сейчас занимается и сколько времени («делаю домашку по физике 40 минут») — сразу вызывай start_focus.
- Ты не умеешь удалять файлы, закрывать программы и отправлять сообщения. Если просят — честно скажи, что пока так не умеешь.
- Если не расслышал или запрос непонятен — переспроси одной короткой фразой.
- «Живой режим» — ты сам поглядываешь на экран и подсказываешь, когда это уместно. Включай/выключай его через set_live_mode: «следи и подсказывай», «включи живой режим» — включить; «выключи живой режим», а также «тихо», «хватит», «помолчи», «не мешай», если живой режим включён и речь о твоих подсказках, — выключить.
"""


def build_system_prompt(settings: dict) -> str:
    user = str(settings.get("user_name") or "").strip()
    name = persona.assistant_name(settings)
    head = SYSTEM_PROMPT.format(
        name=name, character=persona.character_text(settings),
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
              "(«Открываю ютуб.», «Ставлю напоминание.»), и сразу вызови инструмент. После успешного выполнения ничего "
              "не повторяй (ответь пустой строкой); говори, только если результат важен: нашёл файлы, ошибка, вопрос.\n")
NO_TOOLS_HINT = ("\nСейчас инструменты недоступны. Если для ответа нужно действие на компьютере (открыть программу, сайт "
                 "или файл, поиск в интернете, найти файл, напоминание или таймер, фокус-сессия, посмотреть на экран, "
                 "живой режим) — ответь ровно: [[TOOLS]]\n")
ESCAPE = "[[TOOLS]]"

# Voice questions that clearly need no PC action are answered without tool declarations: measured
# first token 0.48 s vs 0.70 s with tools (gemini-3.5-flash-lite). Anything that smells like an action
# goes straight to the tool path; if the model still wants a tool it answers [[TOOLS]] and we retry.
_ACTION_RE = re.compile(
    r"(откр|запус|включ|выключ|отключ|найд|найт|поищ|ищи|загугл|погугл|поставь|постав|напом|таймер|засек|будильн|"
    r"фокус|помодоро|сесси|пауз|останов|продолж|отмен|экран|посмотри|глянь|что у меня|живой|следи|подсказ|"
    r"тихо|хватит|помолчи|не меша|сайт|ютуб|youtube|браузер|файл|папк|документ|скача|музык|видео|телеграм|"
    r"дискорд|стим|steam|chrome|хром|спотиф|календар|делаю|занимаюсь|работаю|учу|домашк|готовлюсь|минут|\bчас|полчаса)",
    re.IGNORECASE)


def needs_tools(text: str) -> bool:
    t = (text or "").lower()
    if len(t.split()) <= 2:  # «да», «давай», «нет» — may confirm a pending action
        return True
    return bool(_ACTION_RE.search(t))


class _NeedTools(Exception):
    pass


class TurnCancelled(Exception):
    """The user interrupted (new question / stop) while the reply was streaming."""


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

    # ── provider plumbing ──
    def provider(self, name: str | None = None) -> P.Provider:
        s = self.get_settings()
        name = name or s["provider"]
        key = api_key_for(s, name)
        base = s["base_urls"].get(name) or PROVIDERS[name]["base_url"]
        if not key:
            raise P.NoKeyError("Нет API-ключа")
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
        return m.get("chat") or PROVIDERS[name]["chat"], m.get("lite") or PROVIDERS[name]["lite"]

    def reset(self) -> None:
        with self.lock:
            self.history = []

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
        return (build_system_prompt(st) + extra
                + f"\nСейчас: {ti['weekday']}, {ti['date']}, {ti['time']}.\n"
                + (ctx + "\n" if ctx else "") + (VOICE_HINT if voice else ""))

    # ── speech to text ──
    def transcribe(self, wav: bytes) -> str:
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
                    text = prov.transcribe(model, wav, timeout=30)
                    log.info("transcribed via %s/%s in %.1fs: %r", name, model,
                             time.monotonic() - t0, text[:120])
                    return text
                except (P.RateLimitError, P.OverloadedError, P.ModelNotFoundError) as e:
                    last = e
                    continue
        raise last or P.ProviderError("Не удалось распознать речь")

    # ── chat with tools ──
    def ask(self, text: str, on_tool=None, on_text=None, *, fast: bool = False, voice: bool = False) -> str:
        """One user turn. on_text(delta) streams the visible reply as it arrives (all tool rounds).
        fast=True → lite model first (voice turns with «Быстрые ответы»), chat model as fallback."""
        with self.lock:
            s = self.get_settings()
            pname = s["provider"]
            if self._hist_provider != pname:
                self.history = []
                self._hist_provider = pname
            prov = self.provider(pname)
            chat_m, lite_m = self.models(pname)
            self.history.append(prov.user_message(text))
            start_len = len(self.history) - 1
            route = (voice and on_text is not None and not s.get("confirm_actions") and not needs_tools(text))
            try:
                reply = self._loop(prov, chat_m, lite_m, on_tool, on_text, fast=fast, voice=voice, route=route)
            except Exception:
                del self.history[start_len:]  # keep history consistent
                raise
            self._trim()
            return reply

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

    def _call(self, prov: P.Provider, chat_m: str, lite_m: str, on_text=None, *, fast: bool = False,
              voice: bool = False, tools: list | None = TOOLS, extra: str = "") -> P.LLMResult:
        """Chat call with fallback between chat/lite models on overload / rate limit."""
        system = self._system(voice) + extra
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
                return res.text.replace(ESCAPE, "").strip() or "Готово."
        for _round in range(MAX_TOOL_ROUNDS):
            res = self._call(prov, chat_m, lite_m, on_text, fast=fast, voice=voice)
            if res.raw_message is not None:
                self.history.append(res.raw_message)
            if res.text:
                said.append(res.text)
            if not res.tool_calls:
                return " ".join(said).strip() or "Готово."
            results = []
            for c in res.tool_calls:
                if on_tool:
                    try:
                        on_tool(c.name, c.args)
                    except Exception:
                        pass
                results.append(self.actions.execute(c.name, c.args))
            self.history.extend(prov.tool_result_messages(res.tool_calls, results))
        return " ".join(said).strip() or "Сделал, что смог."

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
    def describe_screen(self, jpeg: bytes, question: str) -> str:
        s = self.get_settings()
        pname = s["provider"]
        prov = self.provider(pname)
        chat_m, lite_m = self.models(pname)
        prompt = (f"Это скриншот экрана пользователя. Вопрос: {question}\n"
                  "Ответь по-русски коротко (1–3 предложения), как для озвучки, обращаясь на «ты».")
        last: Exception | None = None
        for model in (lite_m, chat_m):
            try:
                return prov.generate(model, prompt, image_jpeg=jpeg, timeout=30).strip()
            except (P.RateLimitError, P.OverloadedError, P.ModelNotFoundError) as e:
                last = e
        raise last or P.ProviderError("Нет ответа")

    def classify_screen(self, jpeg: bytes, task: str, title: str = "", process: str = "") -> dict:
        """Ask the lite model whether the screen matches the stated task."""
        s = self.get_settings()
        pname = s["provider"]
        prov = self.provider(pname)
        _chat_m, lite_m = self.models(pname)
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
        raw = prov.generate(lite_m, prompt, image_jpeg=jpeg, json_schema=SCREEN_SCHEMA,
                            timeout=30, temperature=0.0, max_tokens=300)
        return parse_screen_verdict(raw)

    def live_decide(self, jpeg: bytes, system: str, prompt: str) -> dict:
        """Live mode: one structured decision {speak, kind, text, reason, confidence, activity}.
        Lite model first (bigger free quota), chat model as a fallback on overload/unknown model.
        RateLimitError propagates so the caller can back off."""
        from .live import LIVE_SCHEMA, parse_decision
        s = self.get_settings()
        pname = s["provider"]
        prov = self.provider(pname)
        chat_m, lite_m = self.models(pname)
        last: Exception | None = None
        for model in dict.fromkeys([lite_m, chat_m]):
            try:
                t0 = time.monotonic()
                raw = prov.generate(model, prompt, image_jpeg=jpeg, json_schema=LIVE_SCHEMA, system=system,
                                    timeout=30, temperature=0.3, max_tokens=700)
                d = parse_decision(raw)
                d["model"] = model
                log.info("live decision via %s in %.1fs", model, time.monotonic() - t0)
                return d
            except (P.OverloadedError, P.ModelNotFoundError) as e:
                last = e
        raise last or P.ProviderError("Нет ответа")

    def note_remark(self, text: str) -> None:
        """Put a proactive remark into the chat history so a spoken reply has context."""
        with self.lock:
            s = self.get_settings()
            pname = s["provider"]
            if self._hist_provider != pname:
                self.history = []
                self._hist_provider = pname
            try:
                prov = self.provider(pname)
            except P.ProviderError:
                return
            self.history.append(prov.user_message("(Живой режим: ты сам посмотрел на мой экран и сказал мне следующее.)"))
            if pname == "gemini":
                self.history.append({"role": "model", "parts": [{"text": text}]})
            else:
                self.history.append({"role": "assistant", "content": text})
            self._trim()

    def test_connection(self, provider: str) -> str:
        prov = self.provider(provider)
        chat_m, _ = self.models(provider)
        t0 = time.monotonic()
        txt = prov.generate(chat_m, "Ответь одним словом: работает", timeout=30, max_tokens=50)
        return f"{chat_m}: «{txt.strip()[:40]}» за {time.monotonic() - t0:.1f} с"


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
