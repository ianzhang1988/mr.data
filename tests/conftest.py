import os
import uuid

import pytest

from mr_data.db import PostgresStore
from mr_data.llm import LLMClient


class FakeLLMClient(LLMClient):
    """LLM client that returns deterministic responses for tests."""

    def __init__(self):
        super().__init__(base_url="http://fake", api_key="fake", model="fake")

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        if "查询改写" in system_prompt:
            return "用户查询改写"
        return "这是一个测试回复。"

    def chat_structured(self, system_prompt, user_prompt, response_format, temperature=0.2):
        return {"deltas": []}


@pytest.fixture
def fake_llm():
    return FakeLLMClient()


@pytest.fixture(scope="session")
def pg_available():
    dsn = os.environ.get("MR_DATA_POSTGRES_DSN", "postgresql://user:password@localhost:5432/mrdata")
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
