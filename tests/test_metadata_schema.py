"""落库 metadata schema 类化测试（改进项 49）。

覆盖：
- DialogueLogMetadata 经 Postgres JSONB 往返后读回为模型，blocks/references 保真；
- PersonalityDocMetadata 的 to/from_chroma_metadata 往返（dimension_ids CSV）；
- add_memory + DialogueMemoryMetadata 落库的 metadata key 集合与旧裸 dict 形态一致。
"""

import pytest

from mr_data.db import PostgresStore
from mr_data.models import (
    DialogueLog,
    DialogueLogMetadata,
    DialogueMemoryMetadata,
    PersonalityDocMetadata,
    ReplyBlock,
    ReplyReference,
)


def test_dialogue_log_metadata_pg_roundtrip(pg_available, reset_pg_state):
    pg = PostgresStore()
    session_id = pg.create_session()

    metadata = DialogueLogMetadata(
        inner_monologue="用户在问事实问题",
        blocks=[
            ReplyBlock(
                text="第一段回复",
                references=[
                    ReplyReference(id="line:abc", source_type="line", summary="台词素材"),
                ],
            ),
            ReplyBlock(text="纯表达段"),
        ],
    )
    log_id = pg.insert_dialogue(
        DialogueLog(
            session_id=session_id,
            role="assistant",
            content="第一段回复\n第二段回复",
            metadata=metadata,
        )
    )

    logs = pg.get_recent_dialogues(session_id=session_id)
    found = next((log for log in logs if log.id == log_id), None)
    assert found is not None
    assert isinstance(found.metadata, DialogueLogMetadata)
    assert found.metadata.inner_monologue == "用户在问事实问题"
    assert len(found.metadata.blocks) == 2
    assert found.metadata.blocks[0].text == "第一段回复"
    assert found.metadata.blocks[0].references[0].id == "line:abc"
    assert found.metadata.blocks[0].references[0].source_type == "line"
    assert found.metadata.blocks[1].references == []


def test_dialogue_log_legacy_metadata_with_top_level_references(pg_available, reset_pg_state):
    """历史遗留行（顶层 references、无 blocks）读回不炸，blocks 落空列表。"""
    import json

    pg = PostgresStore()
    session_id = pg.create_session()
    with pg._cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO dialogue_logs (session_id, role, content, metadata)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (
                session_id,
                "assistant",
                "遗留回复",
                json.dumps({"inner_monologue": "旧形态", "references": [{"id": "web:x"}]}),
            ),
        )
        log_id = cur.fetchone()["id"]

    logs = pg.get_recent_dialogues(session_id=session_id)
    found = next(log for log in logs if log.id == log_id)
    assert isinstance(found.metadata, DialogueLogMetadata)
    assert found.metadata.inner_monologue == "旧形态"
    assert found.metadata.blocks == []


def test_personality_doc_metadata_chroma_roundtrip():
    original = PersonalityDocMetadata(
        utterance="我对此充满好奇。",
        context="多轮上下文",
        speaker="Data",
        dimension_ids=[1, 3, 10],
        source_type="evidence",
        source_id="42",
    )
    meta = original.to_chroma_metadata()
    # 落库 dict 全为 Chroma 可接受的标量，dimension_ids 为 CSV 字符串
    assert meta["dimension_ids"] == "1,3,10"
    assert all(isinstance(v, (str, int, float, bool)) for v in meta.values())

    parsed = PersonalityDocMetadata.from_chroma_metadata(meta)
    assert parsed == original


def test_personality_doc_metadata_from_chroma_defaults():
    parsed = PersonalityDocMetadata.from_chroma_metadata({"utterance": "只有台词"})
    assert parsed.dimension_ids == []
    assert parsed.speaker == "assistant"
    assert parsed.source_type == "line"
    assert parsed.source_id == ""
    assert parsed.to_chroma_metadata()["dimension_ids"] == ""


def test_add_memory_dialogue_metadata_key_set(chroma_store):
    """落库 key 集合回归保护：与旧裸 dict 形态逐键一致。"""
    expected_keys = {
        "source_type",
        "session_id",
        "chunk_index",
        "first_dialogue_log_id",
        "last_dialogue_log_id",
        "recall_count",
        "added_at",
        "last_recalled_at",
    }
    memory_id = chroma_store.add_memory(
        "s1",
        "user: 你好\nassistant: 你好！",
        metadata=DialogueMemoryMetadata(
            session_id="s1",
            chunk_index=0,
            first_dialogue_log_id=1,
            last_dialogue_log_id=2,
            added_at="2026-09-13T00:00:00+00:00",
        ),
    )
    meta = chroma_store.memories.get(ids=[memory_id], include=["metadatas"])["metadatas"][0]
    assert set(meta.keys()) == expected_keys
    assert meta["recall_count"] == 0
    assert meta["last_recalled_at"] == ""
    assert meta["first_dialogue_log_id"] == 1
