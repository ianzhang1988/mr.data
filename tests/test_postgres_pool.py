"""改进 89：PostgresStore 连接池回归测试。

验证 _cursor/transaction 走 psycopg_pool 共享池（按 dsn 进程级缓存）：
- 同 dsn 多实例共享同一池对象
- 事务结束归还连接后可再 checkout（嵌套禁止语义不变由 test_attribution_transaction 兜底）
- 高频 _cursor 下服务端连接数不随调用次数增长
"""

import pytest

from mr_data.db.postgres import PostgresStore, _get_pool

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def test_pool_shared_per_dsn(pg):
    other = PostgresStore(dsn=pg.dsn)
    assert _get_pool(pg.dsn) is _get_pool(other.dsn)


def test_pool_connection_reusable_after_transaction(pg):
    with pg.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    # 事务归还后，非事务 _cursor 仍可正常 checkout（同一池）
    with pg._cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == {"?column?": 1}


def test_pool_bounds_server_connections(pg):
    def server_conn_count() -> int:
        with pg._cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()")
            return cur.fetchone()["n"]

    before = server_conn_count()
    for _ in range(30):
        with pg._cursor() as cur:
            cur.execute("SELECT 1")
    after = server_conn_count()
    # 池化后连接数不应随调用次数线性增长（允许初始 min_size 预热带来的少量新增）
    assert after - before <= 8
