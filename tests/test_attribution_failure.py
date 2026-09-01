import pytest

from mr_data.db import PostgresStore
from mr_data.models import DialogueLog
from mr_data.offline import AttributionEngine


def _setup_closed_session(pg: PostgresStore, session_id: str) -> None:
    pg.create_session(session_id)
    pg.insert_dialogue(DialogueLog(session_id=session_id, role="user", content="测试输入"))
    pg.insert_dialogue(
        DialogueLog(session_id=session_id, role="assistant", content="测试回复")
    )
    pg.close_session(session_id)


def _make_engine(pg, chroma_store, fake_llm, log_dir) -> AttributionEngine:
    return AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm, log_dir=log_dir)


def test_attribution_failure_does_not_mark_processed(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    _setup_closed_session(pg, test_session_id)

    def _failing_structured_chat(system_prompt, user_prompt, response_format, temperature=0.2):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr(fake_llm, "structured_chat", _failing_structured_chat)

    engine = _make_engine(pg, chroma_store, fake_llm, temp_log_dir)
    engine.run()

    # Attribution failed, so the dialogues must stay unprocessed for a retry.
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 2

    # No dialogue memories should have been written for the failed session.
    docs = chroma_store.query_memories("测试", session_id=test_session_id, top_k=10)
    assert not any(d["metadata"].get("source_type") == "dialogue" for d in docs)


def test_attribution_retry_after_failure(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    _setup_closed_session(pg, test_session_id)

    def _failing_structured_chat(system_prompt, user_prompt, response_format, temperature=0.2):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr(fake_llm, "structured_chat", _failing_structured_chat)

    engine = _make_engine(pg, chroma_store, fake_llm, temp_log_dir)
    engine.run()

    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 2

    # LLM recovers with an empty (but valid) attribution result; run again.
    def _empty_deltas(system_prompt, user_prompt, response_format, temperature=0.2):
        return {"deltas": []}

    monkeypatch.setattr(fake_llm, "structured_chat", _empty_deltas)
    engine.run()

    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 0

    # Dialogue memories should now be persisted in chunked form.
    docs = chroma_store.query_memories("测试", session_id=test_session_id, top_k=10)
    dialogue_docs = [d for d in docs if d["metadata"].get("source_type") == "dialogue"]
    assert dialogue_docs
    assert all("chunk_index" in d["metadata"] for d in dialogue_docs)
