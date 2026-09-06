"""Unit tests for the unified structured-output entry of LLMClient.

All tests run fully offline: the OpenAI client is replaced with fakes and
``chat_with_messages`` is monkeypatched, so no real network calls happen.
"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from mr_data.config import settings
from mr_data.llm import LLMClient
from mr_data.llm.client import _extract_json


class _SimpleResult(BaseModel):
    value: int


def _make_client() -> LLMClient:
    return LLMClient(base_url="http://fake", api_key="fake", model="fake")


class _FakeParseCompletions:
    """Fake ``beta.chat.completions`` endpoint with a call counter."""

    def __init__(self, error: Exception | None = None, content: str = "{}"):
        self.error = error
        self.content = content
        self.parse_calls = 0

    def parse(self, **kwargs):
        self.parse_calls += 1
        if self.error is not None:
            raise self.error
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _inject_parse_api(client: LLMClient, completions: _FakeParseCompletions) -> None:
    client._client = SimpleNamespace(
        beta=SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert _extract_json('{"value": 1}') == {"value": 1}


def test_extract_json_markdown_fence():
    text = '```json\n{"value": 2}\n```'
    assert _extract_json(text) == {"value": 2}


def test_extract_json_surrounding_text():
    text = '好的，这是结果：\n{"value": 3}\n希望对你有帮助。'
    assert _extract_json(text) == {"value": 3}


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        _extract_json("完全不是 JSON 的回复")


# ---------------------------------------------------------------------------
# llm_structured_mode = "prompt"
# ---------------------------------------------------------------------------


def test_prompt_mode_skips_parse(monkeypatch):
    monkeypatch.setattr(settings, "llm_structured_mode", "prompt")
    client = _make_client()
    completions = _FakeParseCompletions(content='{"value": 99}')
    _inject_parse_api(client, completions)

    captured = {}

    def fake_chat_with_messages(messages, temperature=0.7):
        captured["messages"] = messages
        return '```json\n{"value": 42}\n```'

    monkeypatch.setattr(client, "chat_with_messages", fake_chat_with_messages)

    result = client.structured_chat([{"role": "user", "content": "给个数字"}], _SimpleResult)

    assert result == {"value": 42}
    assert completions.parse_calls == 0
    # The JSON schema instruction must be injected as a leading system message.
    assert captured["messages"][0]["role"] == "system"
    assert "JSON Schema" in captured["messages"][0]["content"]


def test_prompt_mode_merges_into_existing_system_message(monkeypatch):
    monkeypatch.setattr(settings, "llm_structured_mode", "prompt")
    client = _make_client()

    captured = {}

    def fake_chat_with_messages(messages, temperature=0.7):
        captured["messages"] = messages
        return '{"value": 5}'

    monkeypatch.setattr(client, "chat_with_messages", fake_chat_with_messages)

    messages = [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "给个数字"},
    ]
    result = client.structured_chat(messages, _SimpleResult)

    assert result == {"value": 5}
    assert len(captured["messages"]) == 2
    assert "你是助手。" in captured["messages"][0]["content"]
    assert "JSON Schema" in captured["messages"][0]["content"]


# ---------------------------------------------------------------------------
# llm_structured_mode = "auto"
# ---------------------------------------------------------------------------


def test_auto_mode_falls_back_and_caches(monkeypatch):
    monkeypatch.setattr(settings, "llm_structured_mode", "auto")
    client = _make_client()
    completions = _FakeParseCompletions(error=RuntimeError("parse not supported"))
    _inject_parse_api(client, completions)

    monkeypatch.setattr(
        client, "chat_with_messages", lambda messages, temperature=0.7: '{"value": 7}'
    )

    assert client._parse_supported is True
    result = client.structured_chat([{"role": "user", "content": "x"}], _SimpleResult)
    assert result == {"value": 7}
    assert client._parse_supported is False
    assert completions.parse_calls == 1

    # Second call must skip the parse endpoint entirely.
    result = client.structured_chat([{"role": "user", "content": "x"}], _SimpleResult)
    assert result == {"value": 7}
    assert completions.parse_calls == 1


def test_auto_mode_parse_success(monkeypatch):
    monkeypatch.setattr(settings, "llm_structured_mode", "auto")
    client = _make_client()
    completions = _FakeParseCompletions(content='{"value": 11}')
    _inject_parse_api(client, completions)

    result = client.structured_chat([{"role": "user", "content": "x"}], _SimpleResult)

    assert result == {"value": 11}
    assert client._parse_supported is True
    assert completions.parse_calls == 1


# ---------------------------------------------------------------------------
# llm_structured_mode = "parse"
# ---------------------------------------------------------------------------


def test_parse_mode_raises_on_error(monkeypatch):
    monkeypatch.setattr(settings, "llm_structured_mode", "parse")
    client = _make_client()
    completions = _FakeParseCompletions(error=RuntimeError("parse not supported"))
    _inject_parse_api(client, completions)

    monkeypatch.setattr(
        client, "chat_with_messages", lambda messages, temperature=0.7: '{"value": 1}'
    )

    with pytest.raises(RuntimeError, match="parse not supported"):
        client.structured_chat([{"role": "user", "content": "x"}], _SimpleResult)
    # No fallback caching in parse mode.
    assert client._parse_supported is True


# ---------------------------------------------------------------------------
# chat_structured sugar
# ---------------------------------------------------------------------------


def test_chat_structured_builds_messages(monkeypatch):
    monkeypatch.setattr(settings, "llm_structured_mode", "prompt")
    client = _make_client()

    captured = {}

    def fake_chat_with_messages(messages, temperature=0.7):
        captured["messages"] = messages
        return '{"value": 3}'

    monkeypatch.setattr(client, "chat_with_messages", fake_chat_with_messages)

    result = client.chat_structured("系统提示", "用户输入", _SimpleResult)

    assert result == {"value": 3}
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user"]
    assert "系统提示" in captured["messages"][0]["content"]
    assert captured["messages"][1]["content"] == "用户输入"
