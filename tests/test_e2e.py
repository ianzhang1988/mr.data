import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore
from mr_data.models import (
    DialogueLog,
    DialogueLogMetadata,
    DialogueMemoryMetadata,
    MemoryDoc,
    PersonalityEvent,
    DialogueVectorRef,
    WebDoc,
    WebDocMetadata,
)
from mr_data.online import DialogueGraph
from mr_data.offline import AttributionEngine

pytestmark = pytest.mark.usefixtures("reset_pg_state")

def test_dialogue_graph(fake_llm, test_session_id, pg_available, chroma_store, temp_log_dir):
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.create_session(test_session_id)
    graph = DialogueGraph(pg_store=pg, chroma_store=chroma_store, llm=fake_llm, enable_web_search=False)

    reply = graph.chat(test_session_id, "你好")
    assert reply.text

    logs = pg.get_recent_dialogues(session_id=test_session_id)
    assert len(logs) >= 2

def test_offline_attribution(fake_llm, test_session_id, pg_available, chroma_store, temp_log_dir):
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.create_session(test_session_id)

    # Insert a user-assistant pair
    user_id = pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="user", content="测试输入"))
    assistant_id = pg.insert_dialogue(
        DialogueLog(
            session_id=test_session_id,
            role="assistant",
            content="测试回复",
            metadata=DialogueLogMetadata(inner_monologue="用户似乎在测试我"),
        )
    )

    # Close the session before running attribution
    pg.close_session(test_session_id)

    # 改进 54 移除 fallback 后，需显式指定归因 target 才会写 evidence/vector_refs。
    fake_llm.attribution_target_log_id = assistant_id

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    engine.run()

    # Both should be marked processed
    unprocessed = pg.get_recent_dialogues(session_id=test_session_id, unprocessed_only=True)
    assert len(unprocessed) == 0

    # Evidence should be written to the personality collection and linked via vector refs.
    personality_docs = chroma_store.query_personality("测试回复", top_k=10)
    assert any("测试回复" in doc.page_content for doc in personality_docs)

    refs = pg.get_dialogue_vector_refs_by_dimension(1)
    assert any(ref.dialogue_log_id == assistant_id for ref in refs)

def test_chat_turn_preserves_core_flag(fake_llm, test_session_id, pg_available, chroma_store):
    if not pg_available:
        pytest.skip("PostgreSQL not available")
    pg = PostgresStore()
    pg.create_session(test_session_id)
    graph = DialogueGraph(pg_store=pg, chroma_store=chroma_store, llm=fake_llm, enable_web_search=False)

    # Mark dimension 2 as core and run a chat turn.
    with pg._cursor(commit=True) as cur:
        cur.execute("UPDATE personality_dimensions SET core = TRUE WHERE id = 2")

    reply = graph.chat(test_session_id, "你好")
    assert reply.text

    # Dimension 2 should keep its core flag.
    dim = pg.get_dimension(2)
    assert dim is not None
    assert dim.core is True

def test_web_docs_written_to_memory(fake_llm, test_session_id, pg_available, chroma_store, monkeypatch):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    class FakeWebSearch:
        def search(self, query: str) -> list[WebDoc]:
            return [
                WebDoc(
                    page_content="太阳系有八大行星",
                    metadata=WebDocMetadata(
                        url="http://example.com/planets",
                        title="行星",
                    ),
                )
            ]

    pg = PostgresStore()
    pg.create_session(test_session_id)

    # Force the think node to decide web search is needed for this turn.
    def _force_web_search(system_prompt, user_prompt, response_format, temperature=0.2):
        return {
            "inner_monologue": "需要搜索",
            "personality_query": user_prompt,
            "memory_query": user_prompt,
            "needs_web_search": True,
            "search_query": user_prompt,
        }

    monkeypatch.setattr(fake_llm, "chat_structured", _force_web_search)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        web_search=FakeWebSearch(),
        enable_web_search=True,
    )
    reply = graph.chat(test_session_id, "太阳系有几颗行星")
    assert reply.text

    # Web memories are global knowledge, not scoped to a single session.
    # 正文原样入片（无 [网络资料] 包装、无 URL 混入），id 为 web 分片形态。
    docs = chroma_store.query_memories("八大行星", top_k=10)
    matched = [
        d for d in docs
        if "八大行星" in d.page_content
        and "[网络资料]" not in d.page_content
        and "http://" not in d.page_content
    ]
    assert matched
    for d in matched:
        assert d.metadata.source_type == "web"
        assert d.metadata.url == "http://example.com/planets"
        assert d.metadata.title == "行星"
        assert d.metadata.chunk_index == 0
        assert d.id.startswith("web:") and d.id.endswith(":c0")

def test_full_session_lifecycle(fake_llm, pg_available, chroma_store, temp_log_dir):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    graph = DialogueGraph(pg_store=pg, chroma_store=chroma_store, llm=fake_llm, enable_web_search=False)

    # First session
    session_a = pg.create_session()
    reply_a = graph.chat(session_a, "你好")
    assert reply_a.text
    pg.close_session(session_a)

    # Second session
    session_b = pg.create_session()
    reply_b = graph.chat(session_b, "今天天气怎么样")
    assert reply_b.text
    pg.close_session(session_b)

    # Run offline attribution
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    engine.run()

    # Both sessions should be fully processed
    for sid in (session_a, session_b):
        unprocessed = pg.get_recent_dialogues(session_id=sid, unprocessed_only=True)
        assert len(unprocessed) == 0, f"Session {sid} has unprocessed dialogues"

    # Adjustment logs should reference the sessions
    with pg._cursor() as cur:
        cur.execute("SELECT session_id FROM adjustment_logs WHERE session_id IN (%s, %s)", (session_a, session_b))
        rows = cur.fetchall()
    assert len(rows) >= 2
    assert all(row["session_id"] in (session_a, session_b) for row in rows)

def test_dimension_failure_threshold_purges_vectors(fake_llm, test_session_id, pg_available, chroma_store):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    # Insert a dummy dialogue log to own the pre-seeded evidence ref (FK requirement).
    dummy_id = pg.insert_dialogue(
        DialogueLog(session_id=test_session_id, role="assistant", content="legacy")
    )

    # Pre-seed an evidence doc tied to dimension 1 so we can verify cleanup.
    event = PersonalityEvent(
        content="即将被清理的旧证据",
        dimension_ids=[1],
        source_type="evidence",
        source_id="legacy",
    )
    doc_id = chroma_store.add_personality_event(event)
    pg.insert_dialogue_vector_refs(
        dummy_id,
        [DialogueVectorRef(
            dialogue_log_id=dummy_id,
            vector_doc_id=doc_id,
            source_type="evidence",
            content=event.content,
            dimension_ids=[1],
        )],
    )

    # Ensure dimension 1 is not core and set it to the minimum sample count
    # of failures so the incoming attribution triggers pruning.
    with pg._cursor(commit=True) as cur:
        cur.execute(
            "UPDATE personality_dimensions SET core = FALSE, success_count = 0, failure_count = %s WHERE id = 1",
            (settings.failure_min_samples,),
        )

    # Insert a dialogue pair for the closed session.
    pg.insert_dialogue(DialogueLog(session_id=test_session_id, role="user", content="测试输入"))
    assistant_id = pg.insert_dialogue(
        DialogueLog(session_id=test_session_id, role="assistant", content="测试回复")
    )
    pg.close_session(test_session_id)

    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    engine.run()

    # Dimension 1 should be deactivated.
    dim = pg.get_dimension(1)
    assert dim is not None
    assert dim.active is False

    # Vector refs tied to dimension 1 should be removed.
    refs = pg.get_dialogue_vector_refs_by_dimension(1)
    assert len(refs) == 0

    # The pre-seeded evidence doc should also be gone from Chroma.
    remaining = chroma_store.get_personality_event_ids_by_dimension(1)
    assert doc_id not in remaining

def test_global_memory_recall(
    fake_llm, pg_available, chroma_store, temp_log_dir
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()

    session_a = pg.create_session()
    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )
    graph.chat(session_a, "我最喜欢的颜色是蓝色")
    pg.close_session(session_a)

    # Run offline attribution to persist session A dialogue to memories.
    engine = AttributionEngine(pg_store=pg, chroma_store=chroma_store, llm=fake_llm)
    engine.run()

    session_b = pg.create_session()
    final_state = graph.graph.invoke(
        {"session_id": session_b, "user_input": "你还记得我喜欢什么颜色吗"}
    )

    memory_docs = final_state.get("memory_docs", [])
    assert any(
        d.metadata.session_id == session_a
        for d in memory_docs
    ), "Session B should retrieve memory from session A"

    # The recalled session A dialogue memory should have its recall_count incremented.
    dialogue_docs = [
        d for d in memory_docs
        if d.metadata.session_id == session_a
        and d.metadata.source_type == "dialogue"
    ]
    assert dialogue_docs
    assert all(d.metadata.recall_count >= 1 for d in dialogue_docs)

def test_memory_relevance_filter(fake_llm, test_session_id, pg_available, chroma_store, monkeypatch):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    chroma_store.add_memory(test_session_id, "我喜欢蓝色", metadata=DialogueMemoryMetadata(session_id=test_session_id))
    chroma_store.add_memory(test_session_id, "今天天气很好", metadata=DialogueMemoryMetadata(session_id=test_session_id))

    original_chat_structured = fake_llm.chat_structured

    def _patched_chat_structured(system_prompt, user_prompt, response_format, temperature=0.2):
        if response_format.__name__ == "MemoryRelevanceFilterResult":
            return {
                "results": [
                    {"index": 0, "is_relevant": True},
                    {"index": 1, "is_relevant": False},
                ]
            }
        return original_chat_structured(system_prompt, user_prompt, response_format, temperature)

    monkeypatch.setattr(fake_llm, "chat_structured", _patched_chat_structured)
    monkeypatch.setattr(settings, "enable_memory_relevance_filter", True)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )

    state = graph._filter_memory_docs(
        {
            "session_id": test_session_id,
            "user_input": "你喜欢什么颜色",
            "memory_docs": chroma_store.query_memories("颜色", session_id=test_session_id, top_k=10),
        }
    )

    assert len(state["memory_docs"]) == 1
    assert "蓝色" in state["memory_docs"][0].page_content

def test_assemble_and_generate_returns_references(
    fake_llm, test_session_id, pg_available, chroma_store, monkeypatch
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    original_chat_structured = fake_llm.chat_structured

    def _patched_chat_structured(system_prompt, user_prompt, response_format, temperature=0.2):
        name = response_format.__name__
        if name == "ThinkDecision":
            return {
                "inner_monologue": "需要搜索",
                "personality_query": user_prompt,
                "memory_query": user_prompt,
                "needs_web_search": True,
                "search_query": user_prompt,
            }
        return original_chat_structured(system_prompt, user_prompt, response_format, temperature)

    monkeypatch.setattr(fake_llm, "chat_structured", _patched_chat_structured)

    # The final reply generation now goes through the messages-based structured_chat.
    original_structured_chat = fake_llm.structured_chat

    def _patched_structured_chat(messages, response_format, temperature=0.2):
        if response_format.__name__ == "AssistantReply":
            return {
                "text": "根据参考资料，太阳系有八大行星。",
                "blocks": [
                    {
                        "text": "根据参考资料，太阳系有八大行星。",
                        "references": [
                            {"id": "http://example.com/planets", "source_type": "web", "summary": "太阳系有八大行星"},
                            {"id": "fake-id", "source_type": "memory", "summary": "不存在的素材"},
                        ],
                    }
                ],
            }
        return original_structured_chat(messages, response_format, temperature)

    monkeypatch.setattr(fake_llm, "structured_chat", _patched_structured_chat)

    class FakeWebSearch:
        def search(self, query: str) -> list[WebDoc]:
            return [
                WebDoc(
                    page_content="太阳系有八大行星",
                    metadata=WebDocMetadata(
                        url="http://example.com/planets",
                        title="行星",
                    ),
                )
            ]

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        web_search=FakeWebSearch(),
        enable_web_search=True,
    )

    result = graph.chat(test_session_id, "太阳系有几颗行星")
    assert result.text
    all_refs = [
        ref for block in result.blocks for ref in block.references
    ]
    assert any(
        ref.id == "http://example.com/planets" and ref.source_type == "web"
        for ref in all_refs
    )
    assert not any(ref.id == "fake-id" for ref in all_refs)

def test_web_doc_without_url_does_not_register_empty_ref_key(
    fake_llm, pg_available, chroma_store, monkeypatch
):
    """改进 78：无 url 的 web doc 不注册空 known_refs 键，LLM 回传空 id 引用
    被丢弃；有 url 的 doc 仍以 url 为引用键。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    session_id = pg.create_session()

    original_chat_structured = fake_llm.chat_structured

    def _patched_chat_structured(system_prompt, user_prompt, response_format, temperature=0.2):
        name = response_format.__name__
        if name == "ThinkDecision":
            return {
                "inner_monologue": "需要搜索",
                "personality_query": user_prompt,
                "memory_query": user_prompt,
                "needs_web_search": True,
                "search_query": user_prompt,
            }
        return original_chat_structured(system_prompt, user_prompt, response_format, temperature)

    monkeypatch.setattr(fake_llm, "chat_structured", _patched_chat_structured)

    original_structured_chat = fake_llm.structured_chat

    def _patched_structured_chat(messages, response_format, temperature=0.2):
        if response_format.__name__ == "AssistantReply":
            return {
                "text": "根据参考资料回复。",
                "blocks": [
                    {
                        "text": "根据参考资料回复。",
                        "references": [
                            {"id": "", "source_type": "web", "summary": "空 id 引用"},
                            {"id": "http://example.com/planets", "source_type": "web", "summary": "太阳系有八大行星"},
                        ],
                    }
                ],
            }
        return original_structured_chat(messages, response_format, temperature)

    monkeypatch.setattr(fake_llm, "structured_chat", _patched_structured_chat)

    class FakeWebSearch:
        def search(self, query: str) -> list[WebDoc]:
            return [
                WebDoc(
                    page_content="无链接摘要",
                    metadata=WebDocMetadata(url="", title="无链接"),
                ),
                WebDoc(
                    page_content="太阳系有八大行星",
                    metadata=WebDocMetadata(
                        url="http://example.com/planets",
                        title="行星",
                    ),
                ),
            ]

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        web_search=FakeWebSearch(),
        enable_web_search=True,
    )

    result = graph.chat(session_id, "太阳系有几颗行星")
    assert result.text
    all_refs = [
        ref for block in result.blocks for ref in block.references
    ]
    assert not any(ref.id == "" for ref in all_refs)
    assert any(
        ref.id == "http://example.com/planets" and ref.source_type == "web"
        for ref in all_refs
    )

def test_web_memory_refetch_is_idempotent(fake_llm, pg_available, chroma_store, monkeypatch):
    """同 URL 同正文重抓：add_memory 幂等，同页原地刷新，不新增分片、

    也不重置 recall_count 等既有元数据。
    """
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    url = "http://example.com/planets"
    content = "太阳系有八大行星" + "，围绕太阳运转" * 60  # 长正文，确保切成多片

    class FakeWebSearch:
        def search(self, query: str) -> list[WebDoc]:
            return [
                WebDoc(
                    page_content=content,
                    metadata=WebDocMetadata(
                        url=url,
                        title="行星",
                    ),
                )
            ]

    original_chat_structured = fake_llm.chat_structured

    def _patched_chat_structured(system_prompt, user_prompt, response_format, temperature=0.2):
        name = response_format.__name__
        if name == "ThinkDecision":
            return {
                "inner_monologue": "需要搜索",
                "personality_query": user_prompt,
                "memory_query": user_prompt,
                "needs_web_search": True,
                "search_query": user_prompt,
            }
        return original_chat_structured(system_prompt, user_prompt, response_format, temperature)

    monkeypatch.setattr(fake_llm, "chat_structured", _patched_chat_structured)

    pg = PostgresStore()

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        web_search=FakeWebSearch(),
        enable_web_search=True,
    )

    session_a = pg.create_session()
    graph.chat(session_a, "太阳系有几颗行星")
    pg.close_session(session_a)

    # 第一轮后记录该页的分片集合与元数据。
    first = chroma_store.memories.get(include=["metadatas"])
    page_ids = sorted(
        doc_id for doc_id, meta in zip(first["ids"], first["metadatas"])
        if meta.get("url") == url
    )
    assert page_ids
    assert len(page_ids) > 1  # 长正文应切成多片
    first_meta_by_id = dict(zip(first["ids"], first["metadatas"]))

    session_b = pg.create_session()
    graph.chat(session_b, "太阳系有几颗行星")
    pg.close_session(session_b)

    # 重抓幂等：第二轮不新增该页分片，既有分片的 recall_count 未被重置。
    second = chroma_store.memories.get(ids=page_ids, include=["metadatas"])
    assert sorted(second["ids"]) == page_ids
    for doc_id, meta in zip(second["ids"], second["metadatas"]):
        assert meta.get("url") == url
        assert meta.get("recall_count", 0) >= first_meta_by_id[doc_id].get("recall_count", 0)

def test_assemble_respects_token_limit(
    fake_llm, test_session_id, pg_available, chroma_store, monkeypatch
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    monkeypatch.setattr(settings, "llm_context_token_limit", 50)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )

    compress_calls = []

    def fake_compress(text, target):
        compress_calls.append((text, target))
        return "[compressed]"

    monkeypatch.setattr(
        graph.prompt_assembler, "_compress_text", fake_compress)

    state = {
        "session_id": test_session_id,
        "user_input": "short question",
        "identity": pg.get_identity(),
        "user_identity": None,
        "dimensions": pg.list_dimensions(active_only=True),
        "selected_dimension_ids": [],
        "personality_docs": [],
        "memory_docs": [
            MemoryDoc(id="m1", page_content="a" * 300, metadata=DialogueMemoryMetadata())
        ],
        "messages": [],
        "web_docs": [],
        "inner_monologue": None,
    }

    graph._assemble_and_generate(state)
    assert len(compress_calls) > 0
