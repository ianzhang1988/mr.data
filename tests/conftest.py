import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path

# Disable web search by default in tests to avoid external network calls.
os.environ.setdefault("MR_DATA_ENABLE_WEB_SEARCH", "false")

# Skip live web page extraction in tests: fake search results already include page_content.
os.environ.setdefault("MR_DATA_WEB_EXTRACT_MAX_PAGES", "0")

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore, PgEmbedManager, ChromaStore
from mr_data.llm import LLMClient
from mr_data.logging import reset_loggers


class FakeLLMClient(LLMClient):
    """LLM client that returns deterministic responses for tests."""

    def __init__(self):
        super().__init__(base_url="http://fake", api_key="fake", model="fake")
        # 改进 54 移除了 target 的 fallback：需要 evidence/vector_refs 落库的测试
        # 必须在插入对话后把它设为实际的 assistant 日志 id（否则归因无 target）。
        self.attribution_target_log_id = None

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
        if "性格维度选择助手" in system_prompt:
            return '{"dimension_ids": [1]}'
        if "检索查询" in system_prompt and "内心独白" in system_prompt:
            return "检索查询：用户查询改写 内心独白：这是一个测试内心独白"
        if "判断以下网络资料" in system_prompt:
            return "yes"
        if "你是对话归因分析器" in system_prompt:
            return json.dumps({
                "deltas": [
                    {
                        "dimension_id": 1,
                        "delta_success": 1,
                        "delta_failure": 0,
                        "reason": "测试归因原因",
                        "evidence_snippets": ["user: 测试输入\nassistant: 测试回复"],
                        "relation_to_personality": "体现",
                        "target_dialogue_log_id": self.attribution_target_log_id,
                    }
                ]
            })
        return "这是一个测试回复。"

    def chat_with_messages(self, messages: list[dict], temperature: float = 0.7) -> str:
        joined = "\n".join(m.get("content", "") for m in messages)
        return self.chat("", joined, temperature)

    def structured_chat(self, messages: list[dict], response_format, temperature=0.2):
        name = response_format.__name__
        if name == "ThinkDecision":
            return {
                "inner_monologue": "这是一个测试内心独白",
                "personality_query": "测试性格查询",
                "memory_query": "测试记忆查询",
                "needs_web_search": False,
                "search_query": "测试搜索查询",
            }
        if name == "DimensionSelection":
            return {"dimension_ids": [1]}
        if name == "AttributionResult":
            return {
                "deltas": [
                    {
                        "dimension_id": 1,
                        "delta_success": 1,
                        "delta_failure": 0,
                        "reason": "测试归因原因",
                        "evidence_snippets": ["user: 测试输入\nassistant: 测试回复"],
                        "relation_to_personality": "体现",
                        "target_dialogue_log_id": self.attribution_target_log_id,
                    }
                ]
            }
        if name == "AssistantReply":
            return {
                "text": "这是一个测试回复。",
                "blocks": [
                    {"text": "这是一个测试回复。", "references": []}
                ],
            }
        if name == "WebDocExtraction":
            # Empty extraction keeps the original document.
            return {"is_relevant": True, "extracted_text": ""}
        if name == "MemoryRelevanceFilterResult":
            # Returning empty results causes the node to fall back to keeping all docs.
            return {"results": []}
        if name == "DimensionDedupResult":
            # Empty matches means: no duplicate found, allow creating new dimensions.
            return {"matches": []}
        return {"deltas": []}


class FakeEmbedding:
    """Deterministic embedding function that avoids downloading heavy models."""

    def __init__(self, dim: int = 8):
        self._dim = dim

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [self._encode(text) for text in input]

    def _encode(self, text: str) -> list[float]:
        digest = hashlib.md5(text.encode("utf-8")).digest()
        return [float(b) / 255.0 for b in digest[: self._dim]]

    @property
    def dim(self) -> int:
        return self._dim


@pytest.fixture
def fake_llm():
    return FakeLLMClient()


@pytest.fixture
def fake_embedding():
    return FakeEmbedding(dim=8)


@pytest.fixture(scope="session")
def _chroma_base():
    """进程级共享的 ChromaStore。chromadb EphemeralClient 本就是进程级单例
    （src 注释自述 SharedSystemClient），但 __init__ 的 drop+create 两集合
    每测试 ~0.39s；改为 session 级建一次、每测试只清空文档（实测 ~8ms）。
    隔离语义不变：测试可见状态=文档+metadata，清空 == 重建；embedding 由
    ChromaStore 显式传入（集合上绑定的 fn 无关），索引内部细节持久不影响。
    注意：ephemeral 集合名（personality/memories）进程内共享，测试中自建
    ChromaStore(ephemeral=True)（如 test_embeddings）会重建同名集合，
    fixture 每次先清空，隔离仍成立。"""
    return ChromaStore(
        ephemeral=True,
        personality_embedding_fn=FakeEmbedding(dim=8),
        memory_embedding_fn=FakeEmbedding(dim=8),
    )


@pytest.fixture
def chroma_store(_chroma_base):
    """共享 store 的清空视图：每测试删除两集合全部文档，等价于 fresh store。"""
    for name in ("personality", "memories"):
        col = _chroma_base._client.get_collection(name)
        ids = col.get()["ids"]
        if ids:
            col.delete(ids=ids)
    return _chroma_base


@pytest.fixture(scope="session")
def pgembed_server():
    """Start a reusable embedded PG server for the test session when needed.

    The cluster lives in a fixed temp-dir location so the one-time initdb
    cost (~11-15s) is paid only when the cluster is missing or the schema
    fingerprint changed. reset_pg_state wipes business tables before each
    test, so reusing the cluster carries no data-isolation risk.
    """
    if os.environ.get("MR_DATA_POSTGRES_DSN"):
        # User provided an external DSN; no need to start embedded PG.
        yield None
        return

    from mr_data.db.postgres import SCHEMA_SQL

    fingerprint = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()
    data_dir = Path(tempfile.gettempdir()) / f"mr-data-pgembed-test-{os.getuid()}"
    marker = data_dir / ".schema_fingerprint"

    def _start():
        # Drop the singleton cache entry so a retry gets a fresh manager.
        PgEmbedManager._instances.pop(str(data_dir.resolve()), None)
        manager = PgEmbedManager(data_dir=data_dir)
        return manager, manager.start()

    stale_schema = not marker.exists() or marker.read_text().strip() != fingerprint
    if stale_schema and data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        manager, dsn = _start()
    except Exception:
        # Stale postmaster lock or corrupted cluster: wipe and retry once.
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        manager, dsn = _start()
    marker.write_text(fingerprint)

    os.environ["MR_DATA_POSTGRES_DSN"] = dsn
    yield manager
    manager.stop()  # keep the cluster on disk for the next run


@pytest.fixture(scope="session")
def pg_available(pgembed_server):
    """Returns True if PostgreSQL is reachable."""
    dsn = os.environ.get("MR_DATA_POSTGRES_DSN")
    if not dsn and pgembed_server is not None:
        dsn = pgembed_server.get_dsn()
    if not dsn:
        return False
    try:
        store = PostgresStore(dsn=dsn)
        with store._cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture
def test_session_id():
    return f"test-{uuid.uuid4().hex[:8]}"


# All business tables managed by PostgresStore.init_schema (see db/postgres.py).
_PG_BUSINESS_TABLES = (
    "fixed_identity",
    "personality_dimensions",
    "sessions",
    "dialogue_logs",
    "dialogue_dimension_refs",
    "dialogue_vector_refs",
    "adjustment_logs",
    "user_identities",
)


@pytest.fixture(scope="session")
def _pg_schema_ready(pg_available):
    """Session 级一次性建表。SCHEMA_SQL 全部 IF NOT EXISTS，幂等；
    每测试重放 DDL 是纯浪费（~90ms × 百余测试）。PG 不可用时 skip，
    与 reset_pg_state 原守卫等价。"""
    pytest.importorskip("pgembed", reason="pgembed not installed")
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    PostgresStore().init_schema()


@pytest.fixture
def reset_pg_state(_pg_schema_ready):
    """Reset the shared pgembed database to a clean seeded state before each test.

    PG tests share one session-scoped database; without this fixture mutations
    (dimension flags, user identities, unprocessed dialogues) leak across tests.
    Schema creation is session-scoped (see _pg_schema_ready); here only data resets.
    """
    store = PostgresStore()
    with store._cursor(commit=True) as cur:
        cur.execute(
            f"TRUNCATE {', '.join(_PG_BUSINESS_TABLES)} RESTART IDENTITY CASCADE"
        )
    store.seed()


@pytest.fixture
def pg(reset_pg_state):
    """共享 pgembed 连接的 store；reset_pg_state 已保证逐测试 TRUNCATE+seed。"""
    return PostgresStore()


@pytest.fixture(scope="session", autouse=True)
def _cache_personality_pack_for_seed():
    """seed() 每个测试都经 load_personality_pack 重读人格文件（~40ms）。
    按解析路径缓存（wrapper 只作用于 postgres 模块内的引用；
    personality_loader 的直接调用方与 cli 的引用不受影响，
    且测试均不改变 settings.personality_file）。"""
    from mr_data.db import postgres as _postgres

    original = _postgres.load_personality_pack
    cache: dict = {}

    def cached(path=None):
        key = path or settings.personality_file
        if key not in cache:
            cache[key] = original(path)
        return cache[key]

    _postgres.load_personality_pack = cached
    yield
    _postgres.load_personality_pack = original


@pytest.fixture
def temp_log_dir(tmp_path, monkeypatch):
    """Provide a temporary log directory and reset logger cache."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(settings, "log_dir", str(log_dir))
    reset_loggers()
    yield str(log_dir)
    reset_loggers()


def open_log_propagation(caplog, monkeypatch, logger_name="mr_data.online"):
    """临时打开项目 logger 的传播，让 caplog 能捕获其记录。

    项目 logger 由 get_logger 配置为 propagate=False（日志只进 JSONL 文件），
    caplog 挂在 root 上，不打开传播捕获不到。
    """
    monkeypatch.setattr(logging.getLogger(logger_name), "propagate", True)
    caplog.set_level(logging.INFO, logger=logger_name)


def capture_events(caplog, event: str):
    """按 event 过滤 caplog 记录并按对象身份去重。

    同一 record 可能被 root 上的多个 pytest handler 重复收集，
    去重后才是真实的发射次数。
    """
    records = []
    for r in caplog.records:
        if getattr(r, "event", None) == event and r not in records:
            records.append(r)
    return records
