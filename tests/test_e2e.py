import json
from pathlib import Path

import pytest

from mr_data.config import settings
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
    dim = PersonalityDimension(description="我相信轻松的表达能拉近距离。", core=True)
    assert dim.description
    assert dim.core is True
    assert dim.success_count == 0


def test_chroma_personality(tmp_path):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    event = PersonalityEvent(content="测试事件", dimension_ids=[1, 2], source_type="event")
    doc_id = store.add_personality_event(event)
    assert doc_id

    docs = store.query_personality("测试")
    assert any("测试事件" in d["page_content"] for d in docs)
    assert any(1 in d["metadata"]["dimension_ids"] for d in docs)


def test_chroma_personality_context(tmp_path):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    event = PersonalityEvent(
        content=" assistant: 这是 mr.data 的台词",
        context="user: 我们遇到了一个异常值\nassistant: 这是 mr.data 的台词",
        speaker="mr.data",
        dimension_ids=[1],
        source_type="line",
    )
    store.add_personality_event(event)

    # Query by context should return the utterance, not the full context.
    docs = store.query_personality("异常值", top_k=5)
    assert any("这是 mr.data 的台词" in d["page_content"] for d in docs)
    assert all("user:" not in d["page_content"] for d in docs)
    assert docs[0]["metadata"].get("context")


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


def test_dialogue_graph(fake_llm, test_session_id, pg_available, tmp_path, temp_log_dir):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)
    chroma = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    graph = DialogueGraph(pg_store=pg, chroma_store=chroma, llm=fake_llm, enable_web_search=False)

    reply = graph.chat(test_session_id, "你好")
    assert reply

    logs = pg.get_recent_dialogues(session_id=test_session_id)
    assert len(logs) >= 2


def test_offline_attribution(fake_llm, test_session_id, pg_available, tmp_path, temp_log_dir):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)
    chroma = ChromaStore(persist_dir=str(tmp_path / "chroma"))

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

    # Seed a structured thought log so attribution can read assistant thinking process.
    log_file = Path(temp_log_dir) / "mr-data.log"
    thought_entry = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "level": "INFO",
        "logger": "mr_data.online",
        "event": "think.query_generated",
        "message": "Generated retrieval query",
        "session_id": test_session_id,
        "details": {"query": "测试查询", "inner_monologue": "用户似乎在测试我"},
    }
    log_file.write_text(json.dumps(thought_entry, ensure_ascii=False) + "\n", encoding="utf-8")

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma, llm=fake_llm, log_dir=temp_log_dir)
    engine.run()

    # Both should be marked processed
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 0

    # Evidence should be written to the personality collection and linked via vector refs.
    personality_docs = chroma.query_personality("测试回复", top_k=10)
    assert any("测试回复" in doc["page_content"] for doc in personality_docs)

    refs = pg.get_dialogue_vector_refs_by_dimension(1)
    assert any(ref.dialogue_log_id == assistant_id for ref in refs)


def test_select_dimensions_uses_core_flag(fake_llm, test_session_id, pg_available, tmp_path):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)
    chroma = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    graph = DialogueGraph(pg_store=pg, chroma_store=chroma, llm=fake_llm, enable_web_search=False)

    # Mark dimension 2 as core and run a chat turn.
    with pg._cursor(commit=True) as cur:
        cur.execute("UPDATE personality_dimensions SET core = TRUE WHERE id = 2")

    reply = graph.chat(test_session_id, "你好")
    assert reply

    # Dimension 2 should keep its core flag.
    dim = pg.get_dimension(2)
    assert dim is not None
    assert dim.core is True

    # Reset to avoid leaking core=True into other tests that share the embedded PG.
    with pg._cursor(commit=True) as cur:
        cur.execute("UPDATE personality_dimensions SET core = FALSE WHERE id = 2")


def test_web_docs_written_to_memory(fake_llm, test_session_id, pg_available, tmp_path, monkeypatch):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    class FakeWebSearch:
        def search(self, query: str) -> list[dict]:
            return [
                {
                    "id": "web:0",
                    "page_content": "太阳系有八大行星",
                    "metadata": {
                        "source_type": "web",
                        "url": "http://example.com/planets",
                        "title": "行星",
                    },
                }
            ]

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)
    chroma = ChromaStore(persist_dir=str(tmp_path / "chroma"))

    monkeypatch.setattr(settings, "enable_web_page_extraction", False)
    monkeypatch.setattr(settings, "enable_web_relevance_filter", False)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma,
        llm=fake_llm,
        web_search=FakeWebSearch(),
        enable_web_search=True,
    )
    reply = graph.chat(test_session_id, "太阳系有几颗行星")
    assert reply

    docs = chroma.query_memories("八大行星", session_id=test_session_id, top_k=10)
    assert any("八大行星" in d["page_content"] for d in docs)
    assert any(d["metadata"].get("source_type") == "web" for d in docs)
    assert any(d["metadata"].get("url") == "http://example.com/planets" for d in docs)


def test_full_session_lifecycle(fake_llm, pg_available, tmp_path, temp_log_dir):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    chroma = ChromaStore(persist_dir=str(tmp_path / "chroma"))
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
    assert len(rows) >= 2
    assert all(row["session_id"] in (session_a, session_b) for row in rows)


def test_dimension_failure_threshold_purges_vectors(fake_llm, test_session_id, pg_available, tmp_path):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)
    chroma = ChromaStore(persist_dir=str(tmp_path / "chroma"))

    # Insert a dummy dialogue log to own the pre-seeded evidence ref (FK requirement).
    dummy_id = pg.insert_dialogue(
        DialogueLog(session_id=test_session_id, role="assistant", content="legacy")
    )

    # Pre-seed an evidence doc tied to dimension 1 so we can verify cleanup.
    event = PersonalityEvent(
        content="即将被清理的旧证据",
        dimension_ids=[1],
        source_type="evidence",
        source_id="legacy",
    )
    doc_id = chroma.add_personality_event(event)
    pg.insert_dialogue_vector_refs(
        dummy_id,
        [DialogueVectorRef(
            dialogue_log_id=dummy_id,
            vector_doc_id=doc_id,
            source_type="evidence",
            content=event.content,
            dimension_ids=[1],
        )],
    )

    # Ensure dimension 1 is not core and set it to exactly the threshold so the
    # incoming attribution triggers pruning.
    threshold = settings.failure_threshold
    with pg._cursor(commit=True) as cur:
        cur.execute(
            "UPDATE personality_dimensions SET core = FALSE, failure_count = %s WHERE id = 1",
            (threshold,),
        )

    # Insert a dialogue pair for the closed session.
    pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="user", content="测试输入"))
    assistant_id = pg.insert_dialogue(
        DialogueLog(session_id=test_session_id, role="assistant", content="测试回复")
    )
    pg.close_session(test_session_id)

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma, llm=fake_llm)
    engine.run()

    # Dimension 1 should be deactivated.
    dim = pg.get_dimension(1)
    assert dim is not None
    assert dim.active is False

    # Vector refs tied to dimension 1 should be removed.
    refs = pg.get_dialogue_vector_refs_by_dimension(1)
    assert len(refs) == 0

    # The pre-seeded evidence doc should also be gone from Chroma.
    remaining = chroma.get_personality_event_ids_by_dimension(1)
    assert doc_id not in remaining
