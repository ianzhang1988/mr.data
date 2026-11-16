import logging

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore
from mr_data.models import DialogueLog
from mr_data.offline import AttributionEngine

from conftest import capture_events, open_log_propagation

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _setup_closed_session(pg: PostgresStore, session_id: str) -> None:
    pg.create_session(session_id)
    pg.insert_dialogue(DialogueLog(session_id=session_id, role="user", content="测试输入"))
    pg.insert_dialogue(
        DialogueLog(session_id=session_id, role="assistant", content="测试回复")
    )
    pg.close_session(session_id)


def _make_engine(pg, chroma_store, fake_llm) -> AttributionEngine:
    return AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)


def test_attribution_failure_does_not_mark_processed(
    fake_llm, pg, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    _setup_closed_session(pg, test_session_id)

    def _failing_structured_chat(system_prompt, user_prompt, response_format, temperature=0.2):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr(fake_llm, "chat_structured", _failing_structured_chat)

    engine = _make_engine(pg, chroma_store, fake_llm)
    engine.run()

    # Attribution failed, so the dialogues must stay unprocessed for a retry.
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 2

    # No dialogue memories should have been written for the failed session.
    docs = chroma_store.query_memories("测试", session_id=test_session_id, top_k=10)
    assert not any(d.metadata.source_type == "dialogue" for d in docs)


def test_attribution_retry_after_failure(
    fake_llm, pg, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    _setup_closed_session(pg, test_session_id)

    def _failing_structured_chat(system_prompt, user_prompt, response_format, temperature=0.2):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr(fake_llm, "chat_structured", _failing_structured_chat)

    engine = _make_engine(pg, chroma_store, fake_llm)
    engine.run()

    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 2

    # LLM recovers with an empty (but valid) attribution result; run again.
    def _empty_deltas(system_prompt, user_prompt, response_format, temperature=0.2):
        return {"deltas": []}

    monkeypatch.setattr(fake_llm, "chat_structured", _empty_deltas)
    engine.run()

    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 0

    # Dialogue memories should now be persisted in chunked form.
    docs = chroma_store.query_memories("测试", session_id=test_session_id, top_k=10)
    dialogue_docs = [d for d in docs if d.metadata.source_type == "dialogue"]
    assert dialogue_docs
    assert all(hasattr(d.metadata, "chunk_index") for d in dialogue_docs)


def test_attribution_failure_counts_attempts(
    fake_llm, pg, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    """未达死信阈值时：会话保持 unprocessed 等重试，attempts 计数递增。"""
    _setup_closed_session(pg, test_session_id)
    monkeypatch.setattr(settings, "offline_max_attribution_attempts", 3)

    def _failing_structured_chat(system_prompt, user_prompt, response_format, temperature=0.2):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr(fake_llm, "chat_structured", _failing_structured_chat)

    engine = _make_engine(pg, chroma_store, fake_llm)
    engine.run()
    engine.run()

    session = pg.get_session(test_session_id)
    assert session.attribution_attempts == 2
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 2


def test_attribution_dead_letter_after_threshold(
    fake_llm, pg, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    """连续失败达到阈值后：会话未处理日志置为已处理（死信），不再重试。"""
    _setup_closed_session(pg, test_session_id)
    monkeypatch.setattr(settings, "offline_max_attribution_attempts", 2)

    def _failing_structured_chat(system_prompt, user_prompt, response_format, temperature=0.2):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr(fake_llm, "chat_structured", _failing_structured_chat)

    engine = _make_engine(pg, chroma_store, fake_llm)
    engine.run()
    # 第一次失败：仍可重试
    assert len(pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)) == 2

    engine.run()
    # 第二次失败达到阈值：死信落葬，不再出现在待归因列表
    session = pg.get_session(test_session_id)
    assert session.attribution_attempts == 2
    assert pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True) == []
    assert pg.list_closed_sessions_with_unprocessed() == []

    # LLM 恢复后也不会再被拾起（死信不可逆）
    def _empty_deltas(system_prompt, user_prompt, response_format, temperature=0.2):
        return {"deltas": []}

    monkeypatch.setattr(fake_llm, "chat_structured", _empty_deltas)
    engine.run()
    assert pg.list_closed_sessions_with_unprocessed() == []


def test_dead_letter_logs_split_and_transcript_counts(
    fake_llm, pg, chroma_store, temp_log_dir, test_session_id, monkeypatch, caplog
):
    """拆批与死信可见性（截断调查改进项 B2，改进 82）：死信只落葬本批实际
    进过 transcript 的分片，不连坐 limit 之外从未取过的日志；
    never_in_transcript 字段保留（日志 schema 稳定）但分片级落葬后恒为 0。"""
    _setup_closed_session(pg, test_session_id)  # 2 条日志
    monkeypatch.setattr(settings, "offline_max_attribution_attempts", 2)
    monkeypatch.setattr(settings, "offline_max_session_logs", 1)  # 每批只取 1 条 → 拆批

    def _failing_structured_chat(system_prompt, user_prompt, response_format, temperature=0.2):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr(fake_llm, "chat_structured", _failing_structured_chat)

    engine = _make_engine(pg, chroma_store, fake_llm)
    # 引擎构造会重新 get_logger（propagate 重置为 False），传播开关须在构造后打开。
    open_log_propagation(caplog, monkeypatch, "mr_data.offline")
    caplog.set_level(logging.INFO, logger="mr_data.offline")

    engine.run()
    # 第一次失败：未达阈值，无落葬，2 条均保留待重试
    assert len(pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)) == 2

    engine.run()
    # 第二次失败达阈值：只落葬本批取过的 1 条，limit 之外的另 1 条不被连坐
    session = pg.get_session(test_session_id)
    assert session.attribution_attempts == 2
    assert len(pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)) == 1

    split = capture_events(caplog, "offline.session_logs_split")
    # run1、run2 时未处理数均为 2 > limit(1)，各记一次；run1 失败未标记，
    # run2 计数不变。run2 起只剩 1 条，不再有后续批次，不记。
    assert len(split) == 2
    assert split[0].details == {"batch_size": 1, "limit": 1, "total_batches": 2}

    dead = capture_events(caplog, "offline.session_dead_lettered")
    assert len(dead) == 1
    details = dead[0].details
    # 分片级落葬：仅本批进过 transcript 的 1 条；从未取过的日志不被集体置已处理。
    assert details["marked_dialogues"] == 1
    assert details["in_transcript"] == 1
    assert details["never_in_transcript"] == 0

    # 剩余 1 条在第 3 次 run 才被取出；attempts 已超阈值（会话级计数），
    # 失败后即时落葬——分片隔离不连坐，但每个分片终被独立安葬。
    engine.run()
    assert pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True) == []
    assert pg.list_closed_sessions_with_unprocessed() == []
    dead = capture_events(caplog, "offline.session_dead_lettered")
    assert len(dead) == 2
    assert dead[1].details["marked_dialogues"] == 1
    assert dead[1].details["never_in_transcript"] == 0
