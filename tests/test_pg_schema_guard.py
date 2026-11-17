import re

from conftest import _PG_BUSINESS_TABLES
from mr_data.db.postgres import SCHEMA_SQL


def test_pg_business_tables_match_schema():
    """_PG_BUSINESS_TABLES 与 SCHEMA_SQL 双写守护（审计 §2-g）。

    reset_pg_state 的 TRUNCATE 清单必须与建表集合精确一致：
    少了会漏清导致跨测试数据泄漏，多了会对不存在的表报错。
    """
    schema_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA_SQL))
    assert set(_PG_BUSINESS_TABLES) == schema_tables
