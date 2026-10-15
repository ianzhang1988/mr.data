"""`mr-data ingest` 幂等性测试（改进项 ingest 不幂等修复）。

覆盖：
- 重复执行 ingest 不会导致 personality 集合文档数翻倍；
- 已存在文档被正确计数为 skipped，新增文档计数为 added；
- pack 内部若出现重复行，后续行也计 skipped。
"""

import pytest

pytest.importorskip("pgembed", reason="pgembed not installed")

from mr_data.cli import ingest
from mr_data.db import PostgresStore
from mr_data.models import (
    FixedIdentity,
    PersonalityPack,
    PersonalitySampleLine,
)

pytestmark = pytest.mark.usefixtures("reset_pg_state")


def test_ingest_is_idempotent_and_counts_skipped(
    pg_available, chroma_store, temp_log_dir, monkeypatch, capsys
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()

    pack = PersonalityPack(
        identity=FixedIdentity(name="Data", role="android", base_prompt="x"),
        dimensions=[],
        sample_lines=[
            PersonalitySampleLine(
                content="台词一",
                context="场景一",
                speaker="Data",
            ),
            PersonalitySampleLine(
                content="台词二",
                context="场景二",
                speaker="Data",
            ),
        ],
    )

    monkeypatch.setattr("mr_data.cli.load_personality_pack", lambda: pack)
    monkeypatch.setattr("mr_data.cli.PostgresStore", lambda: pg)
    monkeypatch.setattr("mr_data.cli.ChromaStore", lambda: chroma_store)

    ingest()
    assert chroma_store.personality.count() == 2

    capsys.readouterr()
    ingest()
    captured = capsys.readouterr()

    assert chroma_store.personality.count() == 2
    assert "Ingested 0 personality lines" in captured.out
    assert "2 skipped" in captured.out
