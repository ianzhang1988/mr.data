import hashlib
import os

import pytest

import fastembed

from mr_data.db import ChromaStore
from mr_data.embeddings import JinaEmbedding
from mr_data.models import PersonalityEvent


class _FakeVector:
    def __init__(self, values: list[float]):
        self._values = values

    def __getitem__(self, item):
        return _FakeVector(self._values[item])

    def tolist(self):
        return list(self._values)


class _FakeTextEmbedding:
    def __init__(self, *, output_dim: int = 8, **kwargs):
        self._output_dim = output_dim

    def embed(self, texts: list[str]):
        for _ in texts:
            yield _FakeVector([float(i) for i in range(self._output_dim)])


@pytest.fixture
def fake_fastembed(monkeypatch):
    monkeypatch.setattr(fastembed, "TextEmbedding", _FakeTextEmbedding)


def test_jina_returns_full_model_output(fake_fastembed):
    # Jina v2 output is used as-is: no matryoshka truncation.
    embedding = JinaEmbedding(dim=4)
    result = embedding(["test"])
    assert len(result) == 1
    # _FakeTextEmbedding default output_dim is 8, so the full 8 dims are returned.
    assert len(result[0]) == 8
    assert result[0] == [float(i) for i in range(8)]


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


@pytest.mark.parametrize("kind", [
    "personality_document", "personality_query", "memory_query", "memory_document",
])
def test_embeddings_no_prefix_on_add_and_query(kind):
    """Jina v2 requires no prefix: 原始文本原样进嵌入，集合间互不串前缀。"""
    recorder = _RecordingEmbedding(dim=8)
    other = _RecordingEmbedding(dim=8)
    is_personality = kind.startswith("personality")
    store = ChromaStore(
        ephemeral=True,
        personality_embedding_fn=recorder if is_personality else other,
        memory_embedding_fn=recorder if not is_personality else other,
    )
    if kind == "personality_document":
        event = PersonalityEvent(
            content="测试台词",
            context="测试场景",
            dimension_ids=[1],
            source_type="line",
        )
        doc_id = store.add_personality_event(event)
        assert recorder.recorded == ["测试场景"]
        assert store.personality.get(ids=[doc_id])["documents"] == ["测试场景"]
    elif kind == "personality_query":
        # Seed a doc so the query has something to retrieve.
        store.add_personality_event(
            PersonalityEvent(content="测试台词", dimension_ids=[1], source_type="line")
        )
        recorder.recorded.clear()
        store.query_personality("测试查询")
        assert recorder.recorded == ["测试查询"]
    elif kind == "memory_query":
        store.add_memory("s1", "用户：你好")
        recorder.recorded.clear()
        store.query_memories("你好", session_id="s1")
        assert recorder.recorded == ["你好"]
    else:  # memory_document
        store.add_memory("s1", "用户：你好")
        assert recorder.recorded == ["用户：你好"]


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("MR_DATA_RUN_SLOW") != "1",
    reason="Requires downloading the Jina model; set MR_DATA_RUN_SLOW=1",
)
def test_real_embedding_dimensions(tmp_path):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    event = PersonalityEvent(content="测试", dimension_ids=[1], source_type="line")
    store.add_personality_event(event)
    store.add_memory("s1", "测试记忆")

    personality_meta = store.personality.metadata or {}
    memories_meta = store.memories.metadata or {}
    assert personality_meta.get("embedding_dim") == 768
    assert memories_meta.get("embedding_dim") == 768
    assert personality_meta.get("embedding_model") == "jinaai/jina-embeddings-v2-base-zh"
    assert memories_meta.get("embedding_model") == "jinaai/jina-embeddings-v2-base-zh"
