"""chunk_dialogue_logs 分段逻辑的纯单元测试（不依赖 DB fixtures）。"""

from mr_data.models import DialogueLog
from mr_data.offline.attribution import chunk_dialogue_logs


def _make_logs(contents: list[str], session_id: str = "s1") -> list[DialogueLog]:
    """构造对话日志，role 交替 user/assistant，id 从 1 递增；created_at 均为 None，
    排序逻辑下保持传入顺序。"""
    return [
        DialogueLog(
            id=i + 1,
            session_id=session_id,
            role="user" if i % 2 == 0 else "assistant",
            content=content,
        )
        for i, content in enumerate(contents)
    ]


def test_short_session_single_chunk():
    logs = _make_logs(["你好", "你好呀", "今天天气如何", "晴天"])
    chunks = chunk_dialogue_logs(logs, max_chars=1200, overlap_lines=2)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk["chunk_index"] == 0
    assert chunk["content"].split("\n") == [
        "user: 你好",
        "assistant: 你好呀",
        "user: 今天天气如何",
        "assistant: 晴天",
    ]
    assert chunk["first_log_id"] == 1
    assert chunk["last_log_id"] == 4


def test_long_session_multiple_chunks_with_overlap():
    # 每行 "user: "/"assistant: " + 40 字符内容，max_chars=100 时每段最多覆盖 2 行
    logs = _make_logs(["x" * 40] * 6)
    chunks = chunk_dialogue_logs(logs, max_chars=100, overlap_lines=1)

    assert len(chunks) == 3
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2]

    # 各段实际覆盖的首尾 log id（overlap 行不计入覆盖范围）
    assert (chunks[0]["first_log_id"], chunks[0]["last_log_id"]) == (1, 2)
    assert (chunks[1]["first_log_id"], chunks[1]["last_log_id"]) == (3, 4)
    assert (chunks[2]["first_log_id"], chunks[2]["last_log_id"]) == (5, 6)

    # 相邻段之间存在 overlap 行（前一段末行 == 后一段首行）
    for prev, nxt in zip(chunks, chunks[1:]):
        prev_lines = prev["content"].split("\n")
        next_lines = nxt["content"].split("\n")
        # overlap 行也要贡献到 content
        assert next_lines[0] == prev_lines[-1]
        assert len(next_lines) >= 2


def test_zero_overlap_lines():
    logs = _make_logs(["x" * 40] * 6)
    chunks = chunk_dialogue_logs(logs, max_chars=100, overlap_lines=0)

    assert len(chunks) == 3
    # 相邻段无重叠行，且拼接后行序与原会话一致
    all_lines = []
    for prev, nxt in zip(chunks, chunks[1:]):
        prev_lines = prev["content"].split("\n")
        next_lines = nxt["content"].split("\n")
        assert next_lines[0] != prev_lines[-1]
    for chunk in chunks:
        all_lines.extend(chunk["content"].split("\n"))
    assert all_lines == [f"{'user' if i % 2 == 0 else 'assistant'}: {'x' * 40}" for i in range(6)]
    assert (chunks[0]["first_log_id"], chunks[0]["last_log_id"]) == (1, 2)
    assert (chunks[-1]["first_log_id"], chunks[-1]["last_log_id"]) == (5, 6)


def test_empty_logs():
    assert chunk_dialogue_logs([], max_chars=100, overlap_lines=2) == []
