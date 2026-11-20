"""改进 65：检索链路修复（SearXNG 子路径、extract 保留、top_k 接线）。"""

import pytest

from mr_data.config import settings
from mr_data.models import WebDoc, WebDocMetadata
from mr_data.online import DialogueGraph
from mr_data.online.page_extract import ExtractedPage
from mr_data.online.search_providers import SearxngProvider

from conftest import FakeLLMClient, capture_events, open_log_propagation


def test_searxng_preserves_base_url_subpath(monkeypatch):
    """base_url 带子路径前缀时，/search 应拼接在子路径之后而非域名根。"""
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    def _fake_get(url, **kwargs):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr("mr_data.online.search_providers.requests.get", _fake_get)

    provider = SearxngProvider(base_url="http://localhost:8080/searxng/")
    provider.search("test")

    assert captured["url"] == "http://localhost:8080/searxng/search"


def _make_graph(chroma_store):
    # 节点级测试不触 PG，用占位对象避免启动 pgembed。
    return DialogueGraph(
        pg_store=object(),
        chroma_store=chroma_store,
        llm=FakeLLMClient(),
        enable_web_search=False,
    )


def _web_doc(url: str) -> WebDoc:
    return WebDoc(
        page_content=f"snippet of {url}",
        metadata=WebDocMetadata(url=url, title=url),
    )


@pytest.mark.parametrize("keep_reason", ["beyond_max_pages", "extraction_fails"])
def test_extract_web_pages_keeps_docs_beyond_limit_or_on_failure(
    chroma_store, monkeypatch, keep_reason
):
    """不抓正文的 doc 原样保留：max_pages 之后的搜索结果以 snippet 级内容保留；
    抽取失败（extract 返回 None）时原 doc 原样返回（改进 78/79：抽取不重写身份）。"""
    monkeypatch.setattr(settings, "web_extract_max_pages", 2)
    graph = _make_graph(chroma_store)

    if keep_reason == "beyond_max_pages":
        class _StubExtractor:
            def extract(self, url):
                return ExtractedPage(text=f"full text of {url}", url=url)

        monkeypatch.setattr(graph, "page_extractor", _StubExtractor())
        docs = [
            _web_doc("http://a.example"),
            _web_doc("http://b.example"),
            _web_doc("http://c.example"),
        ]
    else:
        class _FailingExtractor:
            def extract(self, url):
                return None

        monkeypatch.setattr(graph, "page_extractor", _FailingExtractor())
        docs = [_web_doc("http://a.example")]

    state = {
        "session_id": "test-session",
        "user_input": "测试输入",
        "web_docs": docs,
    }
    result = graph._extract_web_pages(state)

    out = result["web_docs"]
    if keep_reason == "beyond_max_pages":
        # 改进 78/79：抽取不重写身份，url 原样保留。
        assert [d.metadata.url for d in out] == [
            "http://a.example",
            "http://b.example",
            "http://c.example",
        ]
        assert out[0].metadata.extracted is True
        assert out[1].metadata.extracted is True
        # 第三条原样保留（snippet 级）
        assert out[2].page_content == "snippet of http://c.example"
        assert out[2].metadata.extracted is False
    else:
        assert out[0] == docs[0]


def test_retrieve_personality_uses_configured_top_k(chroma_store, monkeypatch):
    """_retrieve_personality 应使用 settings.personality_retrieval_top_k。"""
    graph = _make_graph(chroma_store)
    captured = {}
    original = chroma_store.query_personality

    def _spy(query, top_k):
        captured["top_k"] = top_k
        return original(query, top_k=top_k)

    monkeypatch.setattr(chroma_store, "query_personality", _spy)
    monkeypatch.setattr(settings, "personality_retrieval_top_k", 7)

    state = {
        "session_id": "test-session",
        "user_input": "测试输入",
        "personality_query": "测试性格查询",
        "retrieval_query": "测试性格查询",
    }
    graph._retrieve_personality(state)

    assert captured["top_k"] == 7


def test_retrieve_memories_failure_returns_empty_docs(chroma_store, monkeypatch):
    """query_memories 抛异常时回退空列表，不炸当轮对话。"""
    graph = _make_graph(chroma_store)

    def _boom(*args, **kwargs):
        raise RuntimeError("chroma memory query failed")

    monkeypatch.setattr(chroma_store, "query_memories", _boom)

    state = {
        "session_id": "test-session",
        "user_input": "测试输入",
        "memory_query": "测试记忆查询",
        "retrieval_query": "测试记忆查询",
    }
    result = graph._retrieve_memories(state)

    assert result["memory_docs"] == []


def test_retrieve_memories_recall_increment_failure_keeps_docs(
    chroma_store, monkeypatch
):
    """increment_memory_recall 抛异常时已检索到的记忆仍返回，recall_count 不变。"""
    from mr_data.models import DialogueMemoryMetadata, MemoryDoc

    graph = _make_graph(chroma_store)
    doc = MemoryDoc(
        id="mem:1",
        page_content="过往对话片段",
        metadata=DialogueMemoryMetadata(session_id="old-session"),
    )

    monkeypatch.setattr(chroma_store, "query_memories", lambda *a, **k: [doc])

    def _boom(*args, **kwargs):
        raise RuntimeError("recall increment failed")

    monkeypatch.setattr(chroma_store, "increment_memory_recall", _boom)

    state = {
        "session_id": "test-session",
        "user_input": "测试输入",
        "memory_query": "测试记忆查询",
        "retrieval_query": "测试记忆查询",
    }
    result = graph._retrieve_memories(state)

    assert result["memory_docs"] == [doc]
    assert result["memory_docs"][0].metadata.recall_count == 0


def test_retrieve_memories_counts_web_recall(chroma_store, monkeypatch):
    """web 记忆命中同样接入召回计数：increment 收到 web id，state 内 doc 的
    recall_count/last_recalled_at 被刷新（prune 超时基准）。"""
    from mr_data.models import MemoryDoc, WebMemoryMetadata

    graph = _make_graph(chroma_store)
    doc = MemoryDoc(
        id="web:abc",
        page_content="[网络资料] 标题\n内容",
        metadata=WebMemoryMetadata(url="https://example.com", title="标题"),
    )

    monkeypatch.setattr(chroma_store, "query_memories", lambda *a, **k: [doc])

    captured = {}

    def _spy(doc_ids):
        captured["doc_ids"] = list(doc_ids)
        # 与真实 ChromaStore 契约一致：返回实际命中的 id 集合。
        return list(doc_ids)

    monkeypatch.setattr(chroma_store, "increment_memory_recall", _spy)

    state = {
        "session_id": "test-session",
        "user_input": "测试输入",
        "memory_query": "测试记忆查询",
        "retrieval_query": "测试记忆查询",
    }
    result = graph._retrieve_memories(state)

    assert captured["doc_ids"] == ["web:abc"]
    meta = result["memory_docs"][0].metadata
    assert meta.recall_count == 1
    assert meta.last_recalled_at


def test_retrieve_events_log_effective_query(chroma_store, monkeypatch, caplog):
    """F-18：retrieve.* 事件的 details.query 记实际生效查询词（think 改写后的
    search_query/personality_query/memory_query），而非 retrieval_query。"""
    graph = DialogueGraph(
        pg_store=object(),
        chroma_store=chroma_store,
        llm=FakeLLMClient(),
        enable_web_search=True,
    )
    open_log_propagation(caplog, monkeypatch)  # 须在 graph 构造后（构造会重置 propagate）

    class _StubWebSearch:
        def search(self, query):
            return []

    monkeypatch.setattr(graph, "web_search", _StubWebSearch())
    monkeypatch.setattr(chroma_store, "query_personality", lambda *a, **k: [])
    monkeypatch.setattr(chroma_store, "query_memories", lambda *a, **k: [])

    state = {
        "session_id": "test-session",
        "user_input": "用户原始输入",
        "retrieval_query": "统一改写检索词",
        "search_query": "改写后的搜索词",
        "personality_query": "改写后的性格词",
        "memory_query": "改写后的记忆词",
    }
    graph._retrieve_web(state)
    graph._retrieve_personality(state)
    graph._retrieve_memories(state)

    web = capture_events(caplog, "retrieve.web")
    personality = capture_events(caplog, "retrieve.personality")
    memory = capture_events(caplog, "retrieve.memory")
    assert len(web) == len(personality) == len(memory) == 1
    assert web[0].details["query"] == "改写后的搜索词"
    assert personality[0].details["query"] == "改写后的性格词"
    assert memory[0].details["query"] == "改写后的记忆词"


def test_retrieve_memories_recall_patch_only_found_ids(chroma_store, monkeypatch):
    """F-20：increment_memory_recall 返回实际命中集合 found_ids，内存补丁只给
    命中文档 +1；未命中（如存储中已不存在）的文档 recall_count 保持原值。"""
    from mr_data.models import DialogueMemoryMetadata, MemoryDoc

    graph = _make_graph(chroma_store)
    docs = [
        MemoryDoc(
            id="mem:hit",
            page_content="命中记忆",
            metadata=DialogueMemoryMetadata(session_id="old-session"),
        ),
        MemoryDoc(
            id="mem:miss",
            page_content="未命中记忆",
            metadata=DialogueMemoryMetadata(session_id="old-session"),
        ),
    ]

    monkeypatch.setattr(chroma_store, "query_memories", lambda *a, **k: docs)
    # 模拟 Chroma 实际命中集合：只命中 mem:hit（mem:miss 在库中已不存在）。
    monkeypatch.setattr(
        chroma_store, "increment_memory_recall", lambda doc_ids: ["mem:hit"]
    )

    state = {
        "session_id": "test-session",
        "user_input": "测试输入",
        "memory_query": "测试记忆查询",
        "retrieval_query": "测试记忆查询",
    }
    result = graph._retrieve_memories(state)

    by_id = {doc.id: doc for doc in result["memory_docs"]}
    assert by_id["mem:hit"].metadata.recall_count == 1
    assert by_id["mem:hit"].metadata.last_recalled_at
    assert by_id["mem:miss"].metadata.recall_count == 0
    assert not by_id["mem:miss"].metadata.last_recalled_at

