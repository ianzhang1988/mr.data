"""改进 69：run() 无待处理会话时不再早退，记忆过期清理（prune）仍须执行。"""

from unittest.mock import MagicMock

from mr_data.config import settings
from mr_data.offline.attribution import AttributionEngine


def test_run_prunes_stale_memories_when_no_sessions():
    """list_closed_sessions_with_unprocessed 返回空列表时，prune 仍被调用一次。"""
    pg = MagicMock()
    pg.list_closed_sessions_with_unprocessed.return_value = []
    chroma = MagicMock()
    llm = MagicMock()

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma, llm=llm)
    engine.run()

    chroma.prune_stale_memories.assert_called_once_with(
        settings.memory_dialogue_retention_days,
        settings.memory_min_recall_count,
    )
