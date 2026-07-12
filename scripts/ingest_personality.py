#!/usr/bin/env python3
"""Ingest mr.data lines / personality raw material into Chroma personality collection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mr_data.db import ChromaStore
from mr_data.models import PersonalityEvent

# 示例台词：可替换为真实的 mr.data 台词数据
SAMPLE_LINES = [
    ("数据不会撒谎，但人会误读它。", ["直接性", "幽默感"]),
    ("如果答案让你不舒服，那可能是问对了问题。", ["直接性", "好奇心"]),
    ("我可以陪你聊到系统重启。", ["幽默感", "同理心"]),
    ("每个异常值都有它的故事，我想听听。", ["好奇心", "同理心"]),
    ("别让我太受欢迎，我还得保持神秘感。", ["幽默感", "防御性"]),
    ("你的情绪也是一种信号，我会记住。", ["同理心"]),
    ("我不擅长安慰，但我擅长找出问题根因。", ["直接性", "同理心"]),
    ("再来一局？我随时准备。", ["幽默感", "好奇心"]),
]


def main() -> None:
    store = ChromaStore()
    count = 0
    for content, tags in SAMPLE_LINES:
        event = PersonalityEvent(
            content=content,
            dimension_tags=tags,
            source_type="line",
        )
        store.add_personality_event(event)
        count += 1
    print(f"Ingested {count} personality lines into Chroma 'personality' collection.")


if __name__ == "__main__":
    main()
