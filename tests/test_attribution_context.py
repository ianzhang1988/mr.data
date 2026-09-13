"""离线归因 _build_context / _build_transcript 重构测试（改进项 51）。

覆盖：
- 性格维度按「本次会话激活 / 其余活跃」分组渲染，不再包含人格素材与思考过程段落；
- 无维度引用记录的会话：激活组为空占位，全部维度进其余组；
- assistant 行的内心独白并入 transcript；
- list_session_dimension_ids 跨会话隔离。
"""

import pytest

from mr_data.db import PostgresStore
from mr_data.models import DialogueLog, DialogueLogMetadata
from mr_data.offline import AttributionEngine

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _make_engine(pg: PostgresStore, chroma_store, fake_llm, temp_log_dir) -> AttributionEngine:
    return AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm, log_dir=temp_log_dir)


def _insert_turn(pg: PostgresStore, session_id: str, monologue: str | None = None) -> int:
    pg.create_session(session_id)
    pg.insert_dialogue(DialogueLog(session_id=session_id, role="user", content="测试输入"))
    return pg.insert_dialogue(
        DialogueLog(
            session_id=session_id,
            role="assistant",
            content="测试回复",
            metadata=DialogueLogMetadata(inner_monologue=monologue) if monologue else None,
        )
    )


def test_context_splits_dimensions_by_session_activation(
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    assistant_id = _insert_turn(pg, test_session_id)
    pg.insert_dialogue_dimension_refs(assistant_id, [1, 3])

    engine = _make_engine(pg, chroma_store, fake_llm, temp_log_dir)
    context = engine._build_context(test_session_id)

    activated_section = context.split("本次会话中实际激活的性格维度：")[1].split("其余活跃的性格维度")[0]
    remaining_section = context.split("其余活跃的性格维度")[1]
    assert "[1]" in activated_section and "[3]" in activated_section
    assert "[2]" in remaining_section and "[4]" in remaining_section and "[5]" in remaining_section
    assert "[1]" not in remaining_section and "[3]" not in remaining_section
    # 已移除的段落
    assert "人格素材" not in context
    assert "思考过程" not in context


def test_context_without_dimension_refs_puts_all_in_remaining(
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    _insert_turn(pg, test_session_id)

    engine = _make_engine(pg, chroma_store, fake_llm, temp_log_dir)
    context = engine._build_context(test_session_id)

    activated_section = context.split("本次会话中实际激活的性格维度：")[1].split("其余活跃的性格维度")[0]
    assert "（无记录）" in activated_section
    remaining_section = context.split("其余活跃的性格维度")[1]
    for dim_id in range(1, 6):
        assert f"[{dim_id}]" in remaining_section


def test_transcript_inlines_inner_monologue(
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    _insert_turn(pg, test_session_id, monologue="用户似乎在测试我")
    logs = pg.get_recent_dialogues(session_id=test_session_id)

    engine = _make_engine(pg, chroma_store, fake_llm, temp_log_dir)
    transcript = engine._build_transcript(logs)

    assert "assistant（内心独白：用户似乎在测试我）: 测试回复" in transcript
    assert "user: 测试输入" in transcript


def test_list_session_dimension_ids_isolated_across_sessions(
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    assistant_a = _insert_turn(pg, test_session_id)
    pg.insert_dialogue_dimension_refs(assistant_a, [1, 3])
    other_session = f"{test_session_id}-other"
    assistant_b = _insert_turn(pg, other_session)
    pg.insert_dialogue_dimension_refs(assistant_b, [2])

    assert pg.list_session_dimension_ids(test_session_id) == [1, 3]
    assert pg.list_session_dimension_ids(other_session) == [2]
    assert pg.list_session_dimension_ids("no-such-session") == []
