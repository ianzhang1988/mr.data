"""chroma_recreate_on_mismatch 数据丢失防护测试。

覆盖：
- embedding 维度 mismatch 重建前自动导出 JSON 备份；
- restore_collection_backup 用新 embedding fn 重算向量并保真 metadata，重复执行全部跳过；
- 导出失败时阻止删除（fail-safe）；
- ephemeral 模式不导出。
"""

import json
from pathlib import Path

import chromadb
import pytest

from mr_data.config import settings
from mr_data.db import ChromaStore
from mr_data.db.chroma import event_doc_id
from mr_data.models import PersonalityEvent

from conftest import FakeEmbedding


def _make_store(persist_dir: Path, dim: int) -> ChromaStore:
    return ChromaStore(
        persist_dir=str(persist_dir),
        personality_embedding_fn=FakeEmbedding(dim=dim),
        memory_embedding_fn=FakeEmbedding(dim=dim),
    )


def _seed_personality(store: ChromaStore, n: int = 3) -> list[str]:
    ids = []
    for i in range(n):
        event = PersonalityEvent(
            id=event_doc_id(i, 1, f"事件摘要{i}"),
            content=f"人格事件{i}",
            context=f"上下文{i}",
            speaker="assistant",
            dimension_ids=[1],
            source_type="event",
            source_id=str(i),
        )
        ids.append(store.add_personality_event(event))
    return ids


def _recreate_with_new_dim(tmp_path, monkeypatch, dim: int = 16) -> ChromaStore:
    monkeypatch.setattr(settings, "personality_embedding_dim", dim)
    monkeypatch.setattr(settings, "memory_embedding_dim", dim)
    monkeypatch.setattr(settings, "chroma_recreate_on_mismatch", True)
    return _make_store(tmp_path / "chroma", dim)


def _backup_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "chroma-backups").glob("*.json"))


# ---------------------------------------------------------------------------
# mismatch 导出
# ---------------------------------------------------------------------------


def test_mismatch_exports_backup_before_recreate(tmp_path, monkeypatch):
    store = _make_store(tmp_path / "chroma", dim=8)
    ids = _seed_personality(store)
    store.add_memory("s1", "记忆内容", memory_id="mem:1", metadata={"recall_count": 2})
    assert store.personality.count() == 3

    # 新 dim 触发 mismatch → 自动备份并重建
    store2 = _recreate_with_new_dim(tmp_path, monkeypatch, dim=16)
    assert store2.personality.count() == 0
    assert store2.memories.count() == 0

    backups = _backup_files(tmp_path)
    assert len(backups) == 2  # personality + memories 各一份
    by_name = {json.loads(p.read_text(encoding="utf-8"))["collection"]: p for p in backups}
    assert set(by_name) == {"personality", "memories"}

    payload = json.loads(by_name["personality"].read_text(encoding="utf-8"))
    assert payload["count"] == 3
    assert payload["collection_metadata"]["embedding_dim"] == 512  # 备份记录的是旧集合的 metadata
    docs = {d["id"]: d for d in payload["documents"]}
    assert set(docs) == set(ids)
    for entry in docs.values():
        assert entry["document"].startswith("search_document: ")
        assert entry["metadata"]["source_type"] == "event"

    mem_payload = json.loads(by_name["memories"].read_text(encoding="utf-8"))
    assert mem_payload["count"] == 1
    assert mem_payload["documents"][0]["metadata"]["recall_count"] == 2


def test_export_failure_blocks_deletion(tmp_path, monkeypatch):
    store = _make_store(tmp_path / "chroma", dim=8)
    _seed_personality(store)
    backup_dir = tmp_path / "chroma-backups"

    def _fail_write(self, *args, **kwargs):
        # 只在备份落盘时抛错，不影响其他写入
        if self.parent == backup_dir:
            raise OSError("disk full")
        raise AssertionError("unexpected write_text during test")

    monkeypatch.setattr(Path, "write_text", _fail_write)
    with pytest.raises(RuntimeError, match="refusing to delete"):
        _recreate_with_new_dim(tmp_path, monkeypatch, dim=16)

    # 集合未被删除，旧数据完好
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    coll = client.get_collection("personality")
    assert coll.count() == 3


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------


def test_restore_backup_recomputes_embeddings_and_is_idempotent(tmp_path, monkeypatch):
    store = _make_store(tmp_path / "chroma", dim=8)
    ids = _seed_personality(store)
    old = store.personality.get(ids=ids, include=["documents", "metadatas"])

    store2 = _recreate_with_new_dim(tmp_path, monkeypatch, dim=16)
    backup = next(p for p in _backup_files(tmp_path) if p.name.startswith("personality-"))

    report = store2.restore_collection_backup(backup)
    assert report == {"collection": "personality", "restored": 3, "skipped": 0}
    assert store2.personality.count() == 3

    restored = store2.personality.get(ids=ids, include=["documents", "metadatas", "embeddings"])
    assert restored["ids"] == old["ids"]
    assert restored["documents"] == old["documents"]
    assert restored["metadatas"] == old["metadatas"]
    # embedding 用新 fn 重算，维度为新 dim
    assert all(len(emb) == 16 for emb in restored["embeddings"])

    # 重复执行：全部跳过，不翻倍
    report2 = store2.restore_collection_backup(backup)
    assert report2 == {"collection": "personality", "restored": 0, "skipped": 3}
    assert store2.personality.count() == 3


# ---------------------------------------------------------------------------
# ephemeral 模式不导出
# ---------------------------------------------------------------------------


def test_ephemeral_mode_skips_export(chroma_store, monkeypatch):
    monkeypatch.setattr(settings, "chroma_recreate_on_mismatch", True)
    _seed_personality(chroma_store)
    assert chroma_store.persist_dir is None
    # 直接触发 mismatch 分支（ephemeral 构造时已清空集合，故手动调用）
    chroma_store._ensure_collection("personality", dim=999, model_name="other-model")
    assert chroma_store.personality.count() == 0
    # 无 persist_dir → 不产出任何备份文件，也不报错
