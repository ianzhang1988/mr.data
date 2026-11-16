"""拆批语义测试（截断调查改进项 B，改进 82）：

- B1：拆批取数分区（本批 100 + 上文回顾 overlap 行）与批间标记；
- B3：system/prompt 的「第 x/y 批」标注，未拆批零变化（prompt 逐字回归锁）；
- B4：拆批时评分段落替换为「背景校准 + 锚定本批事件」规则；
- B1 边界：上文回顾行不可作 target_dialogue_log_id（id 白名单只用本批行）。
"""

import logging

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore
from mr_data.models import DialogueLog
from mr_data.offline import AttributionEngine

from conftest import capture_events, open_log_propagation

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def _setup_session(pg: PostgresStore, session_id: str, n_logs: int) -> list[int]:
    pg.create_session(session_id)
    ids = []
    for i in range(1, n_logs + 1):
        role = "assistant" if i % 2 == 0 else "user"
        ids.append(
            pg.insert_dialogue(
                DialogueLog(session_id=session_id, role=role, content=f"第{i}条")
            )
        )
    pg.close_session(session_id)
    return ids


def _make_engine(pg, chroma_store, fake_llm) -> AttributionEngine:
    return AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)


def _capture_llm(monkeypatch, fake_llm, captured: dict, payload: dict):
    """fake LLM：记录 system/user prompt，返回固定结构化结果。"""

    def _chat(system_prompt, user_prompt, response_format, temperature=0.2):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return payload

    monkeypatch.setattr(fake_llm, "chat_structured", _chat)


@pytest.mark.parametrize("overlap_lines", [2, 0])
def test_batch_partition_and_overlap_context(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch,
    overlap_lines,
):
    """7 条日志 limit=4：run1 本批 4 条（第 4~7）、剩最旧 3 条；
    上文回顾 = 已处理行中按时间紧邻本批上方的最旧 overlap 条（借插入序验证内容）。
    run2 本批 = 剩余 3 条全取，上文回顾第 4、5 条，标记后归零。
    overlap_lines=0（参数化）：拆批仍标注第 x/y 批，但 transcript 不上文回顾段。
    兼 B4：拆批时会话级评分段落替换为「背景校准 + 锚定本批事件」规则。"""
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    _setup_session(pg, test_session_id, 7)
    pg.update_session_rating(test_session_id, 1, "太冷漠了")
    monkeypatch.setattr(settings, "offline_max_session_logs", 4)  # 7 条拆 2 批：4 + 3
    monkeypatch.setattr(settings, "offline_attribution_batch_overlap_lines", overlap_lines)

    captured1: dict = {}
    _capture_llm(monkeypatch, fake_llm, captured1, {"deltas": []})
    engine = _make_engine(pg, chroma_store, fake_llm)
    engine.run()

    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 3  # 本批 4 条已标记，窗口外剩最旧 3 条
    assert "第 1/2 批" in captured1["system"]
    assert "第 1/2 批" in captured1["user"]
    assert "上文回顾" not in captured1["user"]  # 首批无已处理行
    # B4：评分段落替换为「背景校准 + 锚定本批事件」规则
    assert "最直接的成败信号" not in captured1["system"]
    assert "校准判断倾向" in captured1["system"]
    assert "锚定本批对话中的具体事件" in captured1["system"]

    captured2: dict = {}
    _capture_llm(monkeypatch, fake_llm, captured2, {"deltas": []})
    engine.run()

    # run2 本批 = 剩余 3 条全取（≤ limit），标记后归零
    assert pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True) == []
    assert "第 2/2 批" in captured2["system"]
    assert "第 2/2 批" in captured2["user"]
    if overlap_lines > 0:
        assert "上文回顾" in captured2["user"]
        assert "第4条" in captured2["user"]  # 回顾行：紧邻本批的第 4、5 条
        assert "第5条" in captured2["user"]
        assert "第7条" not in captured2["user"]  # 第 1 批最新行不属邻接上文
    else:
        assert "上文回顾" not in captured2["user"]


def test_unbatched_prompt_unchanged(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    """未拆批（≤limit 且无已处理行）：prompt 与现状逐字一致（回归锁）。"""
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    _setup_session(pg, test_session_id, 2)
    pg.update_session_rating(test_session_id, 5, "很棒")

    captured: dict = {}
    _capture_llm(monkeypatch, fake_llm, captured, {"deltas": []})
    engine = _make_engine(pg, chroma_store, fake_llm)
    engine.run()

    assert "完整会话记录" in captured["system"]
    assert "最直接的成败信号" in captured["system"]
    assert "校准判断倾向" not in captured["system"]
    assert "第 1/1 批" not in captured["system"]
    assert "本次会话记录：" in captured["user"]


def test_context_line_cannot_be_target(
    fake_llm, pg_available, chroma_store, temp_log_dir, test_session_id, monkeypatch, caplog
):
    """B1 边界：上文回顾行的 assistant id 不在 target 校验白名单内。

    第 1 批（最新 4 条）正常归因；第 2 批的上文回顾 = 紧邻的第 4、5 条。
    第 2 批 LLM 若把 target 指向回顾行（第 4 条，assistant）→ 置 None 并记
    offline.invalid_target_log_id；指向本批行（第 2 条）的对照 delta 不受影响。"""
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    ids = _setup_session(pg, test_session_id, 7)
    monkeypatch.setattr(settings, "offline_max_session_logs", 4)  # 7 条拆 2 批：4 + 3
    context_id = ids[3]  # 第 4 条：run2 的上文回顾行（assistant）
    batch_id = ids[1]  # 第 2 条：run2 本批行（assistant）
    dim_id = pg.list_dimensions(active_only=True)[0].id

    engine = _make_engine(pg, chroma_store, fake_llm)
    open_log_propagation(caplog, monkeypatch, "mr_data.offline")
    caplog.set_level(logging.INFO, logger="mr_data.offline")

    _capture_llm(monkeypatch, fake_llm, {}, {"deltas": []})
    engine.run()

    # 第 2 批：target 指向回顾行（应被拒），指向本批行（应保留）作对照。
    def _chat(system_prompt, user_prompt, response_format, temperature=0.2):
        return {
            "deltas": [
                {
                    "dimension_id": dim_id,
                    "delta_success": 1,
                    "delta_failure": 0,
                    "reason": "r1",
                    "target_dialogue_log_id": context_id,
                },
                {
                    "dimension_id": dim_id,
                    "delta_success": 1,
                    "delta_failure": 0,
                    "reason": "r2",
                    "target_dialogue_log_id": batch_id,
                },
            ]
        }

    monkeypatch.setattr(fake_llm, "chat_structured", _chat)
    engine.run()

    invalid = capture_events(caplog, "offline.invalid_target_log_id")
    assert len(invalid) == 1
    assert invalid[0].details["target_dialogue_log_id"] == context_id
