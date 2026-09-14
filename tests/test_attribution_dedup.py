"""离线归因新维度去重与收敛截断测试（改进项 53）。

覆盖：
- 新维度不重合：insert_dimension 落库；
- 新维度重合：改挂既有 dimension_id，不新增维度；
- 去重 LLM 调用异常：按允许新建降级；
- 去重返回幻觉 id：视为不重合，允许新建；
- enable_dimension_dedup=False：跳过 LLM 去重直接新建；
- deltas 超过 offline_max_deltas_per_session：仅前 N 条落库（先去重后截断）。
"""

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore
from mr_data.models import DialogueLog
from mr_data.offline import AttributionEngine

from conftest import FakeLLMClient

pytestmark = pytest.mark.usefixtures("reset_pg_state")

SEEDED_DIMENSIONS = 5  # conftest reset_pg_state 的 seed 维度数


class DedupFakeLLM(FakeLLMClient):
    """可控返回归因结果与去重判断的 FakeLLM。"""

    def __init__(self, deltas, dedup_response=None, dedup_raises=False):
        super().__init__()
        self._deltas = deltas
        self._dedup_response = dedup_response if dedup_response is not None else {"matches": []}
        self._dedup_raises = dedup_raises
        self.dedup_calls = 0

    def structured_chat(self, messages, response_format, temperature=0.2):
        name = response_format.__name__
        if name == "AttributionResult":
            return {"deltas": self._deltas}
        if name == "DimensionDedupResult":
            self.dedup_calls += 1
            if self._dedup_raises:
                raise RuntimeError("dedup llm failed")
            return self._dedup_response
        return super().structured_chat(messages, response_format, temperature)


def _new_dimension_delta(**overrides):
    delta = {
        "dimension_id": None,
        "new_dimension_description": "我在压力下倾向于反复确认细节",
        "new_dimension_reason": "既有维度都不涉及细节确认行为，证据：user: 快点",
        "delta_success": 0,
        "delta_failure": 1,
        "reason": "测试归因原因",
        "evidence_snippets": ["user: 测试输入"],
    }
    delta.update(overrides)
    return delta


def _setup_session(pg: PostgresStore, session_id: str):
    pg.create_session(session_id)
    pg.insert_dialogue(DialogueLog(session_id=session_id, role="user", content="测试输入"))
    pg.insert_dialogue(DialogueLog(session_id=session_id, role="assistant", content="测试回复"))
    return pg.get_recent_dialogues(session_id=session_id)


def _attribute_and_apply(pg, chroma_store, llm, session_id, logs):
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=llm)
    result = engine._attribute_session(session_id, logs)
    assert result is not None
    engine._apply(result, session_id, logs)
    return result


def test_new_dimension_created_when_no_duplicate(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs = _setup_session(pg, test_session_id)
    llm = DedupFakeLLM(deltas=[_new_dimension_delta()])

    _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert llm.dedup_calls == 1
    dimensions = pg.list_dimensions()
    assert len(dimensions) == SEEDED_DIMENSIONS + 1
    assert dimensions[-1].description == "我在压力下倾向于反复确认细节"


def test_duplicate_merges_into_existing_dimension(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs = _setup_session(pg, test_session_id)
    before = pg.get_dimension(2)
    llm = DedupFakeLLM(
        deltas=[_new_dimension_delta(delta_success=1, delta_failure=0)],
        dedup_response={"matches": [{"candidate_index": 0, "matched_dimension_id": 2}]},
    )

    result = _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert len(pg.list_dimensions()) == SEEDED_DIMENSIONS  # 未新增维度
    assert result.deltas[0].dimension_id == 2
    assert result.deltas[0].new_dimension_description is None
    after = pg.get_dimension(2)
    assert after.success_count == before.success_count + 1


def test_dedup_failure_falls_back_to_creating(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs = _setup_session(pg, test_session_id)
    llm = DedupFakeLLM(deltas=[_new_dimension_delta()], dedup_raises=True)

    _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert llm.dedup_calls == 1
    assert len(pg.list_dimensions()) == SEEDED_DIMENSIONS + 1


def test_dedup_hallucinated_id_ignored(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs = _setup_session(pg, test_session_id)
    llm = DedupFakeLLM(
        deltas=[_new_dimension_delta()],
        dedup_response={"matches": [{"candidate_index": 0, "matched_dimension_id": 999}]},
    )

    _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert len(pg.list_dimensions()) == SEEDED_DIMENSIONS + 1


def test_dedup_skipped_when_disabled(
    pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    monkeypatch.setattr(settings, "enable_dimension_dedup", False)
    pg = PostgresStore()
    logs = _setup_session(pg, test_session_id)
    llm = DedupFakeLLM(deltas=[_new_dimension_delta()])

    _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert llm.dedup_calls == 0
    assert len(pg.list_dimensions()) == SEEDED_DIMENSIONS + 1


def test_deltas_truncated_to_max_per_session(
    pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    logs = _setup_session(pg, test_session_id)
    before = pg.get_dimension(1)
    deltas = [
        {
            "dimension_id": 1,
            "delta_success": 1,
            "delta_failure": 0,
            "reason": f"第 {i} 条归因",
        }
        for i in range(settings.offline_max_deltas_per_session + 1)
    ]
    llm = DedupFakeLLM(deltas=deltas)

    result = _attribute_and_apply(pg, chroma_store, llm, test_session_id, logs)

    assert len(result.deltas) == settings.offline_max_deltas_per_session
    assert llm.dedup_calls == 0  # 全部挂既有维度，无候选需要去重
    after = pg.get_dimension(1)
    assert after.success_count == before.success_count + settings.offline_max_deltas_per_session
