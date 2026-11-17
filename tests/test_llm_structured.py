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
    """Fake ``beta.chat.completions`` endpoint with a call counter.

    贴近真实 SDK 行为：content 能被 response_format 校验时填 ``parsed`` 实例，
    否则 ``parsed=None``（JSON 非法/校验失败时 SDK 同样置 None）。"""

    def __init__(self, error: Exception | None = None, content: str = "{}"):
        self.error = error
        self.content = content
        self.parse_calls = 0

    def parse(self, **kwargs):
        self.parse_calls += 1
        if self.error is not None:
            raise self.error
        parsed = None
        try:
            parsed = kwargs["response_format"].model_validate_json(self.content)
        except Exception:
            pass
        message = SimpleNamespace(content=self.content, parsed=parsed)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _inject_parse_api(client: LLMClient, completions: _FakeParseCompletions) -> None:
    client._client = SimpleNamespace(
        beta=SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ('{"value": 1}', {"value": 1}),
    ('```json\n{"value": 2}\n```', {"value": 2}),
    ('好的，这是结果：\n{"value": 3}\n希望对你有帮助。', {"value": 3}),
])
def test_extract_json(text, expected):
    assert _extract_json(text) == expected


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


@pytest.mark.parametrize("parse_error", [None, RuntimeError("parse not supported")], ids=["success", "fallback"])
def test_auto_mode_parse_and_caches(monkeypatch, parse_error):
    """parse 成功直接消费 parsed；失败回退 prompt 并缓存不再支持。"""
    monkeypatch.setattr(settings, "llm_structured_mode", "auto")
    client = _make_client()
    completions = _FakeParseCompletions(
        error=parse_error, content='{"value": 11}' if parse_error is None else "{}"
    )
    _inject_parse_api(client, completions)

    monkeypatch.setattr(
        client, "chat_with_messages", lambda messages, temperature=0.7: '{"value": 7}'
    )

    assert client._parse_supported is True
    result = client.structured_chat([{"role": "user", "content": "x"}], _SimpleResult)
    if parse_error is None:
        assert result == {"value": 11}
        assert client._parse_supported is True
    else:
        assert result == {"value": 7}
        assert client._parse_supported is False
    assert completions.parse_calls == 1

    if parse_error is not None:
        # Second call must skip the parse endpoint entirely.
        result = client.structured_chat([{"role": "user", "content": "x"}], _SimpleResult)
        assert result == {"value": 7}
        assert completions.parse_calls == 1


def test_parse_main_path_uses_message_parsed(monkeypatch):
    """主路径消费 SDK 的 message.parsed，不再对 content 做 json.loads（改进 81）。"""
    monkeypatch.setattr(settings, "llm_structured_mode", "parse")
    client = _make_client()
    # content 是非法 JSON：若走 json.loads 必然失败，结果正确即证明走了 .parsed。
    completions = _FakeParseCompletions(content="前面的解释……不是 JSON")
    _inject_parse_api(client, completions)
    # 强制 parsed 为合法实例，模拟 SDK 校验成功（真实 SDK 下两者同源）。
    original_parse = completions.parse

    def parse_with_parsed(**kwargs):
        resp = original_parse(**kwargs)
        resp.choices[0].message.parsed = _SimpleResult(value=21)
        return resp

    completions.parse = parse_with_parsed

    result = client.structured_chat([{"role": "user", "content": "x"}], _SimpleResult)

    assert result == {"value": 21}


def test_parse_falls_back_to_content_json_when_parsed_is_none(monkeypatch):
    """parsed=None（SDK 未填/校验失败）时兜底解析 content（改进 81 保留的兼容路径）。"""
    monkeypatch.setattr(settings, "llm_structured_mode", "auto")
    client = _make_client()
    # "{}") 缺 value 必填字段 → model_validate_json 失败 → fake 置 parsed=None。
    completions = _FakeParseCompletions(content='{"value": 33}')

    def parse_no_parsed(**kwargs):
        completions.parse_calls += 1
        message = SimpleNamespace(content=completions.content, parsed=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions.parse = parse_no_parsed
    _inject_parse_api(client, completions)

    result = client.structured_chat([{"role": "user", "content": "x"}], _SimpleResult)

    assert result == {"value": 33}
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

    with pytest.raises(RuntimeError):
        client.structured_chat([{"role": "user", "content": "x"}], _SimpleResult)
    # No fallback caching in parse mode.
    assert client._parse_supported is True



