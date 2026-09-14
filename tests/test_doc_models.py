"""管道 doc 模型与 collection/chunk schema 化测试（改进项 58）。

覆盖：
- WebDoc model_copy 两阶段流转（extract 重定 id / filter 抽取）字段演进；
- CollectionMetadata alias 序列化/反序列化 roundtrip；
- increment_memory_recall 模型化后落库 key 集不变；
- DialogueChunk 字段名与 DialogueMemoryMetadata 对齐（无改名映射的回归锁定）。
"""

import pytest

from mr_data.models import (
    CollectionMetadata,
    DialogueChunk,
    DialogueMemoryMetadata,
    WebDoc,
    WebDocMetadata,
)


def test_webdoc_model_copy_pipeline_evolution():
    doc = WebDoc(
        id="web:abc",
        page_content="标题\n正文",
        metadata=WebDocMetadata(url="https://baidu.com/link?x=1", title="标题"),
    )

    # extract 阶段：重定 id + 记录原始跳转链接
    extracted = doc.model_copy(update={
        "id": "web:real",
        "page_content": "标题\n提取全文",
        "metadata": doc.metadata.model_copy(update={
            "url": "https://real.example.com/page",
            "source_url": doc.metadata.url,
            "extracted": True,
        }),
    })
    assert extracted.id == "web:real"
    assert extracted.metadata.url == "https://real.example.com/page"
    assert extracted.metadata.source_url == "https://baidu.com/link?x=1"
    assert extracted.metadata.extracted is True
    # 原文档不被就地污染
    assert doc.id == "web:abc"
    assert doc.metadata.extracted is False

    # filter 阶段：llm_extracted 标记
    filtered = extracted.model_copy(update={
        "page_content": "摘取内容",
        "metadata": extracted.metadata.model_copy(update={"llm_extracted": True}),
    })
    assert filtered.page_content == "摘取内容"
    assert filtered.metadata.llm_extracted is True
    assert filtered.metadata.source_url == "https://baidu.com/link?x=1"


def test_webdoc_metadata_allows_unknown_extra_keys():
    meta = WebDocMetadata.model_validate(
        {"source_type": "web", "url": "u", "title": "t", "future_field": 1}
    )
    assert meta.url == "u"
    assert meta.future_field == 1  # extra="allow" 兜底 provider 扩展


def test_collection_metadata_alias_roundtrip():
    meta = CollectionMetadata(embedding_dim=512, embedding_model="nomic")
    dumped = meta.model_dump(by_alias=True, exclude_none=True)
    assert dumped == {
        "hnsw:space": "cosine",
        "embedding_dim": 512,
        "embedding_model": "nomic",
    }
    parsed = CollectionMetadata.model_validate(dumped)
    assert parsed.embedding_dim == 512
    assert parsed.embedding_model == "nomic"
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
