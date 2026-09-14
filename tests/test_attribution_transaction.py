"""离线归因 PG 侧崩溃重放幂等测试（会话级事务）。

覆盖：
- _apply 中途崩溃 → 事务回滚：维度计数不变、adjustment_logs 无新增、对话仍 unprocessed；
- 崩溃恢复后重跑 → 只应用一次，不重复；
- 非事务态自动 commit 行为回归；
- 嵌套事务被拒绝。
"""

import pytest

from mr_data.db import PostgresStore
from mr_data.models import DialogueLog
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


def _adjustment_count(pg: PostgresStore) -> int:
    with pg._cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM adjustment_logs")
        return cur.fetchone()["c"]


def test_apply_crash_rolls_back_and_retry_applies_once(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    _setup_closed_session(pg, test_session_id)

    dim = pg.get_dimension(1)
    base_success, base_failure = dim.success_count, dim.failure_count

    engine = AttributionEngine(
        pg_store=pg, chroma_store=chroma_store, llm=fake_llm
    )

    # 模拟 _apply 中途崩溃：update_dimension 之后 insert_adjustment 抛错。
    def _failing_insert_adjustment(adj):
        raise RuntimeError("模拟 _apply 中途崩溃")

    monkeypatch.setattr(pg, "insert_adjustment", _failing_insert_adjustment)

    # 改进 55：run() 会话级容错——单会话 apply 失败不再传播，断言回滚状态。
    engine.run()

    # 事务回滚：维度计数未变化、adjustment_logs 无新增、对话仍 unprocessed。
    dim = pg.get_dimension(1)
    assert dim.success_count == base_success
    assert dim.failure_count == base_failure
    assert _adjustment_count(pg) == 0
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 2

    # 崩溃恢复后重跑：成功且只应用一次。
    monkeypatch.undo()
    engine.run()

    dim = pg.get_dimension(1)
    assert dim.success_count == base_success + 1
    assert dim.failure_count == base_failure
    assert _adjustment_count(pg) == 1
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 0


def test_crash_on_vector_refs_rolls_back_pg_writes(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    assistant_id = _setup_closed_session(pg, test_session_id)
    # 改进 54 移除 fallback 后，需显式给归因 target 才能走到 vector refs 崩溃点。
    fake_llm.attribution_target_log_id = assistant_id

    dim = pg.get_dimension(1)
    base_success = dim.success_count

    engine = AttributionEngine(
        pg_store=pg, chroma_store=chroma_store, llm=fake_llm
    )

    # 崩溃点更靠后：计数与审计已写入，仅 vector refs 插入失败。
    def _failing_insert_vector_refs(dialogue_log_id, refs):
        raise RuntimeError("模拟 vector refs 写入崩溃")

    monkeypatch.setattr(pg, "insert_dialogue_vector_refs", _failing_insert_vector_refs)

    # 改进 55：run() 会话级容错——崩溃会话回滚留待重跑，不再中断传播。
    engine.run()

    dim = pg.get_dimension(1)
    assert dim.success_count == base_success
    assert _adjustment_count(pg) == 0
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 2


def test_failed_session_does_not_abort_batch(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    """改进 55：单会话 apply 崩溃回滚留待重跑，不中断批处理后续会话。"""
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    session_a = test_session_id
    session_b = f"{test_session_id}-b"
    _setup_closed_session(pg, session_a)
    _setup_closed_session(pg, session_b)

    real_insert = pg.insert_adjustment

    def _failing_for_a(adj):
        if adj.session_id == session_a:
            raise RuntimeError("模拟会话 A 写入崩溃")
        return real_insert(adj)

    monkeypatch.setattr(pg, "insert_adjustment", _failing_for_a)

    engine = AttributionEngine(
        pg_store=pg, chroma_store=chroma_store, llm=fake_llm
    )
    engine.run()

    # 会话 B 正常处理完成；会话 A 回滚、对话仍 unprocessed。
    assert _adjustment_count(pg) == 1
    assert pg.get_recent_dialogues(session_id=session_b, unprocessed_only=True) == []
    assert len(pg.get_recent_dialogues(session_id=session_a, unprocessed_only=True)) == 2

    # 恢复后重跑：会话 A 成功，且只应用一次。
    monkeypatch.undo()
    engine.run()
    assert _adjustment_count(pg) == 2
    assert pg.get_recent_dialogues(session_id=session_a, unprocessed_only=True) == []


def test_non_transactional_writes_still_autocommit(pg_available):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    dim = pg.get_dimension(1)
    pg.update_dimension(1, delta_success=2, delta_failure=1)
    # 无事务上下文：逐方法自动 commit，立即对外可见。
    dim_after = pg.get_dimension(1)
    assert dim_after.success_count == dim.success_count + 2
    assert dim_after.failure_count == dim.failure_count + 1


def test_nested_transaction_rejected(pg_available):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    with pg.transaction():
        with pytest.raises(RuntimeError, match="nested transactions"):
            with pg.transaction():
                pass
