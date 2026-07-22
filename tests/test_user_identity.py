import uuid

import pytest

from mr_data.db import PostgresStore
from mr_data.llm import LLMClient
from mr_data.online import DialogueGraph


class RecordingFakeLLM(LLMClient):
    """Fake LLM that records the last system prompt."""

    def __init__(self):
        super().__init__(base_url="http://fake", api_key="fake", model="fake")
        self.last_system: str = ""
        self.last_prompt: str = ""

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        self.last_system = system_prompt
        self.last_prompt = user_prompt
        if "性格维度选择助手" in system_prompt:
            return '{"dimension_ids": [1]}'
        if "检索查询" in system_prompt and "内心独白" in system_prompt:
            return "检索查询：用户查询改写 内心独白：这是一个测试内心独白"
        return "这是一个测试回复。"

    def chat_structured(self, system_prompt, user_prompt, response_format, temperature=0.2):
        return {"deltas": []}


@pytest.fixture
def recording_fake_llm():
    return RecordingFakeLLM()


def test_seed_user_identities(pg_available):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    identities = pg.list_user_identities()
    assert len(identities) >= 2

    picard = next((i for i in identities if i.name == "Picard"), None)
    assert picard is not None
    assert picard.is_default is True
    assert picard.is_protected is True
    assert "舰长" in picard.description
    assert "Enterprise-D" in picard.description
    assert "Earl Grey" in picard.description

    normal = next((i for i in identities if i.name == "User"), None)
    assert normal is not None
    assert normal.is_protected is True
    assert normal.is_default is False


def test_protected_identity_cannot_be_deleted(pg_available):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    with pytest.raises(ValueError):
        pg.delete_user_identity("Picard")


def test_user_identity_crud(pg_available):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    unique_name = f"test-user-{uuid.uuid4().hex[:8]}"
    identity_id = pg.insert_user_identity(
        name=unique_name,
        role="测试角色",
        description="测试身份说明",
        is_default=False,
        is_protected=False,
    )
    assert identity_id > 0

    identity = pg.get_user_identity(unique_name)
    assert identity is not None
    assert identity.name == unique_name
    assert identity.role == "测试角色"

    assert pg.update_user_identity(unique_name, role="更新角色")
    updated = pg.get_user_identity(str(identity_id))
    assert updated is not None
    assert updated.role == "更新角色"

    assert pg.set_default_user_identity(str(identity_id)) is True
    current = pg.get_current_user_identity()
    assert current is not None
    assert current.id == identity_id

    assert pg.delete_user_identity(str(identity_id)) is True
    assert pg.get_user_identity(str(identity_id)) is None

    # Restore Picard as default to avoid leaking state to other tests.
    pg.set_default_user_identity("Picard")


def test_user_identity_in_system_prompt(
    pg_available, chroma_store, recording_fake_llm, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.set_default_user_identity("Picard")
    pg.create_session(test_session_id)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=recording_fake_llm,
        enable_web_search=False,
    )
    reply = graph.chat(test_session_id, "你好")
    assert reply

    system = recording_fake_llm.last_system
    assert "Picard" in system
    assert "舰长" in system
    assert "Enterprise-D" in system


def test_chat_help_output(pg_available, capsys):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    from mr_data.cli import _print_chat_help

    _print_chat_help(pg)
    captured = capsys.readouterr()
    output = captured.out
    assert "/help" in output
    assert "/newsession" in output
    assert "/exit" in output
    assert "/quit" in output
    assert "/bye" in output
    assert "identity list" in output
    assert "当前用户身份" in output
