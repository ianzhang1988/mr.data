"""改进 75：离线归因 prompt 的 transcript token 预算保护。

覆盖：
- 短会话：prompt 无省略标记，system 保留「完整会话记录」原文（热路径零变化）；
- 超长会话：prompt 含省略标记且 N 与实际省略行数一致，首尾行保留且 [#id] 完整，
  system / prompt 措辞替换为省略版；
- 退化：llm_context_token_limit 极小不抛异常，至少第一行仍在 prompt 中。

fake pg / fake llm 均为 MagicMock 风格（参考 tests/test_attribution_empty_sessions.py）。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from mr_data.config import settings
from mr_data.models import DialogueLog
from mr_data.offline.attribution import AttributionEngine, AttributionResult

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)

OMITTED_MARKER_PREFIX = "……（中间省略 "


def _make_engine(captured: dict) -> AttributionEngine:
    pg = MagicMock()
    pg.get_identity.return_value = None
    pg.list_dimensions.return_value = []
    pg.list_session_dimension_ids.return_value = []
    pg.get_session.return_value = None

    llm = MagicMock()

    def _chat_structured(system, prompt, response_format, temperature=0.2):
        captured["system"] = system
        captured["prompt"] = prompt
        return {"deltas": []}

    llm.chat_structured.side_effect = _chat_structured
    return AttributionEngine(pg_store=pg, chroma_store=MagicMock(), llm=llm)


def _make_logs(session_id: str, contents: list[tuple[str, str]]) -> list[DialogueLog]:
    """contents: (role, content) 列表，id 从 1 递增。"""
    return [
        DialogueLog(
            id=i + 1,
            session_id=session_id,
            role=role,
            content=content,
            created_at=_BASE_TIME + timedelta(seconds=i),
        )
        for i, (role, content) in enumerate(contents)
    ]


def test_short_session_prompt_has_no_omission_marker():
    captured: dict = {}
    engine = _make_engine(captured)
    logs = _make_logs("s-short", [("user", "你好"), ("assistant", "你好！有什么可以帮你？")])

    result = engine._attribute_session("s-short", logs)

    assert isinstance(result, AttributionResult)
    assert result.deltas == []
    assert OMITTED_MARKER_PREFIX not in captured["prompt"]
    assert "完整会话记录" in captured["system"]
    assert "user[#1]" in captured["prompt"]
    assert "assistant[#2]" in captured["prompt"]


def test_long_session_transcript_is_head_tail_truncated():
    captured: dict = {}
    engine = _make_engine(captured)
    # 每条数千字 × 8 轮，总量远超 llm_context_token_limit(30000) 预算。
    big = "这是一段很长的粘贴内容。" * 2000  # ~4 万字符/条
    contents = [
        ("user", "开场问题"),
        ("assistant", "开场回答"),
    ] + [
        ("user" if i % 2 == 0 else "assistant", f"第{i}轮：{big}")
        for i in range(8)
    ] + [
        ("user", "结尾提问"),
        ("assistant", "结尾回答"),
    ]
    logs = _make_logs("s-long", contents)
    original_line_count = len(contents)

    result = engine._attribute_session("s-long", logs)

    assert isinstance(result, AttributionResult)
    prompt = captured["prompt"]
    marker = OMITTED_MARKER_PREFIX
    assert marker in prompt
    omitted_str = prompt.split(marker)[1].split(" 条消息）……")[0]
    omitted = int(omitted_str)
    assert omitted >= 1
    # 省略数 = 总行数 - prompt 中实际保留的 transcript 行数（除标记行）。
    kept_lines = [
        line
        for line in prompt.split("本次会话记录（中间有省略）：\n")[1]
        .split("\n\n请按 JSON 格式返回归因结果。")[0]
        .split("\n")
        if line.strip()
    ]
    non_marker = [line for line in kept_lines if not line.startswith(OMITTED_MARKER_PREFIX)]
    assert len(non_marker) + omitted == original_line_count
    # 首行与尾行都在 prompt 中，且 [#id] 标签完整。
    assert "user[#1]: 开场问题" in prompt
    assert f"assistant[#{original_line_count}]: 结尾回答" in prompt
    for line in non_marker:
        assert "[#" in line
    # system 措辞已替换。
    assert "完整会话记录" not in captured["system"]
    assert f"中间部分已省略，共 {omitted} 条" in captured["system"]


def test_tiny_budget_keeps_first_line_and_does_not_raise(monkeypatch):
    monkeypatch.setattr(settings, "llm_context_token_limit", 200)
    captured: dict = {}
    engine = _make_engine(captured)
    contents = [("user", "超长首行。" * 3000)] + [
        ("assistant" if i % 2 else "user", f"第{i}轮内容。" * 500) for i in range(6)
    ]
    logs = _make_logs("s-tiny", contents)

    result = engine._attribute_session("s-tiny", logs)  # 不应抛异常

    assert isinstance(result, AttributionResult)
    assert "user[#1]" in captured["prompt"]
