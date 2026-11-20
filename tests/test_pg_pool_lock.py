"""F-2：_get_pool 并发首次建池的线程安全回归测试。

验证查-建-放整体持锁：两个线程经 Barrier 同时首次 _get_pool(同一 dsn)，
只建出一个池对象。用 FakePool 顶替真实 ConnectionPool，避免起 pgembed。
"""

import threading

from mr_data.db import postgres


class _FakePool:
    """记录 open 次数的 ConnectionPool 替身（线程安全由 _get_pool 的锁保证）。"""

    open_count = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    def open(self):
        type(self).open_count += 1

    def close(self):
        self.closed = True


def test_get_pool_concurrent_first_call_builds_single_pool(monkeypatch):
    monkeypatch.setattr(postgres, "ConnectionPool", _FakePool)
    _FakePool.open_count = 0
    dsn = "postgresql://pool-lock-test/db"

    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(postgres._get_pool(dsn))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results[0] is results[1]
    assert _FakePool.open_count == 1
    assert postgres._POOLS[dsn] is results[0]

    # 清理进程级共享池，避免污染其他用例
    pool = postgres._POOLS.pop(dsn)
    pool.close()
