"""离线归因 PG 侧崩溃重放幂等测试（会话级事务）。

覆盖：
- _apply 中途崩溃 → 事务回滚：维度计数不变、adjustment_logs 无新增、对话仍 unprocessed；
- 崩溃恢复后重跑 → 只应用一次，不重复；
- post-commit Chroma 写入失败（purge 清理 / add 新证据）不回滚已提交的 PG 会话；
- 非事务态自动 commit 行为回归；
- 嵌套事务被拒绝。
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

def _adjustment_count(pg: PostgresStore) -> int:
    with pg._cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM adjustment_logs")
        return cur.fetchone()["c"]

def test_apply_crash_rolls_back_and_retry_applies_once(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
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
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
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
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
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
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()

    dim = pg.get_dimension(1)
    pg.update_dimension(1, delta_success=2, delta_failure=1)
    # 无事务上下文：逐方法自动 commit，立即对外可见。
    dim_after = pg.get_dimension(1)
    assert dim_after.success_count == dim.success_count + 2
    assert dim_after.failure_count == dim.failure_count + 1

def test_nested_transaction_rejected(pg_available):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    with pg.transaction():
        with pytest.raises(RuntimeError):
            with pg.transaction():
                pass

def test_rollback_does_not_touch_chroma(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    # Pre-seed an evidence doc tied to dimension 1 so we can verify rollback leaves it alone.
    dummy_id = pg.insert_dialogue(
        DialogueLog(session_id=test_session_id, role="assistant", content="legacy")
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

    with pg._cursor(commit=True) as cur:
        cur.execute(
            "UPDATE personality_dimensions SET core = FALSE, success_count = 0, failure_count = %s WHERE id = 1",
            (settings.failure_min_samples,),
        )

    assistant_id = _setup_closed_session(pg, test_session_id)

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)

    def _failing_insert_adjustment(adj):
        raise RuntimeError("模拟 _apply 中途崩溃")

    monkeypatch.setattr(pg, "insert_adjustment", _failing_insert_adjustment)

    engine.run()

    dim = pg.get_dimension(1)
    assert dim is not None
    assert dim.active is True
    assert len(pg.get_dialogue_vector_refs_by_dimension(1)) > 0
    assert doc_id in chroma_store.get_personality_event_ids_by_dimension(1)

    monkeypatch.undo()
    engine.run()

    dim = pg.get_dimension(1)
    assert dim is not None
    assert dim.active is False
    assert len(pg.get_dialogue_vector_refs_by_dimension(1)) == 0
    assert doc_id not in chroma_store.get_personality_event_ids_by_dimension(1)

@pytest.mark.parametrize("failing_chroma_op", ["purge", "add"])
def test_purge_failure_does_not_fail_committed_session(
    failing_chroma_op, fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    """post-commit Chroma 写入失败（purge 清理 / add 新证据）不回滚已提交的 PG 会话（改进 55）。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    dummy_id = pg.insert_dialogue(
        DialogueLog(session_id=test_session_id, role="assistant", content="legacy")
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

    with pg._cursor(commit=True) as cur:
        cur.execute(
            "UPDATE personality_dimensions SET core = FALSE, success_count = 0, failure_count = %s WHERE id = 1",
            (settings.failure_min_samples,),
        )

    assistant_id = _setup_closed_session(pg, test_session_id)

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)

    if failing_chroma_op == "purge":
        def _failing_delete_personality_events(doc_ids):
            raise RuntimeError("模拟 Chroma purge 失败")

        monkeypatch.setattr(chroma_store, "delete_personality_events", _failing_delete_personality_events)
    else:
        # 改进 54 移除了 target fallback：需显式给归因 target 才会走到 evidence/event 的 Chroma 写入。
        fake_llm.attribution_target_log_id = assistant_id

        def _failing_add_personality_event(event):
            raise RuntimeError("模拟 Chroma 写入失败")

        monkeypatch.setattr(chroma_store, "add_personality_event", _failing_add_personality_event)

    engine.run()

    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 0
    dim = pg.get_dimension(1)
    assert dim is not None
    if failing_chroma_op == "purge":
        assert dim.active is False
    assert _adjustment_count(pg) == 1
