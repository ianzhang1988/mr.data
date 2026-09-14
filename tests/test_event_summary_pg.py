"""离线归因 event_summary 写入 PG adjustment_logs 测试（改进项 50）。

覆盖：
- 含 event_summary 的 delta：adjustment_logs.event_summary 读到对应文本；
- 不含 event_summary 的 delta：该列为 NULL。
"""

import pytest

from mr_data.db import PostgresStore
from mr_data.models import DialogueLog
from mr_data.offline import AttributionEngine
from mr_data.offline.attribution import AttributionResult, DimensionDelta

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _setup(pg: PostgresStore, session_id: str):
    pg.init_schema()
    pg.seed()
    pg.create_session(session_id)
    pg.insert_dialogue(DialogueLog(session_id=session_id, role="user", content="测试输入"))
    pg.insert_dialogue(
        DialogueLog(session_id=session_id, role="assistant", content="测试回复")
    )
    pg.close_session(session_id)
    return pg.get_recent_dialogues(session_id=session_id)


def _fetch_event_summaries(pg: PostgresStore) -> list:
    with pg._cursor() as cur:
        cur.execute("SELECT event_summary FROM adjustment_logs")
        return [row["event_summary"] for row in cur.fetchall()]


def test_apply_persists_event_summary_to_adjustment_logs(
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs = _setup(pg, test_session_id)

    dim_id = pg.insert_dimension("事件摘要测试维度")
    result = AttributionResult(
        deltas=[
            DimensionDelta(
                dimension_id=dim_id,
                delta_success=1,
                reason="测试",
                event_summary="值得记录的事件",
                target_dialogue_log_id=logs[-1].id,
            )
        ]
    )
    engine = AttributionEngine(
        pg_store=pg, chroma_store=chroma_store, llm=fake_llm
    )

    engine._apply(result, test_session_id, logs)

    assert _fetch_event_summaries(pg) == ["值得记录的事件"]


def test_apply_without_event_summary_persists_null(
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs = _setup(pg, test_session_id)

    dim_id = pg.insert_dimension("无事件摘要测试维度")
    result = AttributionResult(
        deltas=[
            DimensionDelta(
                dimension_id=dim_id,
                delta_success=1,
                reason="测试",
                target_dialogue_log_id=logs[-1].id,
            )
        ]
    )
    engine = AttributionEngine(
        pg_store=pg, chroma_store=chroma_store, llm=fake_llm
    )

    engine._apply(result, test_session_id, logs)

    assert _fetch_event_summaries(pg) == [None]
