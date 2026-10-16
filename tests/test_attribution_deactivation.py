"""性格维度淘汰规则测试（比率 + 最小样本量）。

覆盖：
- 失败数高但成功数足够高 → 比率不达标，不停用；
- 失败比率达阈值且总样本量达标 → 停用并清理向量；
- 总样本量不足 → 即使比率 100% 也不停用；
- 核心维度豁免停用。
"""

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore
from mr_data.models import DialogueLog, DialogueVectorRef, PersonalityEvent
from mr_data.offline import AttributionEngine

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _setup_closed_session(pg: PostgresStore, session_id: str) -> int:
    pg.create_session(session_id)
    pg.insert_dialogue(DialogueLog(session_id=session_id, role="user", content="测试输入"))
    assistant_id = pg.insert_dialogue(
        DialogueLog(session_id=session_id, role="assistant", content="测试回复")
    )
    pg.close_session(session_id)
    return assistant_id


def _seed_evidence(pg: PostgresStore, chroma_store, session_id: str):
    dummy_id = pg.insert_dialogue(
        DialogueLog(session_id=session_id, role="assistant", content="legacy")
    )
    event = PersonalityEvent(
        content="即将被清理的旧证据",
        dimension_ids=[1],
        source_type="evidence",
        source_id="legacy",
    )
    doc_id = chroma_store.add_personality_event(event)
    pg.insert_dialogue_vector_refs(
        dummy_id,
        [
            DialogueVectorRef(
                dialogue_log_id=dummy_id,
                vector_doc_id=doc_id,
                source_type="evidence",
                content=event.content,
                dimension_ids=[1],
            )
        ],
    )
    return doc_id


def test_high_success_count_avoids_deactivation(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    with pg._cursor(commit=True) as cur:
        cur.execute(
            "UPDATE personality_dimensions SET core = FALSE, success_count = 1000, failure_count = 5 WHERE id = 1"
        )

    _setup_closed_session(pg, test_session_id)
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    engine.run()

    dim = pg.get_dimension(1)
    assert dim is not None
    assert dim.active is True


def test_ratio_and_min_samples_trigger_deactivation_and_purge(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)

    doc_id = _seed_evidence(pg, chroma_store, test_session_id)

    with pg._cursor(commit=True) as cur:
        cur.execute(
            "UPDATE personality_dimensions SET core = FALSE, success_count = 0, failure_count = %s WHERE id = 1",
            (settings.failure_min_samples,),
        )

    _setup_closed_session(pg, test_session_id)
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    engine.run()

    dim = pg.get_dimension(1)
    assert dim is not None
    assert dim.active is False
    assert len(pg.get_dialogue_vector_refs_by_dimension(1)) == 0
    assert doc_id not in chroma_store.get_personality_event_ids_by_dimension(1)


def test_insufficient_samples_avoid_deactivation(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    with pg._cursor(commit=True) as cur:
        cur.execute(
            "UPDATE personality_dimensions SET core = FALSE, success_count = 0, failure_count = 2 WHERE id = 1"
        )

    _setup_closed_session(pg, test_session_id)
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    engine.run()

    dim = pg.get_dimension(1)
    assert dim is not None
    assert dim.active is True


def test_core_dimension_exempt_from_deactivation(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    with pg._cursor(commit=True) as cur:
        cur.execute(
            "UPDATE personality_dimensions SET core = TRUE, success_count = 0, failure_count = %s WHERE id = 1",
            (settings.failure_min_samples,),
        )

    _setup_closed_session(pg, test_session_id)
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    engine.run()

    dim = pg.get_dimension(1)
    assert dim is not None
    assert dim.core is True
    assert dim.active is True
