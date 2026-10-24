"""改进 65：检索链路修复（SearXNG 子路径、extract 保留、top_k 接线）。"""

from mr_data.config import settings
from mr_data.models import WebDoc, WebDocMetadata
from mr_data.online import DialogueGraph
from mr_data.online.page_extract import ExtractedPage
from mr_data.online.search_providers import SearxngProvider

from conftest import FakeLLMClient


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


def _web_doc(doc_id: str, url: str) -> WebDoc:
    return WebDoc(
        id=doc_id,
        page_content=f"snippet of {doc_id}",
        metadata=WebDocMetadata(url=url, title=doc_id),
    )


def test_extract_web_pages_keeps_docs_beyond_max_pages(
    chroma_store, monkeypatch
):
    """max_pages 之后的搜索结果不抓正文，但以 snippet 级内容保留在输出中。"""
    monkeypatch.setattr(settings, "web_extract_max_pages", 2)
    graph = _make_graph(chroma_store)

    class _StubExtractor:
        def extract(self, url):
            return ExtractedPage(text=f"full text of {url}", url=url)

    monkeypatch.setattr(graph, "page_extractor", _StubExtractor())

    docs = [
        _web_doc("web:a", "http://a.example"),
        _web_doc("web:b", "http://b.example"),
        _web_doc("web:c", "http://c.example"),
    ]
    state = {
        "session_id": "test-session",
        "user_input": "测试输入",
        "web_docs": docs,
    }
    result = graph._extract_web_pages(state)

    out = result["web_docs"]
    assert [d.id for d in out] == [
        # 前两条抓正文后 id 变为 real_url 的哈希
        out[0].id,
        out[1].id,
        "web:c",
    ]
    assert out[0].metadata.extracted is True
    assert out[1].metadata.extracted is True
    # 第三条原样保留（snippet 级）
    assert out[2].page_content == "snippet of web:c"
    assert out[2].metadata.extracted is False


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
    original = chroma_store.increment_memory_recall

    def _spy(doc_ids):
        captured["doc_ids"] = list(doc_ids)

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
