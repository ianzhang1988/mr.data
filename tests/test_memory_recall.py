"""increment_memory_recall 回归测试（改进项：recall 计数按返回 id 对齐写回）。

覆盖：
- 多 id 乱序：get 返回存储序而非请求序时，recall_count 仍写到正确的文档；
- 缺失 id：跳过并记 warning，不抛 ValueError；
- dialogue/web 混合：web 文档 metadata 不被注入 dialogue 形态字段。
"""

from mr_data.models import DialogueMemoryMetadata, WebMemoryMetadata


def _get_metadata(chroma_store, memory_id):
    result = chroma_store.memories.get(ids=[memory_id], include=["metadatas"])
    assert result["ids"] == [memory_id]
    return result["metadatas"][0]


def _add_dialogue_memory(chroma_store, memory_id, recall_count, session_id="s1"):
    chroma_store.add_memory(
        session_id,
        f"对话内容 {memory_id}",
        memory_id=memory_id,
        metadata=DialogueMemoryMetadata(recall_count=recall_count, added_at="t0"),
    )


def test_increment_aligned_with_returned_ids_not_request_order(chroma_store):
    _add_dialogue_memory(chroma_store, "mem:a", recall_count=0)
    _add_dialogue_memory(chroma_store, "mem:b", recall_count=5)
    _add_dialogue_memory(chroma_store, "mem:c", recall_count=2)

    # 请求顺序与插入（存储）顺序相反：错位实现会把 a/c 的计数互换
    chroma_store.increment_memory_recall(["mem:c", "mem:a"])

    assert _get_metadata(chroma_store, "mem:a")["recall_count"] == 1
    assert _get_metadata(chroma_store, "mem:b")["recall_count"] == 5
    assert _get_metadata(chroma_store, "mem:c")["recall_count"] == 3


def test_increment_skips_missing_ids_without_raising(chroma_store):
    _add_dialogue_memory(chroma_store, "mem:a", recall_count=0)

    chroma_store.increment_memory_recall(["mem:a", "dialogue:不存在的id"])

    meta = _get_metadata(chroma_store, "mem:a")
    assert meta["recall_count"] == 1
    assert meta["last_recalled_at"] is not None


def test_increment_mixed_dialogue_and_web_keeps_metadata_shape(chroma_store):
    _add_dialogue_memory(chroma_store, "mem:a", recall_count=0)
    chroma_store.upsert_memory(
        session_id="",
        content="[网络资料] 标题\nhttps://example.com\n正文",
        memory_id="web:x",
        metadata=WebMemoryMetadata(
            url="https://example.com",
            title="标题",
            retrieved_at="t0",
            retrieval_session_id="s0",
            query="查询",
        ),
    )

    chroma_store.increment_memory_recall(["web:x", "mem:a"])

    dialogue_meta = _get_metadata(chroma_store, "mem:a")
    assert dialogue_meta["source_type"] == "dialogue"
    assert dialogue_meta["recall_count"] == 1

    web_meta = _get_metadata(chroma_store, "web:x")
    assert web_meta["source_type"] == "web"
    assert web_meta["url"] == "https://example.com"
    # web 文档不被注入 dialogue 形态字段（如 added_at）
    assert "added_at" not in web_meta
