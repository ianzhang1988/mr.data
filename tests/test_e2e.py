import os

import pytest

from mr_data.db import PostgresStore, ChromaStore
from mr_data.models import DialogueLog, PersonalityDimension, PersonalityEvent
from mr_data.online import DialogueGraph
from mr_data.offline import AttributionEngine


def test_models_serialize():
    dim = PersonalityDimension(name="幽默感", current_value=0.5)
    assert dim.name == "幽默感"
    assert -1.0 <= dim.current_value <= 1.0


def test_chroma_personality(tmp_path):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    event = PersonalityEvent(content="测试事件", dimension_tags=["幽默感"], source_type="event")
    doc_id = store.add_personality_event(event)
    assert doc_id

    docs = store.query_personality("测试")
    assert any("测试事件" in d["page_content"] for d in docs)


def test_chroma_memories(tmp_path):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    store.add_memory("s1", "用户：你好")
    store.add_memory("s1", "助手：你好呀")

    docs = store.query_memories("你好", session_id="s1")
    assert len(docs) >= 1
    assert any("你好" in d["page_content"] for d in docs)


@pytest.mark.skipif(
    not os.environ.get("MR_DATA_POSTGRES_DSN"),
    reason="Set MR_DATA_POSTGRES_DSN to run PG integration tests.",
)
def test_postgres_schema_and_seed():
    store = PostgresStore()
    store.init_schema()
    store.seed()
    identity = store.get_identity()
    assert identity is not None
    dims = store.list_dimensions()
    assert len(dims) > 0


@pytest.mark.skipif(
    not os.environ.get("MR_DATA_POSTGRES_DSN"),
    reason="Set MR_DATA_POSTGRES_DSN to run PG integration tests.",
)
def test_dialogue_graph(fake_llm, test_session_id):
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    chroma = ChromaStore()
    graph = DialogueGraph(pg_store=pg, chroma_store=chroma, llm=fake_llm)

    reply = graph.chat(test_session_id, "你好")
    assert reply

    logs = pg.get_recent_dialogues(session_id=test_session_id)
    assert len(logs) >= 2


@pytest.mark.skipif(
    not os.environ.get("MR_DATA_POSTGRES_DSN"),
    reason="Set MR_DATA_POSTGRES_DSN to run PG integration tests.",
)
def test_offline_attribution(fake_llm, test_session_id):
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    chroma = ChromaStore()

    # Insert a user-assistant pair
    user_id = pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="user", content="测试输入"))
    assistant_id = pg.insert_dialogue(
        DialogueLog(
            session_id=test_session_id,
            role="assistant",
            content="测试回复",
            evaluation_score=1,
            evaluation_feedback="不错",
        )
    )

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma, llm=fake_llm)
    engine.run()

    # Both should be marked processed
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 0
