"""用户打分闭环测试（改进项 56）。

覆盖：
- PG 层 sessions.rating / rating_comment 写入与读回、会话 id 复用时重置；
- 离线归因 _build_context 注入用户评分与评论（无评分时段落省略）；
- CLI 退出评分 helper：合法输入落库、空输入/非法输入跳过。
"""

import pytest

from mr_data import cli as cli_module
from mr_data.db import PostgresStore
from mr_data.models import DialogueLog
from mr_data.offline import AttributionEngine

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _make_engine(pg: PostgresStore, chroma_store, fake_llm) -> AttributionEngine:
    return AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)


def test_update_session_rating_roundtrip(pg_available, test_session_id):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)
    pg.update_session_rating(test_session_id, -1, "回复太机械")

    session = pg.get_session(test_session_id)
    assert session is not None
    assert session.rating == -1
    assert session.rating_comment == "回复太机械"


def test_reactivate_session_resets_rating(pg_available, test_session_id):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)
    pg.update_session_rating(test_session_id, 2, "很棒")
    pg.close_session(test_session_id)

    pg.create_session(test_session_id)  # 同 id 复用重激活
    session = pg.get_session(test_session_id)
    assert session.status == "active"
    assert session.rating is None
    assert session.rating_comment is None


def test_context_includes_session_rating(
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)
    pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="user", content="你好"))
    pg.update_session_rating(test_session_id, -1, "一直在重复套话，很死板")

    engine = _make_engine(pg, chroma_store, fake_llm)
    context = engine._build_context(test_session_id)

    assert "用户对本会话的评价" in context
    assert "用户评分：-1（差）" in context
    assert "用户评论：一直在重复套话，很死板" in context


def test_context_omits_rating_when_absent(
    pg_available, chroma_store, fake_llm, temp_log_dir, test_session_id
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)
    pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="user", content="你好"))

    engine = _make_engine(pg, chroma_store, fake_llm)
    context = engine._build_context(test_session_id)

    assert "用户对本会话的评价" not in context


def test_cli_rating_prompt_writes_valid_input(pg_available, test_session_id, monkeypatch):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    answers = iter(["2", "很棒"])
    monkeypatch.setattr(
        cli_module.Prompt, "ask", staticmethod(lambda *a, **k: next(answers))
    )

    cli_module._ask_session_rating(pg, test_session_id)

    session = pg.get_session(test_session_id)
    assert session.rating == 2
    assert session.rating_comment == "很棒"


def test_cli_rating_prompt_skips_empty_and_invalid(
    pg_available, test_session_id, monkeypatch
):
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    # 空输入：直接跳过，不写库
    monkeypatch.setattr(cli_module.Prompt, "ask", staticmethod(lambda *a, **k: ""))
    cli_module._ask_session_rating(pg, test_session_id)
    assert pg.get_session(test_session_id).rating is None

    # 非法输入：非整数与越界均按跳过处理，不写库
    for raw in ("abc", "5", "-3"):
        monkeypatch.setattr(
            cli_module.Prompt, "ask", staticmethod(lambda *a, _raw=raw, **k: _raw)
        )
        cli_module._ask_session_rating(pg, test_session_id)
        assert pg.get_session(test_session_id).rating is None
