"""在线 _log_dialogue 事务化测试。

覆盖：
- insert_dialogue_vector_refs 崩溃 → 事务回滚：本回合 user/assistant 行及
  dialogue_dimension_refs/dialogue_vector_refs 均无残留；
- 正常路径：事务提交后 user+assistant 行、维度 refs、向量 refs 齐全；
- 网络资料写入记忆分片单条失败被隔离：不影响本回合 PG 提交，也不中断对话。
"""

import pytest

from mr_data.db import PostgresStore
from mr_data.db.chroma import web_chunk_base_id
from mr_data.models import (
    DialogueMemoryMetadata,
    MemoryDoc,
    PersonalityDoc,
    PersonalityDocMetadata,
    WebDoc,
    WebDocMetadata,
    WebMemoryMetadata,
)
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
            PersonalityDoc(
                id="personality:tx-1",
                page_content="人格素材内容",
                metadata=PersonalityDocMetadata(
                    utterance="人格素材内容",
                    source_type="line",
                    dimension_ids=[1],
                ),
            )
        ],
        "web_docs": [
            WebDoc(
                page_content="网络资料内容",
                metadata=WebDocMetadata(
                    url="http://example.com/tx",
                    title="事务测试",
                ),
            )
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


def test_log_dialogue_registers_memory_docs(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id
):
    """改进 88：进入 prompt 的 memory_docs（filter 之后的集合）登记进
    dialogue_vector_refs——dialogue 段按段、web 旧片按片，dimension_ids 恒空。"""
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
    memory_docs = [
        MemoryDoc(
            id="memory:dlg-1",
            page_content="旧会话记忆段",
            metadata=DialogueMemoryMetadata(session_id="s-old", chunk_index=0),
        ),
        MemoryDoc(
            id="memory:web-1:c0",
            page_content="旧 web 记忆片",
            metadata=WebMemoryMetadata(
                url="http://example.com/old", title="旧资料"),
        ),
    ]
    state["memory_docs"] = memory_docs

    result = graph._log_dialogue(state)
    assistant_log_id = result["assistant_log_id"]
    assert assistant_log_id is not None

    with pg._cursor() as cur:
        cur.execute(
            """
            SELECT vector_doc_id, source_type, dimension_ids
            FROM dialogue_vector_refs
            WHERE dialogue_log_id = %s
            ORDER BY id
            """,
            (assistant_log_id,),
        )
        rows = cur.fetchall()

    # 人格 1 + 本轮 web 片 1 + memory_docs 2 = 4 行；登记集合与 state 完全一致。
    assert [(r["vector_doc_id"], r["source_type"]) for r in rows] == [
        ("personality:tx-1", "line"),
        ("memory:dlg-1", "dialogue"),
        ("memory:web-1:c0", "web"),
        (f"{web_chunk_base_id('http://example.com/tx', '网络资料内容')}:c0", "web"),
    ]
    # memory refs 的 dimension_ids 落库 NULL（恒空，不参与维度 purge）。
    assert rows[1]["dimension_ids"] is None
    assert rows[2]["dimension_ids"] is None


def test_web_memory_write_failure_does_not_abort_turn(
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

    def _failing_add_memory(*args, **kwargs):
        raise RuntimeError("模拟 Chroma 故障")

    monkeypatch.setattr(chroma_store, "add_memory", _failing_add_memory)

    result = graph._log_dialogue(state)
    assistant_log_id = result["assistant_log_id"]
    assert assistant_log_id is not None

    with pg._cursor() as cur:
        cur.execute(
            "SELECT role FROM dialogue_logs WHERE session_id = %s ORDER BY id",
            (test_session_id,),
        )
        roles = [row["role"] for row in cur.fetchall()]
    assert roles == ["user", "assistant"]

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

    assert chroma_store.memories.count() == 0
