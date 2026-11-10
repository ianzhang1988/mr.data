"""split_text_chunks 与 web_chunk_base_id 的纯单元测试（不依赖 DB fixtures）。"""

import re

from mr_data.db.chunking import split_text_chunks
from mr_data.db.chroma import web_chunk_base_id

_SENTENCE_ENDINGS = ("。", "！", "？", "\n")


def _long_mixed_text(total_chars: int = 1200) -> str:
    """构造多句中文正文，句读（。！？\\n）混合，句长 ~30 字。"""
    sentences = [
        "今天天气不错适合出门散步",
        "你好吗我最近在学习中文",
        "这本书非常有趣值得一看",
        "我们明天一起去公园吧",
    ]
    marks = ["。", "！", "？", "\n"]
    parts = []
    i = 0
    while sum(len(p) for p in parts) < total_chars:
        parts.append(sentences[i % len(sentences)] + marks[i % len(marks)])
        i += 1
    return "".join(parts)


def test_empty_and_whitespace_text_return_no_chunks():
    assert split_text_chunks("", 400, 40) == []
    assert split_text_chunks("   \n\t  ", 400, 40) == []


def test_short_text_yields_single_chunk_unchanged():
    text = "这是一段很短的正文。只有两句话！"
    assert split_text_chunks(text, 400, 40) == [text]


def test_mixed_sentence_endings_chunk_within_budget_with_overlap():
    text = _long_mixed_text()
    chunks = split_text_chunks(text, max_chars=400, overlap_chars=40)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 400
    # 除尾片外每片末尾是句读字符（切口句读优先）
    for chunk in chunks[:-1]:
        assert chunk.endswith(_SENTENCE_ENDINGS)
    # 相邻两片有重叠，且重叠段（前片末尾 == 后片开头）以句读结尾
    for prev, nxt in zip(chunks, chunks[1:]):
        overlap = 0
        for k in range(min(len(prev), len(nxt)), 0, -1):
            if prev[-k:] == nxt[:k]:
                overlap = k
                break
        assert overlap > 0, "相邻两片应存在重叠"
        assert prev[-1] in _SENTENCE_ENDINGS  # 重叠段以句读结尾


def test_no_sentence_endings_hard_cut_and_restore():
    overlap = 40
    text = "字" * 1000  # 全文无任何句读
    chunks = split_text_chunks(text, max_chars=400, overlap_chars=overlap)
    assert len(chunks) == 3  # 400 + 400 + 200
    for chunk in chunks[:-1]:
        assert chunk == "字" * 400
    # 拼接（去 overlap）还原全文
    restored = chunks[0] + "".join(chunk[overlap:] for chunk in chunks[1:])
    assert restored == text


def test_chunk_boundaries_are_stable():
    text = _long_mixed_text()
    first = split_text_chunks(text, max_chars=400, overlap_chars=40)
    second = split_text_chunks(text, max_chars=400, overlap_chars=40)
    assert first == second


def test_convergence_on_2000_chars():
    text = _long_mixed_text(total_chars=2000)
    chunks = split_text_chunks(text, max_chars=400, overlap_chars=40)
    assert len(chunks) >= 4
    for chunk in chunks:
        assert len(chunk) <= 400


def test_web_chunk_base_id_stable_and_content_sensitive():
    url = "https://example.com/article"
    id1 = web_chunk_base_id(url, "正文甲")
    id2 = web_chunk_base_id(url, "正文甲")
    id3 = web_chunk_base_id(url, "正文乙")
    assert id1 == id2
    assert id1 != id3
    assert re.fullmatch(r"web:[0-9a-f]{64}", id1)
