import hashlib

import pytest

from mr_data.db import ChromaStore
from mr_data.embeddings import NomicPersonalityEmbedding, BGEMemoryEmbedding
from mr_data.models import PersonalityEvent


class _RecordingEmbedding:
    """Records the raw text inputs passed to it and returns deterministic vectors."""

    def __init__(self, dim: int = 8):
        self._dim = dim
        self.recorded: list[str] = []

    def __call__(self, input: list[str]) -> list[list[float]]:
        self.recorded.extend(input)
        return [self._encode(text) for text in input]

    def _encode(self, text: str) -> list[float]:
        digest = hashlib.md5(text.encode("utf-8")).digest()
        return [float(b) / 255.0 for b in digest[: self._dim]]

    @property
    def dim(self) -> int:
        return self._dim


def test_personality_document_prefix_on_add(tmp_path):
    recorder = _RecordingEmbedding(dim=8)
    store = ChromaStore(
        persist_dir=str(tmp_path / "chroma"),
        personality_embedding_fn=recorder,
        memory_embedding_fn=_RecordingEmbedding(dim=8),
    )
    event = PersonalityEvent(
        content="测试台词",
        context="测试场景",
        dimension_ids=[1],
        source_type="line",
    )
    store.add_personality_event(event)
    assert any(item.startswith("search_document: ") for item in recorder.recorded)
    assert any("测试场景" in item for item in recorder.recorded)


def test_personality_query_prefix(tmp_path):
    recorder = _RecordingEmbedding(dim=8)
    store = ChromaStore(
        persist_dir=str(tmp_path / "chroma"),
        personality_embedding_fn=recorder,
        memory_embedding_fn=_RecordingEmbedding(dim=8),
    )
    # Seed a doc so the query has something to retrieve.
    store.add_personality_event(
        PersonalityEvent(content="测试台词", dimension_ids=[1], source_type="line")
    )
    recorder.recorded.clear()

    store.query_personality("测试查询")
    assert any(item.startswith("search_query: ") for item in recorder.recorded)


def test_memory_query_prefix(tmp_path):
    recorder = _RecordingEmbedding(dim=8)
    store = ChromaStore(
        persist_dir=str(tmp_path / "chroma"),
        personality_embedding_fn=_RecordingEmbedding(dim=8),
        memory_embedding_fn=recorder,
    )
    store.add_memory("s1", "用户：你好")
    recorder.recorded.clear()

    store.query_memories("你好", session_id="s1")
    assert any(
        item.startswith("Represent this sentence for searching relevant passages: ")
        for item in recorder.recorded
    )


def test_memory_documents_not_prefixed(tmp_path):
    recorder = _RecordingEmbedding(dim=8)
    store = ChromaStore(
        persist_dir=str(tmp_path / "chroma"),
        personality_embedding_fn=_RecordingEmbedding(dim=8),
        memory_embedding_fn=recorder,
    )
    store.add_memory("s1", "用户：你好")
    assert any(item == "用户：你好" for item in recorder.recorded)


@pytest.mark.slow
@pytest.mark.skip(reason="Requires downloading Nomic/BGE models; run manually with --run-slow")
def test_real_embedding_dimensions(tmp_path):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    event = PersonalityEvent(content="测试", dimension_ids=[1], source_type="line")
    store.add_personality_event(event)
    store.add_memory("s1", "测试记忆")

    personality_meta = store.personality.metadata or {}
    memories_meta = store.memories.metadata or {}
    assert personality_meta.get("embedding_dim") == 512
    assert memories_meta.get("embedding_dim") == 768
