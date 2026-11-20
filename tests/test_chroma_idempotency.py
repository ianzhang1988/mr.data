"""Chroma 写入路径幂等性测试（改进项 42）。

覆盖：
- personality / memories 稳定 id 的「已存在则跳过」语义；
- 归因 _apply + _flush_post_commit_chroma 后重放幂等，不产生重复文档。
（_stable_web_id 各分支用例已随改进 78 删除该函数一并移除。）
"""

import logging

import pytest

from mr_data.db.chroma import (
    dialogue_chunk_memory_id,
    evidence_doc_id,
    event_doc_id,
    line_doc_id,
)
from mr_data.models import (
    DialogueLog,
    DialogueMemoryMetadata,
    PersonalityEvent,
    WebMemoryMetadata,
)
from mr_data.offline import AttributionEngine
from mr_data.offline.attribution import AttributionResult, DimensionDelta


# ---------------------------------------------------------------------------
# 稳定 id 派生函数
# ---------------------------------------------------------------------------


def test_stable_doc_id_helpers_are_deterministic_and_prefixed():
    assert evidence_doc_id(1, 2, "片段") == evidence_doc_id(1, 2, "片段")
    assert evidence_doc_id(1, 2, "片段") != evidence_doc_id(1, 2, "别的片段")
    assert evidence_doc_id(1, 2, "x").startswith("evidence:")
    assert event_doc_id(1, 2, "x").startswith("event:")
    assert line_doc_id("Data", "x").startswith("line:")
    assert dialogue_chunk_memory_id("s", 1, 2, "x").startswith("dialogue:")
    # None 与缺省 context 的处理稳定
    assert line_doc_id(None, "x") == line_doc_id("assistant", "x", None)


# ---------------------------------------------------------------------------
# ChromaStore 已存在跳过语义
# ---------------------------------------------------------------------------


def test_add_personality_event_with_stable_id_is_idempotent(chroma_store):
    event = PersonalityEvent(
        id=evidence_doc_id(7, 3, "关键证据"),
        content="关键证据",
        speaker="assistant",
        dimension_ids=[3],
        source_type="evidence",
        source_id="7",
    )
    first = chroma_store.add_personality_event(event)
    second = chroma_store.add_personality_event(event)
    assert first == second
    assert chroma_store.personality.count() == 1


def test_add_personality_event_without_id_stays_random(chroma_store):
    event = PersonalityEvent(content="同一句话", source_type="line")
    assert chroma_store.add_personality_event(event) != chroma_store.add_personality_event(event)
    assert chroma_store.personality.count() == 2


def test_add_memory_with_stable_id_preserves_metadata(chroma_store):
    memory_id = dialogue_chunk_memory_id("s1", 1, 2, "对话内容")
    chroma_store.add_memory(
        "s1",
        "对话内容",
        memory_id=memory_id,
        metadata=DialogueMemoryMetadata(recall_count=0, added_at="t0"),
    )
    chroma_store.increment_memory_recall([memory_id])

    # 重放同一分块：应跳过，不重置 recall_count / added_at
    chroma_store.add_memory(
        "s1",
        "对话内容",
        memory_id=memory_id,
        metadata=DialogueMemoryMetadata(recall_count=0, added_at="t1"),
    )

    assert chroma_store.memories.count() == 1
    meta = chroma_store.memories.get(ids=[memory_id], include=["metadatas"])["metadatas"][0]
    assert meta["recall_count"] == 1
    assert meta["added_at"] == "t0"


# ---------------------------------------------------------------------------
# 归因 _apply 重放（崩溃恢复场景）
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("reset_pg_state")
def test_apply_replay_does_not_duplicate_chroma_docs(
    pg, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pg.create_session(test_session_id)
    pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="user", content="测试输入"))
    pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="assistant", content="测试回复"))
    pg.close_session(test_session_id)
    logs = pg.get_recent_dialogues(session_id=test_session_id)

    dim_id = pg.insert_dimension("幂等测试维度")
    result = AttributionResult(
        deltas=[
            DimensionDelta(
                dimension_id=dim_id,
                delta_success=1,
                reason="测试",
                event_summary="值得记录的事件",
                evidence_snippets=["证据片段一", "证据片段二"],
                target_dialogue_log_id=logs[-1].id,
            )
        ]
    )
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)

    applied, events, purge = engine._apply(result, test_session_id, logs)
    engine._flush_post_commit_chroma(test_session_id, logs, events, purge)
    personality_count = chroma_store.personality.count()
    memories_count = chroma_store.memories.count()
    assert personality_count == 3  # 2 evidence + 1 event_summary
    assert memories_count >= 1  # 至少一个对话分块

    # 模拟崩溃后重放：Chroma 侧文档数不变
    applied, events, purge = engine._apply(result, test_session_id, logs)
    engine._flush_post_commit_chroma(test_session_id, logs, events, purge)
    assert chroma_store.personality.count() == personality_count
    assert chroma_store.memories.count() == memories_count


# ---------------------------------------------------------------------------
# ChromaStore 查询与清理行为（从 test_e2e 归位：不依赖 graph/离线归因）
# ---------------------------------------------------------------------------


def test_chroma_personality(chroma_store):
    store = chroma_store
    event = PersonalityEvent(content="测试事件", dimension_ids=[1, 2], source_type="event")
    doc_id = store.add_personality_event(event)
    assert doc_id

    docs = store.query_personality("测试")
    assert any("测试事件" in d.page_content for d in docs)
    assert any(1 in d.metadata.dimension_ids for d in docs)


def test_chroma_personality_context(chroma_store):
    store = chroma_store
    event = PersonalityEvent(
        content=" assistant: 这是 mr.data 的台词",
        context="user: 我们遇到了一个异常值\nassistant: 这是 mr.data 的台词",
        speaker="mr.data",
        dimension_ids=[1],
        source_type="line",
    )
    store.add_personality_event(event)

    # Query by context should return the utterance, not the full context.
    docs = store.query_personality("异常值", top_k=5)
    assert any("这是 mr.data 的台词" in d.page_content for d in docs)
    assert all("user:" not in d.page_content for d in docs)
    assert docs[0].metadata.context


@pytest.mark.parametrize("memory_form", ["dialogue", "web"])
def test_prune_stale_memories(chroma_store, memory_form):
    from datetime import datetime, timedelta, timezone

    session_id = f"prune-{memory_form}-session"
    old_time = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    if memory_form == "dialogue":
        query = "dialogue"
        old_content = "user: old stale dialogue"
        recent_content = "user: recent dialogue"
        old_meta = DialogueMemoryMetadata(
            session_id=session_id,
            added_at=old_time,
            last_recalled_at=old_time,
        )
        recent_meta = DialogueMemoryMetadata(session_id=session_id, added_at=now)
    else:
        query = "资料"
        old_content = "[网络资料] 旧资料"
        recent_content = "[网络资料] 新资料"
        old_meta = WebMemoryMetadata(
            url="http://example.com/old",
            title="旧资料",
            retrieved_at=old_time,
        )
        recent_meta = WebMemoryMetadata(
            url="http://example.com/new",
            title="新资料",
            retrieved_at=now,
        )
    chroma_store.add_memory(
        session_id, old_content, memory_id="web:old" if memory_form == "web" else None,
        metadata=old_meta,
    )
    chroma_store.add_memory(session_id, recent_content, metadata=recent_meta)
    if memory_form == "web":
        # 老但被召回过的 web 记忆应保留（recall_count 达标）
        chroma_store.add_memory(
            session_id,
            "[网络资料] 老但常用",
            memory_id="web:recalled",
            metadata=WebMemoryMetadata(
                url="http://example.com/recalled",
                title="老但常用",
                retrieved_at=old_time,
            ),
        )
        chroma_store.increment_memory_recall(["web:recalled"])

    pruned = chroma_store.prune_stale_memories(
        cutoff_days=90,
        min_recall_count=1,
    )
    assert pruned == 1

    remaining = chroma_store.query_memories(query, session_id=session_id, top_k=10)
    if memory_form == "dialogue":
        assert all("old stale" not in d.page_content for d in remaining)
    else:
        remaining_ids = {d.id for d in remaining}
        assert "web:old" not in remaining_ids


def test_prune_stale_memories_delete_failure_returns_zero(chroma_store, monkeypatch, caplog):
    """F-16：删除阶段抛异常时兜底——返回 0 并记 chroma.prune_failed，不向上抛。"""
    from datetime import datetime, timedelta, timezone

    from conftest import capture_events, open_log_propagation

    session_id = "prune-fail-session"
    old_time = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    chroma_store.add_memory(
        session_id,
        "user: old stale dialogue",
        metadata=DialogueMemoryMetadata(
            session_id=session_id,
            added_at=old_time,
            last_recalled_at=old_time,
        ),
    )

    def _failing_delete(self, ids):
        raise RuntimeError("chroma delete boom")

    # memories 是 property，每次访问可能拿到不同 wrapper；patch 类方法才稳
    coll = chroma_store.memories
    monkeypatch.setattr(type(coll), "delete", _failing_delete)

    open_log_propagation(caplog, monkeypatch, "mr_data.chroma")
    caplog.set_level(logging.WARNING, logger="mr_data.chroma")
    pruned = chroma_store.prune_stale_memories(cutoff_days=90, min_recall_count=1)

    assert pruned == 0
    failed = capture_events(caplog, "chroma.prune_failed")
    assert failed
    details = failed[0].details
    assert details["cutoff_days"] == 90
    assert details["min_recall_count"] == 1
    assert "chroma delete boom" in details["error"]
