"""句读优先切分的公共模块。

dialogue 行级分段（chunk_dialogue_logs，离线归因写入记忆向量库）与
web 正文分段（split_text_chunks，在线管道写入记忆向量库）共用同一套
句读逻辑：预算窗口内最后一个句号/问号/感叹号/换行处下刀，标点保留在
前段；无句读则按预算硬切。
"""

from datetime import datetime
from typing import Any

from mr_data.models import DialogueChunk, DialogueLog

_SENTENCE_ENDINGS = ("。", "！", "？", "\n")


def _split_overlong_line(line: str, max_chars: int) -> list[str]:
    """把超过字符预算的单行切分为多个不超预算的片段。

    优先在预算窗口内最后一个句号/换行（兼顾！？）处下刀，标点保留在前段；
    无句读则按预算硬切。嵌入模型会静默截断超长文本，分段必须保证不超预算。
    """
    pieces: list[str] = []
    rest = line
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = max(window.rfind(mark) for mark in _SENTENCE_ENDINGS)
        if cut <= 0:
            cut = max_chars
        else:
            cut += 1  # 标点/换行符保留在前一段末尾
        pieces.append(rest[:cut])
        rest = rest[cut:]
    pieces.append(rest)
    return pieces


def chunk_dialogue_logs(
    logs: list[DialogueLog], max_chars: int, overlap_lines: int
) -> list[DialogueChunk]:
    """把会话日志按字符预算分段，段间保留 overlap_lines 行重叠。

    单行超预算时行内切分（见 _split_overlong_line），切出的片段共享同一
    log id——对应分段的 first/last_dialogue_log_id 均为该 log id。
    """
    sorted_logs = sorted(logs, key=lambda x: (x.created_at is None, x.created_at or datetime.min, x.id or 0))
    lines = [
        (f"{'user' if log.role == 'user' else 'assistant'}: {log.content}", log.id)
        for log in sorted_logs
    ]
    # 预切分超预算行：主循环不再存在"整行成段超预算"的路径。
    lines = [
        (piece, log_id)
        for line, log_id in lines
        for piece in _split_overlong_line(line, max_chars)
    ]

    chunks: list[DialogueChunk] = []
    cur_lines: list[str] = []  # 当前段的全部行（含 overlap 前缀）
    cur_ids: list[Any] = []  # 当前段实际覆盖的 log id（不含 overlap 行）
    cur_chars = 0  # 实际覆盖行的字符预算（不含 overlap 行）

    for line, log_id in lines:
        # 当前段已有覆盖行且加入下一行会超预算时关闭当前段。
        if cur_ids and cur_chars + len(line) > max_chars:
            chunks.append(
                DialogueChunk(
                    content="\n".join(cur_lines),
                    chunk_index=len(chunks),
                    first_dialogue_log_id=cur_ids[0],
                    last_dialogue_log_id=cur_ids[-1],
                )
            )
            cur_lines = cur_lines[-overlap_lines:] if overlap_lines > 0 else []
            cur_ids = []
            cur_chars = 0
        cur_lines.append(line)
        cur_ids.append(log_id)
        cur_chars += len(line)

    if cur_ids:
        chunks.append(
            DialogueChunk(
                content="\n".join(cur_lines),
                chunk_index=len(chunks),
                first_dialogue_log_id=cur_ids[0],
                last_dialogue_log_id=cur_ids[-1],
            )
        )
    return chunks


def split_text_chunks(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """把纯正文按字符预算切成多片，供 web 记忆入库使用。

    与 dialogue 分段同口径：预算窗口内最后一个句读处下刀，标点保留在前段；
    无句读则按预算硬切。相邻两片保留 overlap_chars 字符重叠，且下一片起点
    向前推进到句读边界，保证切口稳定。空串或纯空白文本返回 []。
    """
    if not text.strip():
        return []
    chunks: list[str] = []
    pos = 0  # 当前片起点
    n = len(text)
    while n - pos > max_chars:
        window = text[pos:pos + max_chars]
        cut = max(window.rfind(mark) for mark in _SENTENCE_ENDINGS)
        if cut <= 0:
            cut = max_chars  # 窗口内无句读，按预算硬切
        else:
            cut += 1  # 标点保留在前段末尾
        chunks.append(text[pos:pos + cut])
        end = pos + cut
        # 下一片起点：回退 overlap，钳到上个 pos 之后以保证前进。
        nxt = max(end - overlap_chars, pos + 1)
        # 推进到 >= nxt 的第一个句读之后，使下一片从句读边界起。
        span = text[nxt:end]
        hits = [span.find(mark) for mark in _SENTENCE_ENDINGS]
        hits = [h for h in hits if h >= 0]
        if hits:
            nxt += min(hits) + 1
        pos = nxt  # nxt > 上个 pos 恒成立，循环必有进展
    chunks.append(text[pos:])
    return chunks
