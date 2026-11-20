"""行级归因三态（改进 99）：unprocessed / processed / failed。

F-10：死信行不再伪装成已处理——埋葬置 failed 态，行级审计可区分
「真归因完」「死信不再重试」「仍在队列」三种结局。
"""

import pytest

from mr_data.config import settings
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


def _failing_llm(fake_llm, monkeypatch):
    def _failing_structured_chat(system_prompt, user_prompt, response_format, temperature=0.2):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr(fake_llm, "chat_structured", _failing_structured_chat)


def test_insert_dialogue_defaults_to_unprocessed(pg, test_session_id):
    pg.create_session(test_session_id)
    log_id = pg.insert_dialogue(
        DialogueLog(session_id=test_session_id, role="user", content="hi")
    )
    logs = pg.get_recent_dialogues(session_id=test_session_id)
    assert len(logs) == 1
    assert logs[0].id == log_id
    assert logs[0].attribution_status == "unprocessed"


def test_mark_dialogues_failed_guard_and_count(pg, test_session_id):
    """mark_dialogues_failed 只覆盖 unprocessed 行，不碰已 processed 行。"""
    pg.create_session(test_session_id)
    ids = [
        pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="user", content=f"m{i}"))
        for i in range(3)
    ]
    pg.mark_dialogue_processed(ids[0])

    marked = pg.mark_dialogues_failed([ids[0], ids[1]])
    assert marked == 1  # 已 processed 的行不被覆盖

    logs = {log.id: log for log in pg.get_recent_dialogues(session_id=test_session_id)}
    assert logs[ids[0]].attribution_status == "processed"
    assert logs[ids[1]].attribution_status == "failed"
    assert logs[ids[2]].attribution_status == "unprocessed"

    # 重复落葬幂等：failed 行不再计入标记数
    assert pg.mark_dialogues_failed([ids[1]]) == 0
    assert pg.mark_dialogues_failed([]) == 0


def test_count_dialogues_includes_failed_in_processed_side(pg, test_session_id):
    """批号 x/y 的已处理计数含 failed（窗口推进不回退，改进 82 语义保持）。"""
    pg.create_session(test_session_id)
    ids = [
        pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="user", content=f"m{i}"))
        for i in range(3)
    ]
    pg.mark_dialogue_processed(ids[0])
    pg.mark_dialogues_failed([ids[1]])

    assert pg.count_dialogues(test_session_id) == 2  # processed + failed
    assert pg.count_dialogues(test_session_id, unprocessed_only=True) == 1


def test_dead_letter_marks_failed_not_processed(
    fake_llm, pg, chroma_store, temp_log_dir, test_session_id, monkeypatch
):
    """达阈值死信后：进过 transcript 的行是 failed，行级可与真 processed 区分。"""
    _setup_closed_session(pg, test_session_id)
    monkeypatch.setattr(settings, "offline_max_attribution_attempts", 1)
    _failing_llm(fake_llm, monkeypatch)

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    engine.run()

    assert pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True) == []
    logs = pg.get_recent_dialogues(session_id=test_session_id)
    assert len(logs) == 2
    assert all(log.attribution_status == "failed" for log in logs)

    # failed 行不进待归因会话列表，也不作拆批上文（只取 processed）
    assert pg.list_closed_sessions_with_unprocessed() == []
    assert pg.get_adjacent_processed_dialogues(
        test_session_id, logs[0].created_at, logs[0].id, 2
    ) == []


def test_batch_index_advances_after_dead_letter(
    fake_llm, pg, chroma_store, temp_log_dir, test_session_id, monkeypatch, caplog
):
    """死信落葬后批号不回退：下一分片 batch_index 按 processed+failed 计数推进。"""
    import logging

    from conftest import capture_events, open_log_propagation

    pg.create_session(test_session_id)
    for i in range(3):
        pg.insert_dialogue(
            DialogueLog(session_id=test_session_id, role="user", content=f"消息 {i}")
        )
    pg.close_session(test_session_id)
    monkeypatch.setattr(settings, "offline_max_attribution_attempts", 1)
    monkeypatch.setattr(settings, "offline_max_session_logs", 1)  # 每批 1 条 → 拆批
    _failing_llm(fake_llm, monkeypatch)

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    open_log_propagation(caplog, monkeypatch, "mr_data.offline")
    caplog.set_level(logging.INFO, logger="mr_data.offline")

    engine.run()  # 第 1 批（最新 1 条）失败即落葬
    engine.run()  # 第 2 批：batch_index 应为 2，而非死信回退后的 1

    split = capture_events(caplog, "offline.session_logs_split")
    assert len(split) == 2
    # run2 时已了结 1 条（failed）+ 未处理 2 条 → x = 1//1+1 = 2
    assert split[1].details["total_batches"] == 3
    assert split[1].details["batch_size"] == 1
    # x/y 标注进 prompt 的断言由拆批集成用例覆盖，这里只锁计数不回退：
    # 若 failed 不计入已处理侧，total_batches 将算成 2、x 回退为 1。
