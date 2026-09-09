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


def _setup_closed_session(pg: PostgresStore, session_id: str) -> None:
    pg.create_session(session_id)
    pg.insert_dialogue(DialogueLog(session_id=session_id, role="user", content="测试输入"))
    pg.insert_dialogue(
        DialogueLog(session_id=session_id, role="assistant", content="测试回复")
    )
    pg.close_session(session_id)


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
        pg_store=pg, chroma_store=chroma_store, llm=fake_llm, log_dir=temp_log_dir
    )

    # 模拟 _apply 中途崩溃：update_dimension 之后 insert_adjustment 抛错。
    def _failing_insert_adjustment(adj):
        raise RuntimeError("模拟 _apply 中途崩溃")

    monkeypatch.setattr(pg, "insert_adjustment", _failing_insert_adjustment)

    with pytest.raises(RuntimeError):
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
    _setup_closed_session(pg, test_session_id)

    dim = pg.get_dimension(1)
    base_success = dim.success_count

    engine = AttributionEngine(
        pg_store=pg, chroma_store=chroma_store, llm=fake_llm, log_dir=temp_log_dir
    )

    # 崩溃点更靠后：计数与审计已写入，仅 vector refs 插入失败。
    def _failing_insert_vector_refs(dialogue_log_id, refs):
        raise RuntimeError("模拟 vector refs 写入崩溃")

    monkeypatch.setattr(pg, "insert_dialogue_vector_refs", _failing_insert_vector_refs)

    with pytest.raises(RuntimeError):
        engine.run()

    dim = pg.get_dimension(1)
    assert dim.success_count == base_success
    assert _adjustment_count(pg) == 0
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 2


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
