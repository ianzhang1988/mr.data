"""DialogueGraph.chat 事件回调（chat-http-api 计划 §4 方案 a）。

驱动方（HTTP server / agent 测试）经 on_event 拿到本轮管道的结构化事件流，
无需解析日志文件。事件形状与日志结构化 extra 同构：{event, session_id?, details}。
"""

import pytest

from mr_data.db import PostgresStore
from mr_data.online import DialogueGraph

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _build_graph(fake_llm, pg, chroma_store):
    return DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )


def test_chat_on_event_collects_ordered_events(
    fake_llm, test_session_id, pg_available, chroma_store
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.create_session(test_session_id)
    graph = _build_graph(fake_llm, pg, chroma_store)

    events: list[dict] = []
    reply = graph.chat(test_session_id, "你好", on_event=events.append)

    assert reply.text
    names = [e["event"] for e in events]
    # 首尾锚点 + 关键中间事件：顺序即管道执行序
    assert names[0] == "chat.start"
    assert names[-1] == "chat.turn"
    for expected in (
        "personality.loaded",
        "think.decision_generated",
        "chat.reply",
    ):
        assert expected in names
    assert names.index("think.decision_generated") < names.index("chat.reply")

    # 形状：event 恒有；session_id/details 取自结构化 extra
    for e in events:
        assert e["event"]
        assert e.get("session_id", test_session_id) == test_session_id
    turn = events[-1]
    assert "details" in turn
    assert "assistant_log_id" in turn["details"]


def test_chat_without_on_event_unchanged(
    fake_llm, test_session_id, pg_available, chroma_store
):
    """默认 None：行为与旧签名完全一致（既有全部测试守此路径）。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.create_session(test_session_id)
    graph = _build_graph(fake_llm, pg, chroma_store)

    reply = graph.chat(test_session_id, "你好")
    assert reply.text


def test_chat_on_event_survives_turn_exception(
    fake_llm, test_session_id, pg_available, chroma_store
):
    """整轮异常照常抛出，且 tap handler 必须摘除（不影响后续无回调调用）。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.create_session(test_session_id)
    graph = _build_graph(fake_llm, pg, chroma_store)

    original_load = graph._load_recent_messages
    graph._load_recent_messages = lambda sid: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    events: list[dict] = []
    with pytest.raises(RuntimeError, match="boom"):
        graph.chat(test_session_id, "你好", on_event=events.append)
    graph._load_recent_messages = original_load

    # handler 已摘除：logger 上无残留 tap
    assert all(
        not h.__class__.__name__.endswith("EventTapHandler")
        for h in graph.logger.handlers
    )

    # 恢复后无回调调用正常
    reply = graph.chat(test_session_id, "你好")
    assert reply.text
