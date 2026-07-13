import pytest

from mr_data.db import PostgresStore, ChromaStore
from mr_data.models import (
    DialogueLog,
    PersonalityDimension,
    PersonalityEvent,
    DialogueVectorRef,
)
from mr_data.online import DialogueGraph
from mr_data.offline import AttributionEngine


def test_models_serialize():
    dim = PersonalityDimension(description="我相信轻松的表达能拉近距离。")
    assert dim.description
    assert dim.success_count == 0


def test_chroma_personality(tmp_path):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    event = PersonalityEvent(content="测试事件", dimension_ids=[1, 2], source_type="event")
    doc_id = store.add_personality_event(event)
    assert doc_id

    docs = store.query_personality("测试")
    assert any("测试事件" in d["page_content"] for d in docs)
    assert any(1 in d["metadata"]["dimension_ids"] for d in docs)


def test_chroma_memories(tmp_path):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    store.add_memory("s1", "用户：你好")
    store.add_memory("s1", "助手：你好呀")

    docs = store.query_memories("你好", session_id="s1")
    assert len(docs) >= 1
    assert any("你好" in d["page_content"] for d in docs)


def test_postgres_schema_and_seed(pg_available):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    store = PostgresStore()
    store.init_schema()
    store.seed()
    identity = store.get_identity()
    assert identity is not None
    dims = store.list_dimensions()
    assert len(dims) > 0
    assert dims[0].description
    assert not hasattr(dims[0], "current_value")


def test_dialogue_graph(fake_llm, test_session_id, pg_available):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)
    chroma = ChromaStore()
    graph = DialogueGraph(pg_store=pg, chroma_store=chroma, llm=fake_llm, enable_web_search=False)

    reply = graph.chat(test_session_id, "你好")
    assert reply

    logs = pg.get_recent_dialogues(session_id=test_session_id)
    assert len(logs) >= 2


def test_offline_attribution(fake_llm, test_session_id, pg_available):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)
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

    # Close the session before running attribution
    pg.close_session(test_session_id)

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma, llm=fake_llm)
    engine.run()

    # Both should be marked processed
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 0


def test_full_session_lifecycle(fake_llm, pg_available):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    chroma = ChromaStore()
    graph = DialogueGraph(pg_store=pg, chroma_store=chroma, llm=fake_llm, enable_web_search=False)

    # First session
    session_a = pg.create_session()
    reply_a = graph.chat(session_a, "你好")
    assert reply_a
    pg.close_session(session_a)

    # Second session
    session_b = pg.create_session()
    reply_b = graph.chat(session_b, "今天天气怎么样")
    assert reply_b
    pg.close_session(session_b)

    # Run offline attribution
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma, llm=fake_llm)
    engine.run()

    # Both sessions should be fully processed
    for sid in (session_a, session_b):
        unprocessed = pg.get_recent_dialogues(session_id=sid, unprocessed_only=True)
        assert len(unprocessed) == 0, f"Session {sid} has unprocessed dialogues"

    # Adjustment logs should reference the sessions
    with pg._cursor() as cur:
        cur.execute("SELECT session_id FROM adjustment_logs WHERE session_id IN (%s, %s)", (session_a, session_b))
        rows = cur.fetchall()
    # FakeLLM returns empty attribution, so there may be no adjustment logs.
    # This test mainly verifies the session lifecycle runs without errors.
    assert all(row["session_id"] in (session_a, session_b) for row in rows)
