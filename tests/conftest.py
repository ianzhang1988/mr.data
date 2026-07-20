import json
import os
import shutil
import tempfile
import uuid

# Disable web search by default in tests to avoid external network calls.
os.environ.setdefault("MR_DATA_ENABLE_WEB_SEARCH", "false")

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore, PgEmbedManager
from mr_data.llm import LLMClient
from mr_data.logging import reset_loggers


class FakeLLMClient(LLMClient):
    """LLM client that returns deterministic responses for tests."""

    def __init__(self):
        super().__init__(base_url="http://fake", api_key="fake", model="fake")

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        if "检索查询" in system_prompt and "内心独白" in system_prompt:
            return "检索查询：用户查询改写 内心独白：这是一个测试内心独白"
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


@pytest.fixture
def temp_log_dir(tmp_path, monkeypatch):
    """Provide a temporary log directory and reset logger cache."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(settings, "log_dir", str(log_dir))
    reset_loggers()
    yield str(log_dir)
    reset_loggers()
