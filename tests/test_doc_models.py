"""管道 doc 模型与 collection/chunk schema 化测试（改进项 58）。

覆盖：
- CollectionMetadata alias 序列化/反序列化 roundtrip；
- increment_memory_recall 模型化后落库 key 集不变；
- DialogueChunk 字段名与 DialogueMemoryMetadata 对齐（无改名映射的回归锁定）。
"""

from mr_data.models import (
    CollectionMetadata,
    DialogueChunk,
    DialogueMemoryMetadata,
)


def test_collection_metadata_alias_roundtrip():
    meta = CollectionMetadata(embedding_dim=768, embedding_model="jinaai/jina-embeddings-v2-base-zh")
    dumped = meta.model_dump(by_alias=True, exclude_none=True)
    assert dumped == {
        "hnsw:space": "cosine",
        "embedding_dim": 768,
        "embedding_model": "jinaai/jina-embeddings-v2-base-zh",
    }
    parsed = CollectionMetadata.model_validate(dumped)
    assert parsed.embedding_dim == 768
    assert parsed.embedding_model == "jinaai/jina-embeddings-v2-base-zh"
    assert parsed.hnsw_space == "cosine"
    # 空 metadata（旧 collection 无指纹）可 validate，字段全默认
    empty = CollectionMetadata.model_validate({})
    assert empty.embedding_dim is None


def test_increment_memory_recall_preserves_metadata_keys(chroma_store):
    """increment_memory_recall 走模型后，落库 key 集与改进 49 锁定集合一致。"""
    expected_keys = {
        "session_id", "source_type", "chunk_index", "first_dialogue_log_id",
        "last_dialogue_log_id", "recall_count", "added_at", "last_recalled_at",
    }
    memory_id = chroma_store.add_memory(
        "s1",
        "对话内容",
        metadata=DialogueMemoryMetadata(
            session_id="s1", chunk_index=0, first_dialogue_log_id=1,
            last_dialogue_log_id=2, added_at="t0",
        ),
    )
    chroma_store.increment_memory_recall([memory_id])
    meta = chroma_store.memories.get(ids=[memory_id])["metadatas"][0]
    assert set(meta.keys()) == expected_keys
    assert meta["recall_count"] == 1
    assert meta["added_at"] == "t0"
    assert meta["last_recalled_at"] != ""


def test_dialogue_chunk_fields_align_with_memory_metadata():
    """chunk 字段名与 DialogueMemoryMetadata 同名，落库无需改名映射。"""
    chunk = DialogueChunk(
        content="user: a", chunk_index=0,
        first_dialogue_log_id=1, last_dialogue_log_id=3,
    )
    metadata_fields = set(DialogueMemoryMetadata.model_fields)
    for field in ("chunk_index", "first_dialogue_log_id", "last_dialogue_log_id"):
        assert field in metadata_fields
    # 直接属性对齐构造不报错即锁定映射关系
    meta = DialogueMemoryMetadata(
        session_id="s1",
        chunk_index=chunk.chunk_index,
        first_dialogue_log_id=chunk.first_dialogue_log_id,
        last_dialogue_log_id=chunk.last_dialogue_log_id,
        added_at="t0",
    )
    assert meta.first_dialogue_log_id == 1
    assert meta.last_dialogue_log_id == 3
