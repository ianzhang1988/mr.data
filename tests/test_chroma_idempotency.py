"""Chroma 写入路径幂等性测试（改进项 42）。

覆盖：
- personality / memories 稳定 id 的「已存在则跳过」语义；
- 归因 _apply 重放不产生重复文档；
- _stable_web_id 各 fallback 分支与跳转链接（洞 1、洞 2）。
"""

import hashlib

import pytest

from mr_data.db import PostgresStore
from mr_data.db.chroma import (
    dialogue_chunk_memory_id,
    evidence_doc_id,
    event_doc_id,
    line_doc_id,
)
from mr_data.models import DialogueLog, PersonalityEvent
from mr_data.offline import AttributionEngine
from mr_data.offline.attribution import AttributionResult, DimensionDelta
from mr_data.online.search_providers import _is_redirect_url, _stable_web_id


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
        metadata={"source_type": "dialogue", "recall_count": 0, "added_at": "t0"},
    )
    chroma_store.increment_memory_recall([memory_id])

    # 重放同一分块：应跳过，不重置 recall_count / added_at
    chroma_store.add_memory(
        "s1",
        "对话内容",
        memory_id=memory_id,
        metadata={"source_type": "dialogue", "recall_count": 0, "added_at": "t1"},
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
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
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
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm, log_dir=temp_log_dir)

    engine._apply(result, test_session_id, logs)
    personality_count = chroma_store.personality.count()
    memories_count = chroma_store.memories.count()
    assert personality_count == 3  # 2 evidence + 1 event_summary
    assert memories_count >= 1  # 至少一个对话分块

    # 模拟崩溃后重放：Chroma 侧文档数不变
    engine._apply(result, test_session_id, logs)
    assert chroma_store.personality.count() == personality_count
    assert chroma_store.memories.count() == memories_count


# ---------------------------------------------------------------------------
# _stable_web_id（洞 1、洞 2）
# ---------------------------------------------------------------------------


def test_stable_web_id_real_url_uses_url_hash():
    url = "https://example.com/page"
    assert _stable_web_id(url) == hashlib.sha256(url.encode("utf-8")).hexdigest()


def test_stable_web_id_redirect_url_falls_back_to_content_key():
    title, body = "同一页面", "相同摘要"
    id_a = _stable_web_id("https://www.baidu.com/link?url=AAA", body, title)
    id_b = _stable_web_id("https://www.baidu.com/link?url=BBB", body, title)
    id_c = _stable_web_id("https://www.so.com/link?m=CCC", body, title)
    # 跳转链接参数不同但内容相同 → id 相同（洞 2）
    assert id_a == id_b == id_c
    assert id_a.startswith("web:")


def test_stable_web_id_non_redirect_url_on_same_hosts_is_url_keyed():
    url = "https://www.baidu.com/p/12345"
    assert not _is_redirect_url(url)
    assert _stable_web_id(url) == hashlib.sha256(url.encode("utf-8")).hexdigest()


def test_stable_web_id_title_fallback_is_deterministic():
    # 洞 1：无 URL 无正文但有标题时不再退化为随机 id
    assert _stable_web_id("", "", "只有标题") == _stable_web_id("", "", "只有标题")
    assert _stable_web_id("", "正文") == _stable_web_id("", "正文")


def test_stable_web_id_empty_everything_is_random():
    assert _stable_web_id("", "", "") != _stable_web_id("", "", "")
