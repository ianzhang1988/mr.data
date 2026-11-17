"""split_text_chunks 与 web_chunk_base_id 的纯单元测试（不依赖 DB fixtures）。"""

import re

import pytest

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


def test_short_text_yields_single_chunk_unchanged():
    text = "这是一段很短的正文。只有两句话！"
    assert split_text_chunks(text, 400, 40) == [text]


@pytest.mark.parametrize("total_chars", [1200, 2000])
def test_mixed_sentence_endings_chunk_within_budget_with_overlap(total_chars):
    text = _long_mixed_text(total_chars=total_chars)
    chunks = split_text_chunks(text, max_chars=400, overlap_chars=40)
    assert len(chunks) > 1
    if total_chars >= 2000:
        assert len(chunks) >= 4
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


def test_web_chunk_base_id_stable_and_content_sensitive():
    url = "https://example.com/article"
    id1 = web_chunk_base_id(url, "正文甲")
    id2 = web_chunk_base_id(url, "正文甲")
    id3 = web_chunk_base_id(url, "正文乙")
    assert id1 == id2
    assert id1 != id3
    assert re.fullmatch(r"web:[0-9a-f]{64}", id1)


from mr_data.models import WebDoc, WebDocMetadata  # noqa: E402
from mr_data.online.graph import _web_memory_chunks  # noqa: E402


def test_web_memory_chunks_use_original_content_for_hash_and_chunks():
    """C3：切片与哈希基于原文（original_content）——同页不同摘取版

    page_content 不同但原文相同 → 分片 id 与片正文完全一致，
    修掉「同页不同问法重抓产生重复分片」的哈希基准 bug。"""
    url = "https://example.com/article"
    original = _long_mixed_text(total_chars=1200)
    doc_a = WebDoc(page_content="摘取版甲", original_content=original,
                   metadata=WebDocMetadata(url=url))
    doc_b = WebDoc(page_content="摘取版乙", original_content=original,
                   metadata=WebDocMetadata(url=url))

    chunks_a = _web_memory_chunks(doc_a)
    chunks_b = _web_memory_chunks(doc_b)

    assert chunks_a, "原文 1200 字应产生多个分片"
    assert chunks_a == chunks_b
    for _, chunk in chunks_a:
        assert chunk in original  # 片正文来自原文而非摘取版


def test_web_memory_chunks_fallback_to_page_content():
    """C3 退化路径：original_content=None（snippet 透传/抽取失败）回退 page_content。"""
    url = "https://example.com/article"
    text = _long_mixed_text(total_chars=1200)
    doc = WebDoc(page_content=text, metadata=WebDocMetadata(url=url))

    chunks = _web_memory_chunks(doc)

    assert chunks
    assert [cid for cid, _ in chunks] == [
        f"{web_chunk_base_id(url, text)}:c{i}" for i in range(len(chunks))
    ]
