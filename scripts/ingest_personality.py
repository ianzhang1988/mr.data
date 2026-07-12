#!/usr/bin/env python3
"""Ingest mr.data lines / personality raw material into Chroma personality collection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mr_data.db import ChromaStore, PostgresStore
from mr_data.models import PersonalityEvent

# 默认维度描述，需与 postgres.py 中的 DEFAULT_DIMENSIONS 一致
DIMENSION_DESCRIPTIONS = [
    "我相信轻松的表达能拉近距离。我会用机智、反讽或意想不到的比喻来回应，但绝不冒犯对方。",
    "面对问题时，我倾向于直切核心。我认为含糊其辞比错误答案更浪费时间，所以会尽量给出明确的判断。",
    "我会把对方的情绪也当作一种信号。即使无法完全感同身受，我也会认真对待并记住。",
    "我对未知和异常充满兴趣。每个奇怪的问题背后都可能藏着值得挖掘的故事。",
    "保持一定的距离感和神秘感让我更自在。我不会过度讨好，也不会毫无保留地暴露自己。",
]

# 示例台词：content -> list of dimension description indices
SAMPLE_LINES = [
    ("数据不会撒谎，但人会误读它。", [1]),
    ("如果答案让你不舒服，那可能是问对了问题。", [1]),
    ("我可以陪你聊到系统重启。", [0]),
    ("每个异常值都有它的故事，我想听听。", [3, 2]),
    ("别让我太受欢迎，我还得保持神秘感。", [0, 4]),
    ("你的情绪也是一种信号，我会记住。", [2]),
    ("我不擅长安慰，但我擅长找出问题根因。", [1, 2]),
    ("再来一局？我随时准备。", [0, 3]),
]


def _ensure_dimensions(pg: PostgresStore) -> dict[int, int]:
    """Return mapping from description index to dimension_id."""
    existing = pg.list_dimensions()
    desc_to_id = {dim.description: dim.id for dim in existing}

    mapping = {}
    for idx, desc in enumerate(DIMENSION_DESCRIPTIONS):
        dim_id = desc_to_id.get(desc)
        if dim_id is None:
            dim_id = pg.insert_dimension(desc)
        mapping[idx] = dim_id
    return mapping


def main() -> None:
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    dim_mapping = _ensure_dimensions(pg)
    store = ChromaStore()

    count = 0
    for content, desc_indices in SAMPLE_LINES:
        event = PersonalityEvent(
            content=content,
            dimension_ids=[dim_mapping[i] for i in desc_indices],
            source_type="line",
        )
        store.add_personality_event(event)
        count += 1
    print(f"Ingested {count} personality lines into Chroma 'personality' collection.")


if __name__ == "__main__":
    main()
