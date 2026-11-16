"""chunk_dialogue_logs 分段逻辑的纯单元测试（不依赖 DB fixtures）。"""

import pytest

from mr_data.models import DialogueLog
from mr_data.db.chunking import chunk_dialogue_logs


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
    assert chunk.chunk_index == 0
    assert chunk.content.split("\n") == [
        "user: 你好",
        "assistant: 你好呀",
        "user: 今天天气如何",
        "assistant: 晴天",
    ]
    assert chunk.first_dialogue_log_id == 1
    assert chunk.last_dialogue_log_id == 4


@pytest.mark.parametrize("overlap_lines", [1, 0])
def test_long_session_multiple_chunks_with_overlap(overlap_lines):
    # 每行 "user: "/"assistant: " + 40 字符内容，max_chars=100 时每段最多覆盖 2 行
    logs = _make_logs(["x" * 40] * 6)
    chunks = chunk_dialogue_logs(logs, max_chars=100, overlap_lines=overlap_lines)

    assert len(chunks) == 3
    assert [c.chunk_index for c in chunks] == [0, 1, 2]

    # 各段实际覆盖的首尾 log id（overlap 行不计入覆盖范围）
    assert (chunks[0].first_dialogue_log_id, chunks[0].last_dialogue_log_id) == (1, 2)
    assert (chunks[1].first_dialogue_log_id, chunks[1].last_dialogue_log_id) == (3, 4)
    assert (chunks[2].first_dialogue_log_id, chunks[2].last_dialogue_log_id) == (5, 6)

    # 相邻段之间的关系：overlap>=1 时前一段末行 == 后一段首行；overlap=0 时无重叠行
    all_lines: list[str] = []
    for prev, nxt in zip(chunks, chunks[1:]):
        prev_lines = prev.content.split("\n")
        next_lines = nxt.content.split("\n")
        if overlap_lines:
            # overlap 行也要贡献到 content
            assert next_lines[0] == prev_lines[-1]
            assert len(next_lines) >= 2
        else:
            assert next_lines[0] != prev_lines[-1]
    for chunk in chunks:
        all_lines.extend(chunk.content.split("\n"))
    if not overlap_lines:
        # 无重叠时拼接后行序与原会话一致
        assert all_lines == [
            f"{'user' if i % 2 == 0 else 'assistant'}: {'x' * 40}" for i in range(6)
        ]


def test_overlong_single_line_split_at_sentence_boundary():
    """单行超预算且有句读：在预算内最后一个句号处下刀，各段 ≤ 预算且共享原 log id。"""
    content = "第一句。" * 20  # 80 字符，加 "assistant: " 前缀整行 91 > 50
    logs = _make_logs(["短问", content])
    chunks = chunk_dialogue_logs(logs, max_chars=50, overlap_lines=0)

    assert len(chunks) == 3  # log1 一段 + log2 切两段
    assert (chunks[0].first_dialogue_log_id, chunks[0].last_dialogue_log_id) == (1, 1)
    for chunk in chunks[1:]:
        assert (chunk.first_dialogue_log_id, chunk.last_dialogue_log_id) == (2, 2)
        assert len(chunk.content) <= 50
    # 按句号下刀：第一段以句号结尾，拼接后还原原始行
    assert chunks[1].content.endswith("。")
    assert chunks[1].content + chunks[2].content == f"assistant: {content}"


def test_overlong_single_line_hard_cut_without_punctuation():
    """单行超预算且无句读：按预算硬切，各段 ≤ 预算且共享原 log id。"""
    content = "x" * 120
    logs = _make_logs([content])
    chunks = chunk_dialogue_logs(logs, max_chars=50, overlap_lines=0)

    assert len(chunks) == 3  # "user: " + 120 = 126 → 50/50/26
    for chunk in chunks:
        assert len(chunk.content) <= 50
        assert (chunk.first_dialogue_log_id, chunk.last_dialogue_log_id) == (1, 1)
    assert "".join(c.content for c in chunks) == f"user: {content}"


def test_overlong_split_still_overlaps_into_next_chunk():
    """切分后的后续段照常享受 overlap：段首携带前段末行。"""
    logs = _make_logs(["x" * 120, "后续"])
    chunks = chunk_dialogue_logs(logs, max_chars=50, overlap_lines=1)

    last = chunks[-1]
    last_lines = last.content.split("\n")
    assert last.last_dialogue_log_id == 2
    assert last_lines[-1] == "assistant: 后续"
    # 段首为前段末行（overlap）
    prev_lines = chunks[-2].content.split("\n")
    assert last_lines[0] == prev_lines[-1]
