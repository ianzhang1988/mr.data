"""在线 _log_dialogue 事务化测试。

覆盖：
- insert_dialogue_vector_refs 崩溃 → 事务回滚：本回合 user/assistant 行及
  dialogue_dimension_refs/dialogue_vector_refs 均无残留；
- 正常路径：事务提交后 user+assistant 行、维度 refs、向量 refs 齐全。
"""

import pytest

from mr_data.db import PostgresStore
from mr_data.online import DialogueGraph

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _build_state(pg: PostgresStore, session_id: str) -> dict:
    dimensions = pg.list_dimensions(active_only=True)
    return {
        "session_id": session_id,
        "user_input": "事务测试输入",
        "reply": "事务测试回复",
        "reply_blocks": [],
        "dimensions": dimensions,
        "selected_dimension_ids": [d.id for d in dimensions if d.id is not None],
        "personality_docs": [
            {
                "id": "personality:tx-1",
                "page_content": "人格素材内容",
                "metadata": {"source_type": "line", "dimension_ids": [1]},
            }
        ],
        "web_docs": [
            {
                "id": "web:tx-1",
                "page_content": "网络资料内容",
                "metadata": {
                    "source_type": "web",
                    "url": "http://example.com/tx",
                    "title": "事务测试",
                },
            }
        ],
        "inner_monologue": "测试内心独白",
    }


def _count(pg: PostgresStore, sql: str, params: tuple = ()) -> int:
    with pg._cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()["c"]


def test_vector_refs_failure_rolls_back_dialogue_writes(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )
    state = _build_state(pg, test_session_id)

    # 崩溃点在最后一步：user/assistant 行与维度 refs 已写入事务，vector refs 插入失败。
    def _failing_insert_vector_refs(dialogue_log_id, refs):
        raise RuntimeError("模拟 _log_dialogue 中途崩溃")

    monkeypatch.setattr(pg, "insert_dialogue_vector_refs",
                        _failing_insert_vector_refs)

    with pytest.raises(RuntimeError, match="模拟 _log_dialogue 中途崩溃"):
        graph._log_dialogue(state)

    # 事务回滚：本回合 dialogue_logs 无残留（user/assistant 行都不在）。
    assert _count(
        pg,
        "SELECT COUNT(*) AS c FROM dialogue_logs WHERE session_id = %s",
        (test_session_id,),
    ) == 0
    # refs 表同样无残留。
    assert _count(pg, "SELECT COUNT(*) AS c FROM dialogue_dimension_refs") == 0
    assert _count(pg, "SELECT COUNT(*) AS c FROM dialogue_vector_refs") == 0


def test_log_dialogue_commits_all_rows(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.init_schema()
    pg.seed()
    pg.create_session(test_session_id)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )
    state = _build_state(pg, test_session_id)

    result = graph._log_dialogue(state)
    assistant_log_id = result["assistant_log_id"]
    assert assistant_log_id is not None

    # 事务提交后：user + assistant 两行齐全。
    with pg._cursor() as cur:
        cur.execute(
            "SELECT role FROM dialogue_logs WHERE session_id = %s ORDER BY id",
            (test_session_id,),
        )
        roles = [row["role"] for row in cur.fetchall()]
    assert roles == ["user", "assistant"]

    # 维度 refs 与向量 refs（人格 + 网络资料）齐全。
    expected_dim_ids = set(state["selected_dimension_ids"])
    assert _count(
        pg,
        "SELECT COUNT(*) AS c FROM dialogue_dimension_refs WHERE dialogue_log_id = %s",
        (assistant_log_id,),
    ) == len(expected_dim_ids)
    assert _count(
        pg,
        "SELECT COUNT(*) AS c FROM dialogue_vector_refs WHERE dialogue_log_id = %s",
        (assistant_log_id,),
    ) == 2
