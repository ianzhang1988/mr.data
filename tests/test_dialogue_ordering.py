"""改进 65：对话排序 tiebreaker（created_at 相同按 id 确定顺序）。"""

from datetime import datetime, timezone

import pytest

from mr_data.models import DialogueLog
from mr_data.db.chunking import chunk_dialogue_logs


@pytest.mark.usefixtures("reset_pg_state")
def test_get_recent_dialogues_tiebreaks_by_id_desc(test_session_id, pg):
    """同事务插入的两条对话 created_at 相同（NOW() 是事务时间），
    查询必须按 id DESC 确定性排序，后插入的（assistant）排在前面。"""
    pg.create_session(test_session_id)

    with pg.transaction():
        user_id = pg.insert_dialogue(
            DialogueLog(session_id=test_session_id, role="user", content="问")
        )
        assistant_id = pg.insert_dialogue(
            DialogueLog(session_id=test_session_id, role="assistant", content="答")
        )

    logs = pg.get_recent_dialogues(session_id=test_session_id, limit=10)
    assert [log.id for log in logs] == [assistant_id, user_id]


def test_chunk_dialogue_logs_tiebreaks_by_id_asc():
    """created_at 相同的日志按 id 升序排列（与 SQL id DESC 取数后翻转一致）。"""
    same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    logs = [
        DialogueLog(id=2, session_id="s", role="assistant", content="答", created_at=same_time),
        DialogueLog(id=1, session_id="s", role="user", content="问", created_at=same_time),
    ]
    chunks = chunk_dialogue_logs(logs, max_chars=1000, overlap_lines=0)
    assert len(chunks) == 1
    assert chunks[0].content == "user: 问\nassistant: 答"


def test_chunk_dialogue_logs_tolerates_none_created_at():
    """created_at 为 None 的日志排最后，且不与有时间的日志发生类型比较。"""
    timed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    logs = [
        DialogueLog(id=2, session_id="s", role="assistant", content="无时间"),
        DialogueLog(id=1, session_id="s", role="user", content="有时间", created_at=timed),
    ]
    chunks = chunk_dialogue_logs(logs, max_chars=1000, overlap_lines=0)
    assert chunks[0].content == "user: 有时间\nassistant: 无时间"
