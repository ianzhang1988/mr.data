"""改进 93：PostgresStore 事务槽改 thread-local 的并发回归测试。

server 线程池下多线程共享同一 PostgresStore 实例，事务槽必须是线程独立的：
两线程并发事务互不干扰；同线程嵌套仍拒绝。
"""

import threading
import uuid

import pytest


def test_concurrent_transactions_in_different_threads_do_not_interfere(pg):
    """两线程各自持事务重叠执行（barrier 保证重叠），旧实现下第二个
    线程会抛 nested RuntimeError，修复后两边都应正常提交。"""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    sid_a = f"tx-a-{uuid.uuid4().hex[:8]}"
    sid_b = f"tx-b-{uuid.uuid4().hex[:8]}"

    def worker(sid: str):
        try:
            with pg.transaction():
                barrier.wait(timeout=30)  # 两事务在此重叠
                pg.create_session(sid)
        except BaseException as exc:  # noqa: BLE001 - 收集线程内异常供断言
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(sid,)) for sid in (sid_a, sid_b)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"线程内出现异常: {errors!r}"
    assert pg.get_session(sid_a) is not None
    assert pg.get_session(sid_b) is not None


def test_nested_transaction_in_same_thread_still_rejected(pg):
    """同线程嵌套事务仍应拒绝（thread-local 不影响嵌套判定）。"""
    with pg.transaction():
        with pytest.raises(RuntimeError, match="nested"):
            with pg.transaction():
                pass
