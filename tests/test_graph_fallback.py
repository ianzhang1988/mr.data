"""Fallback and hardening tests for the online dialogue chain."""

import pytest

from mr_data.models import (
    PersonalityDocMetadata,
    ReplyBlock,
)
from mr_data.online import DialogueGraph

from conftest import FakeLLMClient, capture_events, open_log_propagation


class _BothRepliesFailLLM(FakeLLMClient):
    """structured_chat（AssistantReply）与 chat_with_messages 都抛异常。"""

    def structured_chat(self, messages, response_format, temperature=0.2):
        if response_format.__name__ == "AssistantReply":
            raise RuntimeError("structured reply failed")
        return super().structured_chat(messages, response_format, temperature)

    def chat_with_messages(self, messages, temperature=0.7):
        raise RuntimeError("plain fallback failed")


class _StructuredReplyFailsLLM(FakeLLMClient):
    """structured_chat（AssistantReply）抛异常；纯文本 chat_with_messages 仍成功。"""

    def structured_chat(self, messages, response_format, temperature=0.2):
        if response_format.__name__ == "AssistantReply":
            raise RuntimeError("structured reply failed")
        return super().structured_chat(messages, response_format, temperature)


@pytest.mark.usefixtures("reset_pg_state")
def test_assemble_and_log_static_fallback(
    test_session_id, pg, chroma_store, temp_log_dir
):
    """结构化与纯文本回复都失败时，assemble 返回静态兜底且 log_dialogue 仍落库。

    直接调用两个节点而非完整的 graph.chat：end-to-end 在 pgembed 冷启动时
    偶发出现新连接看不到刚提交 session 的时序问题，而节点级调用能稳定复现
    相同的降级与落库语义。
    """
    pg.create_session(test_session_id)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=_BothRepliesFailLLM(),
        enable_web_search=False,
    )

    dimensions = pg.list_dimensions(active_only=True)
    identity = pg.get_identity()
    state = {
        "session_id": test_session_id,
        "user_input": "测试输入",
        "identity": identity,
        "user_identity": None,
        "dimensions": dimensions,
        "selected_dimension_ids": [d.id for d in dimensions if d.id is not None],
        "personality_docs": [],
        "memory_docs": [],
        "messages": [],
        "web_docs": [],
        "inner_monologue": None,
    }

    expected_text = "我的处理回路暂时受到干扰，无法组织回复。"
    assemble_state = graph._assemble_and_generate(state)

    assert assemble_state["reply"] == expected_text
    assert assemble_state["reply_blocks"] == [ReplyBlock(text=expected_text, references=[])]

    graph._log_dialogue(assemble_state)

    # 当轮对话仍应写入 Postgres：user + assistant 各一条。
    with pg._cursor() as cur:
        cur.execute(
            "SELECT role, content FROM dialogue_logs WHERE session_id = %s ORDER BY id",
            (test_session_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "测试输入"
    assert rows[1]["role"] == "assistant"
    assert rows[1]["content"] == expected_text


@pytest.mark.usefixtures("reset_pg_state")
def test_assemble_falls_back_to_plain_text_reply(
    test_session_id, pg, chroma_store, caplog, monkeypatch
):
    """中间层降级：结构化回复失败 → 纯文本 chat_with_messages 成功。

    断言 chat.reply_fallback 恰好 1 条且无 chat.reply_fallback_failed。
    """
    pg.create_session(test_session_id)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=_StructuredReplyFailsLLM(),
        enable_web_search=False,
    )
    open_log_propagation(caplog, monkeypatch)  # 须在 graph 构造后（构造会重置 propagate）

    dimensions = pg.list_dimensions(active_only=True)
    state = {
        "session_id": test_session_id,
        "user_input": "测试输入",
        "identity": pg.get_identity(),
        "user_identity": None,
        "dimensions": dimensions,
        "selected_dimension_ids": [d.id for d in dimensions if d.id is not None],
        "personality_docs": [],
        "memory_docs": [],
        "messages": [],
        "web_docs": [],
        "inner_monologue": None,
    }

    assemble_state = graph._assemble_and_generate(state)

    expected_text = "这是一个测试回复。"
    assert assemble_state["reply"] == expected_text
    assert assemble_state["reply_blocks"] == [ReplyBlock(text=expected_text, references=[])]

    assert len(capture_events(caplog, "chat.reply_fallback")) == 1
    assert capture_events(caplog, "chat.reply_fallback_failed") == []


def test_retrieve_personality_failure_returns_empty_docs(
    fake_llm, chroma_store, monkeypatch
):
    graph = DialogueGraph(
        pg_store=None,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("chroma personality query failed")

    monkeypatch.setattr(chroma_store, "query_personality", _boom)

    state = {
        "session_id": "test-session",
        "user_input": "测试输入",
        "personality_query": "测试性格查询",
        "retrieval_query": "测试记忆查询",
    }
    result = graph._retrieve_personality(state)

    assert result["personality_docs"] == []


@pytest.mark.parametrize(
    "dim_ids_str, expected_ids",
    [
        ("1,abc,3", [1, 3]),
        ("", []),
        (None, []),
        (" 2 , bad , 4 ", [2, 4]),
        ("abc", []),
        ("1,2,3", [1, 2, 3]),
    ],
)
def test_from_chroma_metadata_tolerates_dirty_dimension_ids(
    dim_ids_str, expected_ids
):
    meta = {
        "utterance": "test",
        "source_id": "src-1",
        "dimension_ids": dim_ids_str,
    }
    parsed = PersonalityDocMetadata.from_chroma_metadata(meta)
    assert parsed.dimension_ids == expected_ids
    assert parsed.utterance == "test"
    assert parsed.source_id == "src-1"
