"""离线归因 LLM 幻觉 id 防护测试（改进项 54）。

覆盖：
- transcript 拼行带日志 id（assistant[#id] 格式）；
- 幻觉 target_dialogue_log_id（不存在 / 跨会话 / 指向 user 行）→ 置 None，
  adjustment 落库 dialogue_log_id=NULL，evidence/event 跳过不写向量库；
- 幻觉 dimension_id → 置 None 且 delta 丢弃，不触发外键违规；
- 无 target + 有 evidence/event_summary → 跳过 Chroma 写入（无 "None" 污染）；
- 合法 target → evidence 正常入库 + vector_refs 落库（回归保护）。
"""

import pytest

from mr_data.db import PostgresStore
from mr_data.models import DialogueLog
from mr_data.offline import AttributionEngine

from test_attribution_dedup import DedupFakeLLM

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _setup_session(pg: PostgresStore, session_id: str):
    pg.create_session(session_id)
    user_id = pg.insert_dialogue(
        DialogueLog(session_id=session_id, role="user", content="测试输入")
    )
    assistant_id = pg.insert_dialogue(
        DialogueLog(session_id=session_id, role="assistant", content="测试回复")
    )
    logs = pg.get_recent_dialogues(session_id=session_id)
    return logs, user_id, assistant_id


def _attribute_and_apply(pg, chroma_store, llm, session_id, logs):
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=llm)
    result = engine._attribute_session(session_id, logs)
    assert result is not None
    engine._apply(result, session_id, logs)
    return result


def _fetch_adjustments(pg: PostgresStore) -> list:
    with pg._cursor() as cur:
        cur.execute("SELECT dimension_id, dialogue_log_id FROM adjustment_logs")
        return cur.fetchall()


def _delta(**overrides):
    delta = {
        "dimension_id": 1,
        "delta_success": 1,
        "delta_failure": 0,
        "reason": "测试归因原因",
        "evidence_snippets": ["assistant 的测试回复"],
        "event_summary": "测试事件摘要",
    }
    delta.update(overrides)
    return delta


def test_transcript_lines_carry_log_ids(
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs, user_id, assistant_id = _setup_session(pg, test_session_id)

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    transcript = engine._build_transcript(logs)

    assert f"user[#{user_id}]: 测试输入" in transcript
    assert f"assistant[#{assistant_id}]: 测试回复" in transcript


def test_hallucinated_target_id_nulled_and_evidence_skipped(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs, _, _ = _setup_session(pg, test_session_id)
    llm = DedupFakeLLM(deltas=[_delta(target_dialogue_log_id=999)])

    result = _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert result.deltas[0].target_dialogue_log_id is None
    rows = _fetch_adjustments(pg)
    assert len(rows) == 1 and rows[0]["dialogue_log_id"] is None
    # evidence 与 event 均跳过，不产生 "None" 污染文档
    assert chroma_store.get_personality_event_ids_by_dimension(1) == []


def test_target_id_from_other_session_nulled(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs, _, _ = _setup_session(pg, test_session_id)
    other_session = f"{test_session_id}-other"
    _, _, other_assistant_id = _setup_session(pg, other_session)

    llm = DedupFakeLLM(deltas=[_delta(target_dialogue_log_id=other_assistant_id)])
    result = _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert result.deltas[0].target_dialogue_log_id is None
    rows = _fetch_adjustments(pg)
    assert len(rows) == 1 and rows[0]["dialogue_log_id"] is None
    assert chroma_store.get_personality_event_ids_by_dimension(1) == []


def test_target_id_pointing_to_user_row_nulled(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs, user_id, _ = _setup_session(pg, test_session_id)
    llm = DedupFakeLLM(deltas=[_delta(target_dialogue_log_id=user_id)])

    result = _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert result.deltas[0].target_dialogue_log_id is None
    rows = _fetch_adjustments(pg)
    assert len(rows) == 1 and rows[0]["dialogue_log_id"] is None


def test_hallucinated_dimension_id_discarded_without_fk_violation(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs, _, assistant_id = _setup_session(pg, test_session_id)
    llm = DedupFakeLLM(
        deltas=[_delta(dimension_id=999, target_dialogue_log_id=assistant_id)]
    )

    result = _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert result.deltas[0].dimension_id is None
    assert _fetch_adjustments(pg) == []  # delta 被丢弃，无 FK 违规炸事务


def test_valid_target_persists_evidence_and_refs(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs, _, assistant_id = _setup_session(pg, test_session_id)
    llm = DedupFakeLLM(deltas=[_delta(target_dialogue_log_id=assistant_id)])

    _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    rows = _fetch_adjustments(pg)
    assert len(rows) == 1 and rows[0]["dialogue_log_id"] == assistant_id
    # evidence + event 两个文档入库
    assert len(chroma_store.get_personality_event_ids_by_dimension(1)) == 2
    refs = pg.get_dialogue_vector_refs_by_dimension(1)
    assert len(refs) == 1 and refs[0].dialogue_log_id == assistant_id
