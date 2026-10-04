"""LLM provider layer: Gemini (REST) and OpenAI-compatible (OpenAI, xAI Grok).

Each provider keeps conversation history in its own native message format;
the Brain only uses the methods below, so swapping providers is a setting.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterator

import requests

log = logging.getLogger("jarvis")


# ─── Errors ──────────────────────────────────────────────────────────────────

class ProviderError(Exception):
    """Generic provider failure with a short Russian user-facing message."""

    def __init__(self, message: str, *, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class RateLimitError(ProviderError):
    def __init__(self, message: str, *, retry_after: float | None = None, daily: bool = False,
                 status: int | None = 429, detail: str = "") -> None:
        super().__init__(message, status=status, detail=detail)
        self.retry_after = retry_after
        self.daily = daily


class OverloadedError(ProviderError):
    pass


class AuthError(ProviderError):
    pass


class ModelNotFoundError(ProviderError):
    pass


class NoKeyError(ProviderError):
    pass


# ─── Data ────────────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: object = None  # provider-native assistant message for history
    model: str = ""


def _parse_duration(s: str | None) -> float | None:
    if not s:
        return None
    m = re.match(r"^\s*([\d.]+)\s*s?\s*$", str(s))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def parse_retry_after(headers: dict | None, body: object) -> float | None:
    """Retry delay from a Retry-After header or Gemini RetryInfo detail."""
    if headers:
        for k, v in headers.items():
            if k.lower() == "retry-after":
                d = _parse_duration(v)
                if d is not None:
                    return d
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else None
        for det in (err or {}).get("details", []) or []:
            if isinstance(det, dict) and "retryDelay" in det:
                d = _parse_duration(det.get("retryDelay"))
                if d is not None:
                    return d
        msg = str((err or {}).get("message", ""))
        m = re.search(r"retry in ([\d.]+)\s*s", msg, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _is_daily_quota(body: object) -> bool:
    text = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
    return bool(re.search(r"PerDay|per day|RequestsPerDay|daily", text, re.IGNORECASE))


def raise_for_response(resp: requests.Response, provider: str) -> None:
    if resp.status_code < 400:
        return
    try:
        body: object = resp.json()
    except ValueError:
        body = resp.text[:500]
    msg = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message", ""))
        elif isinstance(err, str):
            msg = err
    detail = (msg or str(body))[:400]
    st = resp.status_code
    log.warning("%s HTTP %s: %s", provider, st, detail[:300])
    if st == 429:
        daily = _is_daily_quota(body)
        ra = parse_retry_after(dict(resp.headers), body)
        raise RateLimitError("Лимит запросов", retry_after=ra, daily=daily, detail=detail)
    if st in (401, 403):
        raise AuthError("Ключ API не подходит", status=st, detail=detail)
    if st == 404:
        raise ModelNotFoundError("Модель не найдена", status=st, detail=detail)
    if st in (500, 502, 503, 504):
        raise OverloadedError("Сервис перегружен", status=st, detail=detail)
    raise ProviderError("Ошибка запроса", status=st, detail=detail)


def _post(session: requests.Session, url: str, *, headers: dict, payload: dict,
          timeout: float, provider: str) -> dict:
    try:
        resp = session.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.Timeout as e:
        raise OverloadedError("Сервис не ответил вовремя", detail=str(e)) from e
    except requests.RequestException as e:
        raise ProviderError("Нет связи с сервисом", detail=str(e)[:300]) from e
    raise_for_response(resp, provider)
    try:
        return resp.json()
    except ValueError as e:
        raise ProviderError("Некорректный ответ сервиса", detail=resp.text[:300]) from e


# ─── Base ────────────────────────────────────────────────────────────────────

class Provider:
    name = "base"
    supports_audio = False
    supports_vision = True

    def __init__(self, api_key: str, base_url: str) -> None:
        if not api_key:
            raise NoKeyError("Нет API-ключа")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    # history helpers (native format)
    def user_message(self, text: str, image_jpeg: bytes | None = None) -> object:
        raise NotImplementedError

    def tool_result_messages(self, calls: list[ToolCall], results: list[object]) -> list[object]:
        raise NotImplementedError

    def chat(self, model: str, system: str, messages: list, tools: list[dict] | None = None,
             *, timeout: float = 40.0, temperature: float = 0.7) -> LLMResult:
        raise NotImplementedError

    def chat_stream(self, model: str, system: str, messages: list, tools: list[dict] | None = None,
                    *, on_text, timeout: float = 40.0, temperature: float = 0.7,
                    max_tokens: int = 2048) -> LLMResult:
        """Like chat(), but on_text(delta) is called as text arrives. Default: no streaming."""
        res = self.chat(model, system, messages, tools, timeout=timeout, temperature=temperature)
        if res.text:
            on_text(res.text)
        return res

    def generate(self, model: str, prompt: str, *, image_jpeg: bytes | None = None,
                 audio_wav: bytes | None = None, json_schema: dict | None = None,
                 system: str | None = None, timeout: float = 40.0,
                 temperature: float = 0.2, max_tokens: int = 1024) -> str:
        raise NotImplementedError

    def transcribe(self, model: str, wav: bytes, *, timeout: float = 40.0, lang: str = "ru") -> str:
        raise ProviderError("Распознавание речи не поддерживается этим провайдером")


# ─── Gemini ──────────────────────────────────────────────────────────────────

class GeminiProvider(Provider):
    name = "gemini"
    supports_audio = True

    def _url(self, model: str) -> str:
        model = model.strip()
        if model.startswith("models/"):
            model = model[len("models/"):]
        return f"{self.base_url}/models/{model}:generateContent"

    def _headers(self) -> dict:
        # key in a header, never in the URL (keeps it out of logs/proxies)
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def user_message(self, text: str, image_jpeg: bytes | None = None) -> dict:
        parts: list[dict] = []
        if image_jpeg:
            parts.append({"inlineData": {"mimeType": "image/jpeg",
                                         "data": base64.b64encode(image_jpeg).decode()}})
        parts.append({"text": text})
        return {"role": "user", "parts": parts}

    def tool_result_messages(self, calls: list[ToolCall], results: list[object]) -> list[dict]:
        parts = []
        for c, r in zip(calls, results):
            fr: dict = {"name": c.name, "response": {"result": r}}
            if c.id and not c.id.startswith("local-"):
                fr["id"] = c.id
            parts.append({"functionResponse": fr})
        return [{"role": "user", "parts": parts}]

    @staticmethod
    def _tools(tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return None
        return [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
            for t in tools
        ]}]

    @staticmethod
    def _parse(data: dict, model: str) -> LLMResult:
        cands = data.get("candidates") or []
        if not cands:
            fb = data.get("promptFeedback") or {}
            raise ProviderError("Пустой ответ модели", detail=json.dumps(fb)[:300])
        content = cands[0].get("content") or {}
        parts = content.get("parts") or []
        texts, calls = [], []
        for i, p in enumerate(parts):
            if "functionCall" in p:
                fc = p["functionCall"] or {}
                calls.append(ToolCall(id=str(fc.get("id") or f"local-{i}"),
                                      name=str(fc.get("name", "")),
                                      args=dict(fc.get("args") or {})))
            elif "text" in p and not p.get("thought"):
                texts.append(p["text"])
        raw = {"role": "model", "parts": parts} if parts else None
        return LLMResult(text="".join(texts).strip(), tool_calls=calls, raw_message=raw,
                         model=str(data.get("modelVersion") or model))

    def chat(self, model, system, messages, tools=None, *, timeout=40.0, temperature=0.7):
        payload: dict = {
            "contents": messages,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 2048},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        t = self._tools(tools)
        if t:
            payload["tools"] = t
        data = _post(self.session, self._url(model), headers=self._headers(), payload=payload,
                     timeout=timeout, provider="gemini")
        return self._parse(data, model)

    def chat_stream(self, model, system, messages, tools=None, *, on_text, timeout=40.0, temperature=0.7,
                    max_tokens=2048):
        """streamGenerateContent (SSE): text deltas go to on_text immediately; tool calls and the
        full model message (with thought signatures) are returned like chat()."""
        payload: dict = {
            "contents": messages,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        t = self._tools(tools)
        if t:
            payload["tools"] = t
        url = self._url(model).replace(":generateContent", ":streamGenerateContent") + "?alt=sse"
        try:
            resp = self.session.post(url, headers=self._headers(), json=payload, timeout=(6.0, timeout), stream=True)
        except requests.Timeout as e:
            raise OverloadedError("Сервис не ответил вовремя", detail=str(e)) from e
        except requests.RequestException as e:
            raise ProviderError("Нет связи с сервисом", detail=str(e)[:300]) from e
        with resp:
            raise_for_response(resp, "gemini")
            acc = StreamAccumulator(model)
            try:
                for data in iter_sse(resp):
                    for delta in acc.add(data):
                        on_text(delta)
            except requests.RequestException as e:
                if not acc.got_any:
                    raise OverloadedError("Обрыв связи с сервисом", detail=str(e)[:300]) from e
                raise ProviderError("Обрыв связи с сервисом", detail=str(e)[:300]) from e
        return acc.result()

    def generate(self, model, prompt, *, image_jpeg=None, audio_wav=None, json_schema=None,
                 system=None, timeout=40.0, temperature=0.2, max_tokens=1024):
        parts: list[dict] = []
        if audio_wav:
            parts.append({"inlineData": {"mimeType": "audio/wav",
                                         "data": base64.b64encode(audio_wav).decode()}})
        if image_jpeg:
            parts.append({"inlineData": {"mimeType": "image/jpeg",
                                         "data": base64.b64encode(image_jpeg).decode()}})
        parts.append({"text": prompt})
        gen: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_schema:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = json_schema
        payload: dict = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = _post(self.session, self._url(model), headers=self._headers(), payload=payload,
                     timeout=timeout, provider="gemini")
        return self._parse(data, model).text

    def transcribe(self, model, wav, *, timeout=40.0, lang="ru"):
        from .lang import LANGS
        usual = LANGS.get(lang, LANGS["ru"])["name_gen"]
        prompt = (
            f"Дословно расшифруй речь в этом аудио на языке оригинала (обычно {usual}). "
            "Выведи только текст, без кавычек и пояснений. "
            "Если речи нет или она неразборчива, выведи ровно: <пусто>"
        )
        text = self.generate(model, prompt, audio_wav=wav, timeout=timeout,
                             temperature=0.0, max_tokens=800)
        return clean_transcript(text)


def iter_sse(resp) -> Iterator[dict]:
    """Parse a Server-Sent Events body into JSON objects. Splits on raw b"\n" (str.splitlines would
    also split on U+2028 etc. inside Russian text)."""
    buf = b""
    data_lines: list[bytes] = []
    for chunk in resp.iter_content(chunk_size=None):
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if data_lines:
                    yield from _sse_event(data_lines)
                    data_lines = []
                continue
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
    if buf.strip().startswith(b"data:"):
        data_lines.append(buf.strip()[5:].lstrip())
    if data_lines:
        yield from _sse_event(data_lines)


def _sse_event(lines: list[bytes]):
    raw = b"\n".join(lines).decode("utf-8", "replace").strip()
    if not raw or raw == "[DONE]":
        return
    try:
        obj = json.loads(raw)
    except ValueError:
        log.debug("bad SSE chunk: %r", raw[:200])
        return
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = obj["error"]
        code = int(err.get("code") or 0)
        msg = str(err.get("message", ""))[:400]
        if code == 429:
            raise RateLimitError("Лимит запросов", retry_after=parse_retry_after(None, obj),
                                 daily=_is_daily_quota(obj), detail=msg)
        if code in (500, 502, 503, 504):
            raise OverloadedError("Сервис перегружен", status=code, detail=msg)
        raise ProviderError("Ошибка запроса", status=code or None, detail=msg)
    yield obj


class StreamAccumulator:
    """Collects Gemini stream chunks into one LLMResult; add() returns new visible text deltas."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.parts: list[dict] = []
        self.texts: list[str] = []
        self.calls: list[ToolCall] = []
        self.got_any = False
        self.model_version = ""
        self.blocked = ""

    def add(self, data: dict) -> list[str]:
        out: list[str] = []
        self.model_version = str(data.get("modelVersion") or self.model_version)
        cands = data.get("candidates") or []
        if not cands:
            fb = data.get("promptFeedback") or {}
            if fb.get("blockReason"):
                self.blocked = json.dumps(fb)[:300]
            return out
        for p in (cands[0].get("content") or {}).get("parts") or []:
            if not isinstance(p, dict):
                continue
            self.got_any = True
            if "functionCall" in p:
                fc = p["functionCall"] or {}
                self.calls.append(ToolCall(id=str(fc.get("id") or f"local-{len(self.parts)}"),
                                           name=str(fc.get("name", "")), args=dict(fc.get("args") or {})))
                self.parts.append(p)
                if self.texts and len(self.calls) == 1:
                    out.append("\n")  # text before a tool call is complete: let the caller speak it now
            elif p.get("thought"):
                continue
            elif "text" in p:
                txt = p.get("text") or ""
                if txt:
                    self.texts.append(txt)
                    out.append(txt)
                # merge consecutive text parts; keep a thought signature if one arrives
                if self.parts and "text" in self.parts[-1] and "functionCall" not in self.parts[-1]:
                    last = self.parts[-1]
                    last["text"] = (last.get("text") or "") + txt
                    if p.get("thoughtSignature") and not last.get("thoughtSignature"):
                        last["thoughtSignature"] = p["thoughtSignature"]
                else:
                    self.parts.append(dict(p))
            elif p.get("thoughtSignature"):
                self.parts.append(p)
        return out

    def result(self) -> LLMResult:
        if not self.got_any:
            raise ProviderError("Пустой ответ модели", detail=self.blocked)
        parts = [p for p in self.parts if not ("text" in p and not p.get("text") and not p.get("thoughtSignature"))]
        raw = {"role": "model", "parts": parts} if parts else None
        return LLMResult(text="".join(self.texts).strip(), tool_calls=self.calls, raw_message=raw,
                         model=self.model_version or self.model)


# ─── OpenAI-compatible (OpenAI, xAI) ─────────────────────────────────────────

class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, api_key: str, base_url: str, name: str = "openai") -> None:
        super().__init__(api_key, base_url)
        self.name = name
        self.supports_audio = name == "openai"  # Whisper-style transcription endpoint

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def user_message(self, text: str, image_jpeg: bytes | None = None) -> dict:
        if image_jpeg:
            url = "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode()
            return {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": url}},
            ]}
        return {"role": "user", "content": text}

    def tool_result_messages(self, calls, results):
        return [{"role": "tool", "tool_call_id": c.id,
                 "content": json.dumps(r, ensure_ascii=False)} for c, r in zip(calls, results)]

    @staticmethod
    def _tools(tools):
        if not tools:
            return None
        return [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools]

    def _complete(self, payload: dict, timeout: float) -> dict:
        return _post(self.session, f"{self.base_url}/chat/completions", headers=self._headers(),
                     payload=payload, timeout=timeout, provider=self.name)

    def chat(self, model, system, messages, tools=None, *, timeout=40.0, temperature=0.7):
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        payload: dict = {"model": model, "messages": msgs, "temperature": temperature}
        t = self._tools(tools)
        if t:
            payload["tools"] = t
        data = self._complete(payload, timeout)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(id=str(tc.get("id", "")), name=str(fn.get("name", "")),
                                  args=args if isinstance(args, dict) else {}))
        raw = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            raw["tool_calls"] = msg["tool_calls"]
        return LLMResult(text=(msg.get("content") or "").strip(), tool_calls=calls,
                         raw_message=raw, model=str(data.get("model") or model))

    def generate(self, model, prompt, *, image_jpeg=None, audio_wav=None, json_schema=None,
                 system=None, timeout=40.0, temperature=0.2, max_tokens=1024):
        if audio_wav:
            raise ProviderError("Аудио не поддерживается этим провайдером")
        msgs = ([{"role": "system", "content": system}] if system else [])
        msgs.append(self.user_message(prompt, image_jpeg))
        payload: dict = {"model": model, "messages": msgs, "temperature": temperature,
                         "max_tokens": max_tokens}
        if json_schema:
            payload["response_format"] = {"type": "json_object"}
        data = self._complete(payload, timeout)
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""

    def transcribe(self, model, wav, *, timeout=40.0, lang="ru"):
        if self.name != "openai":
            raise ProviderError("Распознавание речи не поддерживается этим провайдером")
        try:
            resp = self.session.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": ("speech.wav", wav, "audio/wav")},
                data={"model": "whisper-1", "language": lang if lang in ("ru", "uk", "en", "de", "pl") else "ru"},
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise ProviderError("Нет связи с сервисом", detail=str(e)[:300]) from e
        raise_for_response(resp, self.name)
        return clean_transcript((resp.json() or {}).get("text", ""))


def clean_transcript(text: str) -> str:
    t = (text or "").strip().strip('"«»').strip()
    if not t or "<пусто>" in t.lower() or t.lower() in ("пусто", "<empty>", "(тишина)"):
        return ""
    if re.fullmatch(r"[\d\s:.,\-–—]+", t):  # timestamps / digits only = hallucinated silence
        return ""
    return t


def make_provider(name: str, api_key: str, base_url: str) -> Provider:
    if name == "gemini":
        return GeminiProvider(api_key, base_url)
    if name in ("openai", "xai"):
        return OpenAICompatProvider(api_key, base_url, name=name)
    raise ProviderError(f"Неизвестный провайдер: {name}")


def friendly_error(e: Exception) -> str:
    """Short spoken-style Russian explanation for the user."""
    if isinstance(e, NoKeyError):
        return "Нет API-ключа. Добавь его в настройках."
    if isinstance(e, RateLimitError):
        if e.daily:
            return "Дневной лимит бесплатного API исчерпан. Попробуй завтра или смени модель в настройках."
        if e.retry_after:
            return f"Упёрся в лимит бесплатного API. Подожди секунд {int(e.retry_after) + 1}."
        return "Упёрся в лимит бесплатного API. Подожди минутку."
    if isinstance(e, AuthError):
        return "Ключ API не подходит. Проверь его в настройках."
    if isinstance(e, ModelNotFoundError):
        return "Такой модели нет. Выбери другую в настройках."
    if isinstance(e, OverloadedError):
        return "Сервис сейчас перегружен. Попробуй ещё раз чуть позже."
    if isinstance(e, ProviderError):
        return f"Не получилось: {e}."
    return "Что-то пошло не так. Подробности в журнале."


def now_ms() -> int:
    return int(time.time() * 1000)
