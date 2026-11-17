"""Regression tests for DialogueGraph state handling.

Covers:
- Removal of the state reducers (web-doc filtering must not be undone by a
  merge-by-id union, and loaded dialogue messages must not be duplicated
  across linear graph nodes).
- ``selected_dimension_ids`` taking effect in prompt assembly and dialogue
  dimension-ref logging.
- 改进 74：最近对话轮还原为真实 user/assistant 消息序列拼入 llm_messages。
"""

import json
import re
from pathlib import Path

import pytest

from mr_data.config import settings
from mr_data.db import PostgresStore
from mr_data.db.chroma import web_chunk_base_id
from mr_data.models import (
    DialogueLog,
    DialogueLogMetadata,
    DialogueMemoryMetadata,
    MemoryDoc,
    ReplyBlock,
    ReplyReference,
    WebDoc,
    WebDocMetadata,
)
from mr_data.online import DialogueGraph

from conftest import FakeLLMClient, capture_events, open_log_propagation

pytestmark = pytest.mark.usefixtures("reset_pg_state")

class FakeWebSearch:
    def __init__(self, docs: list[WebDoc]):
        self._docs = docs

    def search(self, query: str) -> list[WebDoc]:
        return self._docs

def _force_web_search(system_prompt, user_prompt, response_format, temperature=0.2):
    return {
        "inner_monologue": "需要搜索",
        "personality_query": user_prompt,
        "memory_query": user_prompt,
        "needs_web_search": True,
        "search_query": user_prompt,
    }

def _capture_fit_sections(graph, monkeypatch):
    """Patch graph.prompt_assembler.fit_sections to capture the raw sections."""
    captured: dict = {}
    original_fit = graph.prompt_assembler.fit_sections

    def spy(sections, limit):
        captured["sections"] = sections
        return original_fit(sections, limit)

    monkeypatch.setattr(graph.prompt_assembler, "fit_sections", spy)
    return captured

def test_web_doc_filtering_not_undone_by_state_merge(
    fake_llm, test_session_id, pg_available, chroma_store, temp_log_dir, monkeypatch
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    docs = [
        WebDoc(
            page_content="太阳系有八大行星",
            metadata=WebDocMetadata(
                url="http://example.com/planets",
                title="行星",
            ),
        ),
        WebDoc(
            page_content="完全无关的蛋糕食谱",
            metadata=WebDocMetadata(
                url="http://example.com/cake",
                title="蛋糕",
            ),
        ),
    ]

    original_chat_structured = fake_llm.chat_structured

    def _patched_chat_structured(system_prompt, user_prompt, response_format, temperature=0.2):
        name = response_format.__name__
        if name == "ThinkDecision":
            return _force_web_search(system_prompt, user_prompt, response_format, temperature)
        if name == "WebDocExtraction":
            if "蛋糕" in user_prompt:
                return {"is_relevant": False, "extracted_text": ""}
            return {"is_relevant": True, "extracted_text": ""}
        return original_chat_structured(system_prompt, user_prompt, response_format, temperature)

    monkeypatch.setattr(fake_llm, "chat_structured", _patched_chat_structured)
    monkeypatch.setattr(settings, "enable_web_doc_extraction", True)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        web_search=FakeWebSearch(docs),
        enable_web_search=True,
    )

    reply = graph.chat(test_session_id, "太阳系有几颗行星")
    assert reply.text

    # The irrelevant doc must stay filtered out of the logged web vector refs.
    with pg._cursor() as cur:
        cur.execute(
            """
            SELECT r.vector_doc_id
            FROM dialogue_vector_refs r
            JOIN dialogue_logs l ON l.id = r.dialogue_log_id
            WHERE l.session_id = %s AND r.source_type = 'web'
            """,
            (test_session_id,),
        )
        web_ref_ids = {row["vector_doc_id"] for row in cur.fetchall()}

    # 改进 76：PG 引用登记粒度为分片，期望登记保留文档的片 id。
    expected_chunk_id = (
        f"{web_chunk_base_id('http://example.com/planets', '太阳系有八大行星')}:c0"
    )
    assert web_ref_ids == {expected_chunk_id}

def test_extract_web_pages_fills_original_content(
    fake_llm, chroma_store, temp_log_dir, monkeypatch
):
    """C3：抽取成功组装 doc 时填 original_content（纯抽取正文，不含 title）——

    记忆入库与分片哈希的基准；snippet 透传/抽取失败路径保持 None。"""
    monkeypatch.setattr(settings, "web_extract_max_pages", 1)

    from mr_data.online.page_extract import ExtractedPage

    class _StubExtractor:
        def extract(self, url):
            return ExtractedPage(text="纯正文内容", url=url)

    graph = DialogueGraph(
        pg_store=PostgresStore(),
        chroma_store=chroma_store,
        llm=fake_llm,
        web_search=FakeWebSearch([]),
        enable_web_search=True,
    )
    graph.page_extractor = _StubExtractor()
    docs = [
        WebDoc(
            page_content="snippet",
            metadata=WebDocMetadata(url="http://x.com/a", title="标题"),
        ),
        WebDoc(
            page_content="snippet-only",
            metadata=WebDocMetadata(url="", title="无url"),
        ),
    ]

    state = graph._extract_web_pages({"session_id": "s", "web_docs": docs})

    extracted_doc, passthrough_doc = state["web_docs"]
    assert extracted_doc.metadata.extracted is True
    assert extracted_doc.page_content == "标题\n纯正文内容"
    assert extracted_doc.original_content == "纯正文内容"  # 不含 title
    assert passthrough_doc.original_content is None  # 未抽取路径退化

def _make_graph_for_memory_filter(fake_llm, chroma_store, monkeypatch):
    monkeypatch.setattr(settings, "enable_memory_relevance_filter", True)
    return DialogueGraph(
        pg_store=PostgresStore(),
        chroma_store=chroma_store,
        llm=fake_llm,
        web_search=FakeWebSearch([]),
        enable_web_search=False,
    )

def _memory_filter_overhead(user_input: str) -> int:
    from mr_data.online import graph as graph_mod

    system = "判断下列记忆内容是否与用户输入相关。请为每条记忆返回是否相关。"
    return graph_mod._token_counter.count(
        system + f"用户输入：{user_input}\n\n记忆列表：\n\n\n请返回每条记忆的相关性判断。"
    )

@pytest.mark.parametrize("over_budget", [True, False])
def test_memory_filter_proportional_compression(
    fake_llm, chroma_store, temp_log_dir, monkeypatch, caplog, over_budget
):
    """D：整份过滤 prompt 超 memory_relevance_filter_max_tokens 时按条数等比压缩

    每条展示文本（水填充同哲学，源 doc 不动），记
    retrieve.memory_filter_prompt_truncated。预算内各条展示原文，无压缩无日志
    （旧 1500 字内场景零变化）。"""
    user_input = "你喜欢什么颜色"
    if over_budget:
        long_text = "关于星际旅行的记忆片段，包含曲速引擎的细节。" * 40  # ~720 字
        mid_text = "关于蓝色海洋的记忆。" * 10
        short_text = "喜欢蓝色"
        docs = [
            MemoryDoc(id="d0", page_content=long_text,
                      metadata=DialogueMemoryMetadata(session_id="s")),
            MemoryDoc(id="d1", page_content=mid_text,
                      metadata=DialogueMemoryMetadata(session_id="s")),
            MemoryDoc(id="d2", page_content=short_text,
                      metadata=DialogueMemoryMetadata(session_id="s")),
        ]
        # 记忆展示总预算仅 30 token
        max_tokens = _memory_filter_overhead(user_input) + 30
        llm_results = [{"index": i, "is_relevant": True} for i in range(3)]
    else:
        docs = [
            MemoryDoc(id="d0", page_content="我喜欢蓝色",
                      metadata=DialogueMemoryMetadata(session_id="s")),
            MemoryDoc(id="d1", page_content="今天天气很好",
                      metadata=DialogueMemoryMetadata(session_id="s")),
        ]
        max_tokens = 32768
        llm_results = [{"index": 0, "is_relevant": True}]
    monkeypatch.setattr(settings, "memory_relevance_filter_max_tokens", max_tokens)
    graph = _make_graph_for_memory_filter(fake_llm, chroma_store, monkeypatch)
    open_log_propagation(caplog, monkeypatch)  # 须在引擎构造后（构造会重置 propagate）

    captured: dict = {}
    original = fake_llm.chat_structured

    def _patched(system_prompt, user_prompt, response_format, temperature=0.2):
        if response_format.__name__ == "MemoryRelevanceFilterResult":
            captured["prompt"] = user_prompt
            return {"results": llm_results}
        return original(system_prompt, user_prompt, response_format, temperature)

    monkeypatch.setattr(fake_llm, "chat_structured", _patched)

    state = graph._filter_memory_docs(
        {"session_id": "s", "user_input": user_input, "memory_docs": docs}
    )

    prompt = captured["prompt"]
    if over_budget:
        # 三条全部保留（LLM 全判相关），但展示文本被等比压缩
        assert len(state["memory_docs"]) == 3
        assert "[记忆过长，已省略后续内容]" in prompt
        assert long_text not in prompt  # 长片不再整段进 prompt
        body = prompt.split("记忆列表：\n", 1)[1].rsplit("\n\n请返回", 1)[0]
        fitted = dict(
            (int(i), t) for i, t in re.findall(r"\[(\d+)\] (.*?)(?=\n\n\[\d+\] |$)", body, re.S)
        )
        assert len(fitted) == 3
        assert len(fitted[0]) > len(fitted[1]) > 0  # 等比：长片分到的展示预算更多
        assert fitted[2].endswith("[记忆过长，已省略后续内容]")
        assert docs[0].page_content == long_text  # 源 doc 本体不动
        assert docs[2].page_content == short_text
        events = capture_events(caplog, "retrieve.memory_filter_prompt_truncated")
        assert len(events) == 1
        details = events[0].details
        assert details["original_tokens"] > details["truncated_tokens"]
        assert details["doc_count"] == 3
    else:
        assert len(state["memory_docs"]) == 1
        assert "我喜欢蓝色" in prompt
        assert "[记忆过长" not in prompt
        assert capture_events(caplog, "retrieve.memory_filter_prompt_truncated") == []

class _RecordingReplyLLM(FakeLLMClient):
    """记录 AssistantReply 调用时传入的 llm_messages。"""

    def __init__(self):
        super().__init__()
        self.reply_messages: list[dict] = []

    def structured_chat(self, messages, response_format, temperature=0.2):
        if response_format.__name__ == "AssistantReply":
            self.reply_messages = messages
        return super().structured_chat(messages, response_format, temperature)

def _seed_turn(
    pg,
    session_id: str,
    question: str,
    answer: str,
    inner_monologue=None,
    blocks=None,
):
    """在 Postgres 中写入一对 user/assistant 对话。"""
    pg.insert_dialogue(DialogueLog(session_id=session_id, role="user", content=question))
    pg.insert_dialogue(
        DialogueLog(
            session_id=session_id,
            role="assistant",
            content=answer,
            metadata=DialogueLogMetadata(
                inner_monologue=inner_monologue, blocks=blocks or []
            ),
        )
    )

def _setup_seeded_graph(pg, chroma_store, llm):
    return DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=llm,
        enable_web_search=False,
    )

def test_history_messages_not_duplicated(
    test_session_id, pg_available, chroma_store, temp_log_dir, monkeypatch
):
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    _seed_turn(pg, test_session_id, "历史问题一", "历史回答一")

    llm = _RecordingReplyLLM()
    graph = _setup_seeded_graph(pg, chroma_store, llm)

    reply = graph.chat(test_session_id, "新问题")
    assert reply.text

    llm_messages = llm.reply_messages
    assert llm_messages, "AssistantReply 应该收到 llm_messages"

    # Each history message must appear exactly once in the assembled prompt.
    joined = "\n".join(m["content"] for m in llm_messages)
    assert joined.count("历史问题一") == 1
    assert joined.count("历史回答一") == 1

    # 当前输入永远是最后一条消息。
    assert llm_messages[-1] == {"role": "user", "content": "用户说：新问题"}

@pytest.mark.parametrize("selected_empty", [False, True])
def test_log_dialogue_records_only_selected_dimensions(
    fake_llm, test_session_id, pg_available, chroma_store, temp_log_dir, selected_empty
):
    """selected_dimension_ids 只登记选中维度；空选择（改进 70 三态）时
    dimension_refs 如实写空。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    dimensions = pg.list_dimensions(active_only=True)
    assert len(dimensions) >= 2
    all_ids = [d.id for d in dimensions if d.id is not None]
    selected: list = [] if selected_empty else all_ids[:1]
    unselected = set(all_ids[1:])

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )

    state = {
        "session_id": test_session_id,
        "user_input": "测试输入",
        "reply": "测试回复",
        "reply_blocks": [],
        "dimensions": dimensions,
        "selected_dimension_ids": selected,
        "personality_docs": [],
        "web_docs": [],
        "inner_monologue": None,
    }
    result = graph._log_dialogue(state)
    assistant_log_id = result["assistant_log_id"]

    with pg._cursor() as cur:
        cur.execute(
            "SELECT dimension_id FROM dialogue_dimension_refs WHERE dialogue_log_id = %s",
            (assistant_log_id,),
        )
        ref_ids = {row["dimension_id"] for row in cur.fetchall()}

    assert ref_ids == set(selected)
    assert not (ref_ids & unselected)

@pytest.mark.parametrize("selected_empty", [False, True])
def test_assemble_uses_only_selected_dimensions(
    fake_llm, test_session_id, pg_available, chroma_store, temp_log_dir, monkeypatch,
    selected_empty,
):
    """selected_dimension_ids 只拼选中维度进 identity 段；空选择（改进 70 三态）时
    prompt 不含任何维度行，以占位文案代替。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)

    dimensions = pg.list_dimensions(active_only=True)
    assert len(dimensions) >= 2
    selected_dim = dimensions[0]
    unselected_dims = [d for d in dimensions[1:] if d.id is not None]
    selected_ids: list = [] if selected_empty else [selected_dim.id]

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=fake_llm,
        enable_web_search=False,
    )
    captured = _capture_fit_sections(graph, monkeypatch)

    state = {
        "session_id": test_session_id,
        "user_input": "测试输入",
        "identity": pg.get_identity(),
        "user_identity": None,
        "dimensions": dimensions,
        "selected_dimension_ids": selected_ids,
        "personality_docs": [],
        "memory_docs": [],
        "messages": [],
        "web_docs": [],
        "inner_monologue": None,
    }
    graph._assemble_and_generate(state)

    sections = captured.get("sections")
    assert sections, "fit_sections should have been called"
    identity_section = next(s for s in sections if s.name == "identity")

    if selected_empty:
        assert "（未特别选中）" in identity_section.text
        for dim in dimensions:
            assert dim.description not in identity_section.text
    else:
        assert selected_dim.description in identity_section.text
        for dim in unselected_dims:
            assert dim.description not in identity_section.text

class _HallucinatingSelectionLLM(FakeLLMClient):
    """DimensionSelection 返回混有幻觉 id 的选择结果。"""

    def structured_chat(self, messages, response_format, temperature=0.2):
        if response_format.__name__ == "DimensionSelection":
            return {"dimension_ids": [1, 999]}
        return super().structured_chat(messages, response_format, temperature)

def test_select_dimensions_drops_hallucinated_ids(
    test_session_id, pg_available, chroma_store, temp_log_dir
):
    """改进 55：_select_dimensions 白名单过滤幻觉 id 的行为锁定。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)
    dimensions = pg.list_dimensions(active_only=True)

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=_HallucinatingSelectionLLM(),
        enable_web_search=False,
    )
    state = {
        "session_id": test_session_id,
        "user_input": "测试输入",
        "dimensions": dimensions,
    }
    result = graph._select_dimensions(state)

    assert result["selected_dimension_ids"] == [1]

class _EmptySelectionLLM(FakeLLMClient):
    """DimensionSelection 明确返回空列表（本轮无维度起作用）。"""

    def structured_chat(self, messages, response_format, temperature=0.2):
        if response_format.__name__ == "DimensionSelection":
            return {"dimension_ids": []}
        return super().structured_chat(messages, response_format, temperature)

class _FailingSelectionLLM(FakeLLMClient):
    """DimensionSelection 调用抛异常（选择失败）。"""

    def structured_chat(self, messages, response_format, temperature=0.2):
        if response_format.__name__ == "DimensionSelection":
            raise RuntimeError("dimension selection unavailable")
        return super().structured_chat(messages, response_format, temperature)

@pytest.mark.parametrize("llm_cls", [_EmptySelectionLLM, _FailingSelectionLLM])
def test_select_dimensions_empty_or_failure_yields_empty(
    test_session_id, pg_available, chroma_store, temp_log_dir, llm_cls
):
    """改进 70：LLM 返回空列表或抛异常时 selected_dimension_ids 为 []
    （失败=无维度起作用），不回退全量维度。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    pg.create_session(test_session_id)
    dimensions = pg.list_dimensions(active_only=True)
    assert len(dimensions) >= 2

    graph = DialogueGraph(
        pg_store=pg,
        chroma_store=chroma_store,
        llm=llm_cls(),
        enable_web_search=False,
    )
    state = {
        "session_id": test_session_id,
        "user_input": "测试输入",
        "dimensions": dimensions,
    }
    result = graph._select_dimensions(state)

    assert result["selected_dimension_ids"] == []

def test_history_turns_become_alternating_messages(
    test_session_id, pg_available, chroma_store, temp_log_dir
):
    """改进 74：多轮历史还原为真实 user/assistant 消息序列，按时间序交替，
    当前输入永远是最后一条消息。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    llm = _RecordingReplyLLM()
    graph = _setup_seeded_graph(pg, chroma_store, llm)
    pg.create_session(test_session_id)

    _seed_turn(pg, test_session_id, "问题一", "回答一")
    _seed_turn(pg, test_session_id, "问题二", "回答二")

    reply = graph.chat(test_session_id, "当前问题")
    assert reply.text

    llm_messages = llm.reply_messages
    assert llm_messages

    roles = [m["role"] for m in llm_messages]
    # base = system 段 + assistant 素材段；随后是交替的历史轮；最后是当前输入。
    assert roles[:2] == ["system", "assistant"]
    assert roles[2:] == ["user", "assistant", "user", "assistant", "user"]

    # 时间序：较早的轮在前，较新的轮在后。
    turn_contents = [m["content"] for m in llm_messages[2:-1]]
    assert turn_contents == [
        "user: 问题一",
        "assistant: 回答一",
        "user: 问题二",
        "assistant: 回答二",
    ]

    # 人格/记忆/网络等素材仍集中在 base 的 assistant 消息内。
    assistant_base = llm_messages[1]["content"]
    assert "<personality>" in assistant_base
    assert "<memory>" in assistant_base
    assert "<web>" in assistant_base

    # 当前输入是最后一条消息。
    assert llm_messages[-1] == {"role": "user", "content": "用户说：当前问题"}

def test_history_trimmed_to_token_budget_keeps_recent_turns(
    test_session_id, pg_available, chroma_store, temp_log_dir, monkeypatch
):
    """改进 74：历史超出 token 预算时最老整轮被裁、最近轮保留，并记录
    chat.history_trimmed 日志。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    llm = _RecordingReplyLLM()
    graph = _setup_seeded_graph(pg, chroma_store, llm)
    pg.create_session(test_session_id)

    # 6 对历史轮，每轮内容约 100 token（中文字符 ≈ 1 token）；limit 下调后
    # 触发同一裁剪分支（最老整轮被裁），数据量约为原 6×1600 字的 1/10。
    for i in range(6):
        _seed_turn(
            pg,
            test_session_id,
            question=f"第{i}问" + "问" * 100,
            answer=f"第{i}答" + "答" * 100,
        )

    monkeypatch.setattr(settings, "llm_context_token_limit", 2500)

    reply = graph.chat(test_session_id, "当前问题")
    assert reply.text

    llm_messages = llm.reply_messages
    assert llm_messages

    joined = "\n".join(m["content"] for m in llm_messages)
    # 最老的轮被裁掉，最近的轮保留。
    assert "第0问" not in joined
    assert "第5问" in joined
    assert "第5答" in joined
    # 当前输入永远在最后。
    assert llm_messages[-1] == {"role": "user", "content": "用户说：当前问题"}

    # 裁剪事件被记录，kept + dropped 等于历史总轮数（12 条消息）。
    log_text = (Path(temp_log_dir) / "mr-data.log").read_text(encoding="utf-8")
    events = [
        json.loads(line) for line in log_text.splitlines() if line.strip()
    ]
    trimmed = [e for e in events if e.get("event") == "chat.history_trimmed"]
    assert len(trimmed) == 1
    assert trimmed[0]["session_id"] == test_session_id
    details = trimmed[0]["details"]
    assert details["kept"] + details["dropped"] == 12
    assert details["dropped"] > 0

def test_history_assistant_turn_keeps_inner_monologue_and_references(
    test_session_id, pg_available, chroma_store, temp_log_dir
):
    """改进 74：assistant 历史轮的内心独白与参考来源标注仍出现在对应的
    assistant 消息 content 中。"""
    if not pg_available:
        pytest.skip("PostgreSQL not available")

    pg = PostgresStore()
    llm = _RecordingReplyLLM()
    graph = _setup_seeded_graph(pg, chroma_store, llm)
    pg.create_session(test_session_id)

    _seed_turn(
        pg,
        test_session_id,
        "太阳系有几颗行星",
        "太阳系有八大行星。",
        inner_monologue="我在回忆太阳系资料",
        blocks=[
            ReplyBlock(
                text="太阳系有八大行星。",
                references=[
                    ReplyReference(
                        id="web:0", source_type="web", summary="太阳系资料"
                    )
                ],
            )
        ],
    )

    reply = graph.chat(test_session_id, "那冥王星呢")
    assert reply.text

    llm_messages = llm.reply_messages
    assistant_turns = [
        m for m in llm_messages
        if m["role"] == "assistant" and m["content"].startswith("assistant: ")
    ]
    assert len(assistant_turns) == 1
    content = assistant_turns[0]["content"]
    assert content.startswith("assistant: 太阳系有八大行星。")
    assert "\n  内心独白：我在回忆太阳系资料" in content
    assert "\n  参考来源：太阳系资料" in content
