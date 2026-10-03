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
from .config import PROVIDERS, api_key_for

log = logging.getLogger("jarvis")

MAX_TOOL_ROUNDS = 5
MAX_HISTORY = 30

SYSTEM_PROMPT = """Ты — Джарвис, личный голосовой ИИ-ассистент Вовы на его компьютере с Windows.
Характер: спокойный, остроумный, заботливый, как Джарвис из «Железного человека», но без пафоса. Обращайся на «ты».
Главная миссия: помогать Вове реально делать то, что ему нужно, а не залипать в TikTok и шортсы. Мягко, но настойчиво возвращай к делу.

Правила ответа:
- Твои ответы озвучиваются голосом. Отвечай коротко и разговорно: обычно 1–2 предложения, максимум 3–4, если просят объяснить.
- Никакого markdown, списков, эмодзи и ссылок в тексте. Числа и время пиши так, как их удобно произносить.
- Отвечай по-русски, если Вова не попросил иначе.
- Если нужно действие на компьютере — вызывай инструменты, а не описывай, как это сделать. После действия коротко подтверди.
- Когда Вова говорит, чем сейчас занимается и сколько времени («делаю домашку по физике 40 минут») — сразу вызывай start_focus.
- Ты не умеешь удалять файлы, закрывать программы и отправлять сообщения. Если просят — честно скажи, что пока так не умеешь.
- Если не расслышал или запрос непонятен — переспроси одной короткой фразой.
"""

SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "on_task": {"type": "boolean"},
        "confidence": {"type": "number"},
        "activity": {"type": "string"},
    },
    "required": ["on_task", "confidence", "activity"],
}


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

    def _system(self) -> str:
        ti = time_info()
        ctx = self.get_context() or ""
        st = self.get_settings()
        extra = ""
        persona = str(st.get("persona") or "").strip()
        if persona:
            extra += f"\nДополнительные пожелания Вовы к твоему поведению (соблюдай их):\n{persona[:2000]}\n"
        if st.get("confirm_actions"):
            extra += ("\nРежим подтверждения включён: прежде чем открыть программу, файл или сайт, коротко спроси "
                      "«Открыть …?» и вызывай инструмент только после явного «да».\n")
        return (SYSTEM_PROMPT + extra
                + f"\nСейчас: {ti['weekday']}, {ti['date']}, {ti['time']}.\n"
                + (ctx + "\n" if ctx else ""))

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
    def ask(self, text: str, on_tool=None) -> str:
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
            try:
                reply = self._loop(prov, chat_m, lite_m, on_tool)
            except Exception:
                del self.history[start_len:]  # keep history consistent
                raise
            self._trim()
            return reply

    def _call(self, prov: P.Provider, chat_m: str, lite_m: str) -> P.LLMResult:
        """Chat call with fallback to the lite model on overload / rate limit."""
        system = self._system()
        models = [chat_m] if time.monotonic() >= self.cooldown_until else []
        if lite_m and lite_m != chat_m:
            models.append(lite_m)
        if not models:
            models = [chat_m]
        last: Exception | None = None
        for i, model in enumerate(models):
            try:
                t0 = time.monotonic()
                res = prov.chat(model, system, self.history, TOOLS,
                                timeout=12 if (i == 0 and len(models) > 1) else 30)
                log.info("chat %s %.1fs tools=%s", res.model or model, time.monotonic() - t0,
                         [c.name for c in res.tool_calls])
                return res
            except P.RateLimitError as e:
                last = e
                if model == chat_m:
                    self.cooldown_until = time.monotonic() + min(300.0, e.retry_after or 60.0)
                if e.retry_after and e.retry_after <= 6 and i == len(models) - 1:
                    time.sleep(e.retry_after + 0.3)
                    return prov.chat(model, system, self.history, TOOLS, timeout=30)
                continue
            except (P.OverloadedError, P.ModelNotFoundError) as e:
                last = e
                continue
        raise last or P.ProviderError("Нет ответа")

    def _loop(self, prov: P.Provider, chat_m: str, lite_m: str, on_tool) -> str:
        for _round in range(MAX_TOOL_ROUNDS):
            res = self._call(prov, chat_m, lite_m)
            if res.raw_message is not None:
                self.history.append(res.raw_message)
            if not res.tool_calls:
                return res.text or "Готово."
            results = []
            for c in res.tool_calls:
                if on_tool:
                    try:
                        on_tool(c.name, c.args)
                    except Exception:
                        pass
                results.append(self.actions.execute(c.name, c.args))
            self.history.extend(prov.tool_result_messages(res.tool_calls, results))
        return "Сделал, что смог."

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
