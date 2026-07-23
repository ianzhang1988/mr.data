import hashlib
import json
import os
import shutil
import tempfile
import uuid

# Disable web search by default in tests to avoid external network calls.
os.environ.setdefault("MR_DATA_ENABLE_WEB_SEARCH", "false")

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore, PgEmbedManager, ChromaStore
from mr_data.llm import LLMClient
from mr_data.logging import reset_loggers


class FakeLLMClient(LLMClient):
    """LLM client that returns deterministic responses for tests."""

    def __init__(self):
        super().__init__(base_url="http://fake", api_key="fake", model="fake")

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        if "性格维度选择助手" in system_prompt:
            return '{"dimension_ids": [1]}'
        if "检索查询" in system_prompt and "内心独白" in system_prompt:
            return "检索查询：用户查询改写 内心独白：这是一个测试内心独白"
        if "判断以下网络资料" in system_prompt:
            return "yes"
        if "你是对话归因分析器" in system_prompt:
            return json.dumps({
                "deltas": [
                    {
                        "dimension_id": 1,
                        "delta_success": 1,
                        "delta_failure": 0,
                        "reason": "测试归因原因",
                        "evidence_snippets": ["user: 测试输入\nassistant: 测试回复"],
                        "relation_to_personality": "体现",
                    }
                ]
            })
        return "这是一个测试回复。"

    def chat_structured(self, system_prompt, user_prompt, response_format, temperature=0.2):
        name = response_format.__name__
        if name == "ThinkDecision":
            return {
                "inner_monologue": "这是一个测试内心独白",
                "personality_query": "测试性格查询",
                "memory_query": "测试记忆查询",
                "needs_web_search": False,
                "search_query": "测试搜索查询",
            }
        if name == "DimensionSelection":
            return {"dimension_ids": [1]}
        if name == "AttributionResult":
            return {
                "deltas": [
                    {
                        "dimension_id": 1,
                        "delta_success": 1,
                        "delta_failure": 0,
                        "reason": "测试归因原因",
                        "evidence_snippets": ["user: 测试输入\nassistant: 测试回复"],
                        "relation_to_personality": "体现",
                    }
                ]
            }
        if name in ("WebRelevanceFilterResult", "MemoryRelevanceFilterResult"):
            # Returning empty results causes the node to fall back to keeping all docs.
            return {"results": []}
        return {"deltas": []}


class FakeEmbedding:
    """Deterministic embedding function that avoids downloading heavy models."""

    def __init__(self, dim: int = 8):
        self._dim = dim

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [self._encode(text) for text in input]

    def _encode(self, text: str) -> list[float]:
        digest = hashlib.md5(text.encode("utf-8")).digest()
        return [float(b) / 255.0 for b in digest[: self._dim]]

    @property
    def dim(self) -> int:
        return self._dim


@pytest.fixture
def fake_llm():
    return FakeLLMClient()


@pytest.fixture
def fake_embedding():
    return FakeEmbedding(dim=8)


@pytest.fixture
def chroma_store(tmp_path):
    """ChromaStore with lightweight fake embeddings for unit/integration tests."""
    return ChromaStore(
        persist_dir=str(tmp_path / "chroma"),
        personality_embedding_fn=FakeEmbedding(dim=8),
        memory_embedding_fn=FakeEmbedding(dim=8),
    )


@pytest.fixture(scope="session")
def pgembed_server():
    """Start a temporary pgembed server for the test session when needed."""
    if os.environ.get("MR_DATA_POSTGRES_DSN"):
        # User provided an external DSN; no need to start embedded PG.
        yield None
        return

    data_dir = tempfile.mkdtemp(prefix="pgembed-")
    manager = PgEmbedManager(data_dir=data_dir)
    dsn = manager.start()
    os.environ["MR_DATA_POSTGRES_DSN"] = dsn
    yield manager
    manager.stop()
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def pg_available(pgembed_server):
    """Returns True if PostgreSQL is reachable."""
    dsn = os.environ.get("MR_DATA_POSTGRES_DSN")
    if not dsn and pgembed_server is not None:
        dsn = pgembed_server.get_dsn()
    if not dsn:
        return False
    try:
        store = PostgresStore(dsn=dsn)
        with store._cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture
def test_session_id():
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def temp_log_dir(tmp_path, monkeypatch):
    """Provide a temporary log directory and reset logger cache."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(settings, "log_dir", str(log_dir))
    reset_loggers()
    yield str(log_dir)
    reset_loggers()
