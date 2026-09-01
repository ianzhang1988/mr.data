"""Regression tests for DialogueGraph state handling.

Covers:
- Removal of the state reducers (web-doc filtering must not be undone by a
  merge-by-id union, and loaded dialogue messages must not be duplicated
  across linear graph nodes).
- ``selected_dimension_ids`` taking effect in prompt assembly and dialogue
  dimension-ref logging.
"""

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore
from mr_data.models import DialogueLog
from mr_data.online import DialogueGraph


class FakeWebSearch:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def search(self, query: str) -> list[dict]:
        return self._docs


def _force_web_search(system_prompt, user_prompt, response_format, temperature=0.2):
    return {
        "inner_monologue": "需要搜索",
        "personality_query": user_prompt,
        "memory_query": user_prompt,
        "needs_web_search": True,
        "search_query": user_prompt,
    }


def _capture_fit_sections(graph, monkeypatch):
    """Patch graph.prompt_assembler.fit_sections to capture the raw sections."""
    captured: dict = {}
    original_fit = graph.prompt_assembler.fit_sections

    def spy(sections, limit):
        captured["sections"] = sections
        return original_fit(sections, limit)

    monkeypatch.setattr(graph.prompt_assembler, "fit_sections", spy)
    return captured


def test_web_doc_filtering_not_undone_by_state_merge(
    fake_llm, test_session_id, pg_available, chroma_store, temp_log_dir, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)

    kept_id = "web:kept"
    dropped_id = "web:dropped"
    docs = [
        {
            "id": kept_id,
            "page_content": "太阳系有八大行星",
            "metadata": {
                "source_type": "web",
                "url": "http://example.com/planets",
                "title": "行星",
            },
        },
        {
            "id": dropped_id,
            "page_content": "完全无关的蛋糕食谱",
            "metadata": {
                "source_type": "web",
                "url": "http://example.com/cake",
                "title": "蛋糕",
            },
        },
    ]

    original_chat_structured = fake_llm.chat_structured

    def _patched_chat_structured(system_prompt, user_prompt, response_format, temperature=0.2):
        name = response_format.__name__
        if name == "ThinkDecision":
            return _force_web_search(system_prompt, user_prompt, response_format, temperature)
        if name == "WebDocExtraction":
            if "蛋糕" in user_prompt:
                return {"is_relevant": False, "extracted_text": ""}
            return {"is_relevant": True, "extracted_text": ""}
        return original_chat_structured(system_prompt, user_prompt, response_format, temperature)

    monkeypatch.setattr(fake_llm, "chat_structured", _patched_chat_structured)
    monkeypatch.setattr(settings, "enable_web_doc_extraction", True)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        web_search=FakeWebSearch(docs),
        enable_web_search=True,
    )

    reply = graph.chat(test_session_id, "太阳系有几颗行星")
    assert reply

    # The irrelevant doc must stay filtered out of the logged web vector refs.
    with pg._cursor() as cur:
        cur.execute(
            """
            SELECT r.vector_doc_id
            FROM dialogue_vector_refs r
            JOIN dialogue_logs l ON l.id = r.dialogue_log_id
            WHERE l.session_id = %s AND r.source_type = 'web'
            """,
            (test_session_id,),
        )
        web_ref_ids = {row["vector_doc_id"] for row in cur.fetchall()}

    assert web_ref_ids == {kept_id}


def test_history_messages_not_duplicated(
    fake_llm, test_session_id, pg_available, chroma_store, temp_log_dir, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)

    # Seed one history turn directly in Postgres.
    pg.insert_dialogue(
        DialogueLog(session_id=test_session_id, role="user", content="历史问题一")
    )
    pg.insert_dialogue(
        DialogueLog(
            session_id=test_session_id,
            role="assistant",
            content="历史回答一",
            metadata={"inner_monologue": None, "blocks": []},
        )
    )

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )
    captured = _capture_fit_sections(graph, monkeypatch)

    reply = graph.chat(test_session_id, "新问题")
    assert reply

    sections = captured.get("sections")
    assert sections, "fit_sections should have been called"
    messages_section = next(s for s in sections if s.name == "messages")

    # Each history message must appear exactly once in the assembled prompt.
    assert messages_section.text.count("历史问题一") == 1
    assert messages_section.text.count("历史回答一") == 1


def test_log_dialogue_records_only_selected_dimensions(
    fake_llm, test_session_id, pg_available, chroma_store, temp_log_dir
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)

    dimensions = pg.list_dimensions(active_only=True)
    assert len(dimensions) >= 2
    all_ids = [d.id for d in dimensions if d.id is not None]
    selected = all_ids[:1]
    unselected = set(all_ids[1:])

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )

    state = {
        "session_id": test_session_id,
        "user_input": "测试输入",
        "reply": "测试回复",
        "reply_blocks": [],
        "dimensions": dimensions,
        "selected_dimension_ids": selected,
        "personality_docs": [],
        "web_docs": [],
        "inner_monologue": None,
    }
    result = graph._log_dialogue(state)
    assistant_log_id = result["assistant_log_id"]

    with pg._cursor() as cur:
        cur.execute(
            "SELECT dimension_id FROM dialogue_dimension_refs WHERE dialogue_log_id = %s",
            (assistant_log_id,),
        )
        ref_ids = {row["dimension_id"] for row in cur.fetchall()}

    assert ref_ids == set(selected)
    assert not (ref_ids & unselected)


def test_assemble_uses_only_selected_dimensions(
    fake_llm, test_session_id, pg_available, chroma_store, temp_log_dir, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)

    dimensions = pg.list_dimensions(active_only=True)
    assert len(dimensions) >= 2
    selected_dim = dimensions[0]
    unselected_dims = [d for d in dimensions[1:] if d.id is not None]

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )
    captured = _capture_fit_sections(graph, monkeypatch)

    state = {
        "session_id": test_session_id,
        "user_input": "测试输入",
        "identity": pg.get_identity(),
        "user_identity": None,
        "dimensions": dimensions,
        "selected_dimension_ids": [selected_dim.id],
        "personality_docs": [],
        "memory_docs": [],
        "messages": [],
        "web_docs": [],
        "inner_monologue": None,
    }
    graph._assemble_and_generate(state)

    sections = captured.get("sections")
    assert sections, "fit_sections should have been called"
    identity_section = next(s for s in sections if s.name == "identity")

    assert selected_dim.description in identity_section.text
    for dim in unselected_dims:
        assert dim.description not in identity_section.text
