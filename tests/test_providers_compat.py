"""v1.6: OpenAI-compatible providers (Hubris, custom): streaming tool calls, errors, model list, quirks."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from jarvis_app import providers as P
from jarvis_app.config import CLOUD_PROVIDERS, OPENAI_COMPAT, PROVIDERS, normalize_settings


class FakeResp:
    def __init__(self, status=200, body=None, headers=None, chunks=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}
        self._chunks = chunks or []
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None, stream=False, **kw):
        self.calls.append({"url": url, "headers": headers, "json": json, "stream": stream})
        return self.responses.pop(0)

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers})
        return self.responses.pop(0)


def sse(*objs):
    out = b""
    for o in objs:
        out += b"data: " + (o if isinstance(o, bytes) else json.dumps(o, ensure_ascii=False).encode()) + b"\n\n"
    return [out[i:i + 37] for i in range(0, len(out), 37)]  # split mid-line on purpose


def test_config_has_hubris_and_custom():
    assert "hubris" in CLOUD_PROVIDERS and "custom" in CLOUD_PROVIDERS
    assert {"openai", "xai", "hubris", "custom"} <= set(OPENAI_COMPAT)
    h = PROVIDERS["hubris"]
    assert h["base_url"] == "https://api.hubris.pw/v1"
    assert h["chat"] in h["chat_models"] and h["lite"] in h["lite_models"]
    assert all("/" in m for m in h["chat_models"] + h["lite_models"])
    s = normalize_settings({"provider": "hubris", "api_keys": {"hubris": " sk-gw-abc "}})
    assert s["provider"] == "hubris" and s["api_keys"]["hubris"] == "sk-gw-abc"
    assert s["models"]["hubris"]["chat"] == h["chat"]
    c = normalize_settings({"provider": "custom", "base_urls": {"custom": "https://openrouter.ai/api/v1/"},
                            "models": {"custom": {"chat": "openai/gpt-4o-mini"}}})
    assert c["base_urls"]["custom"] == "https://openrouter.ai/api/v1"
    assert c["models"]["custom"]["lite"] == "openai/gpt-4o-mini"  # empty lite → chat model
    assert normalize_settings({})["provider"] == "gemini"  # old settings keep their provider


def test_make_provider_compat_and_custom_needs_base():
    p = P.make_provider("hubris", "sk-gw-x", "https://api.hubris.pw/v1")
    assert isinstance(p, P.OpenAICompatProvider) and p.name == "hubris"
    with pytest.raises(P.ProviderError):
        P.make_provider("custom", "k", "")


def test_stream_text_and_tool_calls():
    p = P.OpenAICompatProvider("sk-gw-x", "https://api.hubris.pw/v1", name="hubris")
    chunks = sse(
        {"model": "google/gemini-3.8-flash", "choices": [{"delta": {"role": "assistant", "content": "Открываю "}}]},
        {"choices": [{"delta": {"content": "браузер."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                  "function": {"name": "open_app", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"name\": \"Chr"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ome\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10}},
        b"[DONE]")
    p.session = FakeSession([FakeResp(200, headers={"Content-Type": "text/event-stream"}, chunks=chunks)])
    got = []
    res = p.chat_stream("google/gemini-3.8-flash", "sys", [p.user_message("открой браузер")],
                        [{"name": "open_app", "description": "d", "parameters": {"type": "object"}}], on_text=got.append)
    assert "".join(got).strip() == "Открываю браузер."
    assert [(c.id, c.name, c.args) for c in res.tool_calls] == [("call_1", "open_app", {"name": "Chrome"})]
    assert res.raw_message["tool_calls"][0]["function"]["arguments"] == "{\"name\": \"Chrome\"}"
    body = p.session.calls[0]["json"]
    assert body["stream"] is True and body["tools"][0]["function"]["name"] == "open_app"
    assert body["reasoning_effort"] == "low"  # Hubris only
    assert p.session.calls[0]["headers"]["Authorization"] == "Bearer sk-gw-x"


def test_vision_message_is_data_url():
    p = P.OpenAICompatProvider("k", "https://x/v1", name="custom")
    m = p.user_message("что на экране?", b"\xff\xd8jpeg")
    assert m["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_402_and_401_errors_are_clear():
    with pytest.raises(P.PaymentError) as ei:
        P.raise_for_response(FakeResp(402, {"error": {"message": "Недостаточно средств", "code": "insufficient_funds"}}), "hubris")
    msg = P.friendly_error(ei.value)
    assert "Hubris" in msg and "баланс" in msg.lower()
    with pytest.raises(P.AuthError) as ei:
        P.raise_for_response(FakeResp(401, {"error": {"message": "bad key"}}), "hubris")
    assert "Ключ Hubris" in P.friendly_error(ei.value)
    with pytest.raises(P.RateLimitError) as ei:
        P.raise_for_response(FakeResp(429, {"error": {"message": "daily limit"}}), "hubris")
    assert "Hubris" in P.friendly_error(ei.value)
    e = P.NoKeyError("Нет API-ключа", provider="hubris")
    assert "Hubris" in P.friendly_error(e)


def test_stream_error_event_with_string_code():
    p = P.OpenAICompatProvider("k", "https://api.hubris.pw/v1", name="hubris")
    chunks = sse({"error": {"code": "insufficient_funds", "message": "balance too low"}})
    p.session = FakeSession([FakeResp(200, headers={"Content-Type": "text/event-stream"}, chunks=chunks)])
    with pytest.raises(P.PaymentError) as ei:
        p.chat_stream("m", "", [], on_text=lambda d: None)
    assert ei.value.provider == "hubris"


def test_quirk_retry_temperature_and_max_tokens():
    p = P.OpenAICompatProvider("k", "https://api.openai.com/v1", name="openai")
    ok = {"model": "gpt-5-mini", "choices": [{"message": {"content": "работает"}}]}
    p.session = FakeSession([
        FakeResp(400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.2"}}),
        FakeResp(400, {"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."}}),
        FakeResp(200, ok)])
    assert p.generate("gpt-5-mini", "hi") == "работает"
    last = p.session.calls[-1]["json"]
    assert "temperature" not in last and last["max_completion_tokens"] == 1024 and "max_tokens" not in last
    p.session = FakeSession([FakeResp(200, ok)])
    p.generate("gpt-5-mini", "again")  # remembered per model: one request
    assert len(p.session.calls) == 1 and "temperature" not in p.session.calls[0]["json"]


def test_empty_model_is_a_clear_error():
    p = P.OpenAICompatProvider("k", "https://x/v1", name="custom")
    with pytest.raises(P.ModelNotFoundError) as ei:
        p.chat("", "", [])
    assert "модель" in P.friendly_error(ei.value).lower()


def test_parse_model_list_hubris_shape():
    data = {"object": "list", "data": [
        {"id": "anthropic/claude-haiku-4.5", "input_modalities": ["text", "image"], "output_modalities": ["text"],
         "supported_parameters": ["tools", "temperature"], "pricing": {"input_rub_per_million": 126.24, "output_rub_per_million": 631.19, "is_free": False}},
        {"id": "google/lyria-3-pro-preview", "input_modalities": ["text"], "output_modalities": ["audio"]},
        {"id": "meta-llama/llama-3.2-1b-instruct", "input_modalities": ["text"], "output_modalities": ["text"],
         "supported_parameters": ["temperature"], "pricing": {"input_rub_per_million": 3.41, "output_rub_per_million": 25.38}},
        {"id": "hubris/free", "input_modalities": ["text", "image"], "output_modalities": ["text"],
         "supported_parameters": ["tools"], "pricing": {"input_rub_per_million": 0, "output_rub_per_million": 0, "is_free": True}},
    ]}
    out = P.parse_model_list(data)
    ids = [m["id"] for m in out]
    assert "google/lyria-3-pro-preview" not in ids
    assert ids[-1] == "meta-llama/llama-3.2-1b-instruct"  # no tools → last
    haiku = out[ids.index("anthropic/claude-haiku-4.5")]
    assert "126/631 ₽" in haiku["label"] and "инструменты" in haiku["label"] and haiku["vision"]
    assert out[ids.index("hubris/free")]["free"]
    assert [m["id"] for m in P.parse_model_list({"data": [{"id": "gpt-4o"}]})] == ["gpt-4o"]  # plain OpenAI shape


def test_list_models_uses_key():
    p = P.OpenAICompatProvider("sk-gw-k", "https://api.hubris.pw/v1", name="hubris")
    p.session = FakeSession([FakeResp(200, {"data": [{"id": "openai/gpt-5.4-mini"}]})])
    assert p.list_models()[0]["id"] == "openai/gpt-5.4-mini"
    assert p.session.calls[0]["url"] == "https://api.hubris.pw/v1/models"
