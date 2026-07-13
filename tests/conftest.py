import os
import shutil
import tempfile
import uuid

# Disable web search by default in tests to avoid external network calls.
os.environ.setdefault("MR_DATA_ENABLE_WEB_SEARCH", "false")

import pytest

from mr_data.db import PostgresStore, PgEmbedManager
from mr_data.llm import LLMClient


class FakeLLMClient(LLMClient):
    """LLM client that returns deterministic responses for tests."""

    def __init__(self):
        super().__init__(base_url="http://fake", api_key="fake", model="fake")

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        if "查询改写" in system_prompt:
            return "用户查询改写"
        if "归因分析器" in system_prompt:
            # Return empty attribution to avoid creating dimensions in tests
            return '{"deltas": []}'
        return "这是一个测试回复。"

    def chat_structured(self, system_prompt, user_prompt, response_format, temperature=0.2):
        return {"deltas": []}


@pytest.fixture
def fake_llm():
    return FakeLLMClient()


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
