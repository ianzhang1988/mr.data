import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, TypedDict

from langgraph.graph import StateGraph, END

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore
from mr_data.db.chunking import split_text_chunks
from mr_data.db.chroma import web_chunk_base_id
from mr_data.llm import LLMClient
from mr_data.llm.tokenizer import TokenCounter
from mr_data.logging import get_logger
from mr_data.models import (
    AssistantReply,
    DialogueLog,
    DialogueLogMetadata,
    DialogueMessage,
    DialogueVectorRef,
    FixedIdentity,
    UserIdentity,
    PersonalityDimension,
    ReplyBlock,
    ReplyReference,
    ThinkDecision,
    DimensionSelection,
    MemoryDoc,
    MemoryRelevanceFilterResult,
    PersonalityDoc,
    WebDoc,
    WebMemoryMetadata,
)
from mr_data.online.page_extract import PageExtractor
from mr_data.online.search_providers import _stable_web_id
from mr_data.online.web_filter import filter_web_docs
from mr_data.online.prompt_assembly import (
    PromptAssembler,
    PromptSection,
    build_messages,
    wrap_section,
)
from mr_data.online.web_search import WebSearchTool

# 当结构化回复与纯文本回退均失败时使用的静态兜底文案。
_STATIC_FALLBACK_REPLY = "我的处理回路暂时受到干扰，无法组织回复。"


def _web_memory_chunks(doc: WebDoc) -> list[tuple[str, str]]:
    """把 web 文档切成记忆分片，返回 [(chunk_id, chunk_text), ...]。

    base_id 由 URL+正文哈希派生（web_chunk_base_id）：同页重抓正文不变 →
    片界稳定 → 同 id 原地刷新；正文变化 → base_id 变化 → 全新 id 写入，
    不会产生孤儿分片。空/纯空白正文不产生任何分片。
    """
    chunks = split_text_chunks(
        doc.page_content,
        settings.memory_web_chunk_chars,
        settings.memory_web_chunk_overlap_chars,
    )
    if not chunks:
        return []
    base_id = web_chunk_base_id(doc.metadata.url, doc.page_content)
    return [(f"{base_id}:c{i}", chunk) for i, chunk in enumerate(chunks)]


def _render_memory_doc(d: MemoryDoc) -> str:
    """按 source_type 渲染 memory 段的一条素材。

    web 记忆渲染为「来源行 + 缩进两空格的片正文」两行；dialogue 记忆
    单行平铺。供 _assemble_and_generate 的 memory 段使用。
    """
    if isinstance(d.metadata, WebMemoryMetadata):
        title = d.metadata.title or "web"
        if d.metadata.url:
            header = f"- [id: {d.id}] [web] 《{title}》 {d.metadata.url}"
        else:
            header = f"- [id: {d.id}] [web] {title}"
        return f"{header}\n  {d.page_content}"
    return f"- [id: {d.id}] [dialogue] {d.page_content}"


@dataclass
class ChatResult:
    """在线对话的单轮返回结果。"""

    text: str
    blocks: list[ReplyBlock] = field(default_factory=list)

    @property
    def references(self) -> list[ReplyReference]:
        """返回所有块中引用的去重并集（兼容旧的平铺引用访问）。"""
        seen: set[str] = set()
        refs: list[ReplyReference] = []
        for block in self.blocks:
            for ref in block.references:
                if ref.id and ref.id not in seen:
                    refs.append(ref)
                    seen.add(ref.id)
        return refs

    def __str__(self) -> str:
        return self.text


class DialogueState(TypedDict, total=False):
    session_id: str
    user_input: str
    identity: Optional[FixedIdentity]
    user_identity: Optional[UserIdentity]
    dimensions: list[PersonalityDimension]
    selected_dimension_ids: list[int]
    retrieval_query: str
    inner_monologue: Optional[str]
    personality_query: str
    memory_query: str
    needs_web_search: bool
    search_query: Optional[str]
    web_docs: list[WebDoc]
    personality_docs: list[PersonalityDoc]
    memory_docs: list[MemoryDoc]
    messages: list[DialogueMessage]
    reply: str
    reply_blocks: list[ReplyBlock]
    assistant_log_id: Optional[int]


class DialogueGraph:
    def __init__(
        self,
        pg_store: Optional[PostgresStore] = None,
        chroma_store: Optional[ChromaStore] = None,
        llm: Optional[LLMClient] = None,
        web_search: Optional[WebSearchTool] = None,
        enable_web_search: Optional[bool] = None,
        logger: Optional = None,
    ):
        self.pg = pg_store or PostgresStore()
        self.chroma = chroma_store or ChromaStore()
        self.llm = llm or LLMClient()
        self.web_search = web_search or WebSearchTool()
        self.page_extractor = PageExtractor()
        self.enable_web_search = (
            enable_web_search if enable_web_search is not None else settings.enable_web_search
        )
        self.logger = logger or get_logger("mr_data.online")
        self.prompt_assembler = PromptAssembler(self.llm)
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(DialogueState)

        builder.add_node("load_personality", self._load_personality)
        builder.add_node("select_dimensions", self._select_dimensions)
        builder.add_node("think", self._think)
        builder.add_node("retrieve_web", self._retrieve_web)
        builder.add_node("extract_web_pages", self._extract_web_pages)
        builder.add_node("filter_web_docs", self._filter_web_docs)
        builder.add_node("retrieve_personality", self._retrieve_personality)
        builder.add_node("retrieve_memories", self._retrieve_memories)
        builder.add_node("filter_memory_docs", self._filter_memory_docs)
        builder.add_node("assemble_and_generate", self._assemble_and_generate)
        builder.add_node("log_dialogue", self._log_dialogue)

        builder.set_entry_point("load_personality")
        builder.add_edge("load_personality", "select_dimensions")
        builder.add_edge("select_dimensions", "think")
        builder.add_conditional_edges(
            "think",
            self._route_web,
            {"web": "retrieve_web", "personality": "retrieve_personality"},
        )
        builder.add_edge("retrieve_web", "extract_web_pages")
        builder.add_edge("extract_web_pages", "filter_web_docs")
        builder.add_edge("filter_web_docs", "retrieve_personality")
        builder.add_edge("retrieve_personality", "retrieve_memories")
        builder.add_edge("retrieve_memories", "filter_memory_docs")
        builder.add_edge("filter_memory_docs", "assemble_and_generate")
        builder.add_edge("assemble_and_generate", "log_dialogue")
        builder.add_edge("log_dialogue", END)

        return builder.compile()

    def _load_personality(self, state: DialogueState) -> DialogueState:
        identity = self.pg.get_identity()
        user_identity = self.pg.get_current_user_identity()
        dimensions = self.pg.list_dimensions(active_only=True)
        self.logger.info(
            "Loaded personality",
            extra={
                "event": "personality.loaded",
                "session_id": state["session_id"],
                "details": {
                    "identity": identity.name if identity else None,
                    "user_identity": user_identity.name if user_identity else None,
                    "dimension_count": len(dimensions),
                },
            },
        )
        return {
            **state,
            "identity": identity,
            "user_identity": user_identity,
            "dimensions": dimensions,
        }

    def _select_dimensions(self, state: DialogueState) -> DialogueState:
        dimensions = state.get("dimensions", [])
        # 三态语义：[] 表示「本轮无维度起作用」（LLM 明确不选或选择失败），
        # 不再预置全量兜底，避免与失败兜底混同而污染下游归因。
        selected_ids: list[int] = []

        if dimensions:
            dim_text = "\n".join(
                f"- [{dim.id}] {dim.description}"
                for dim in dimensions
                if dim.id is not None
            )
            system = "你是一个性格维度选择助手。请根据用户输入，从下列维度中选出最应当起作用的一个或多个维度。"
            prompt = f"用户输入：{state['user_input']}\n\n可选维度：\n{
                dim_text}\n\n请返回维度 ID 列表。"
            try:
                result = self.llm.chat_structured(
                    system, prompt, response_format=DimensionSelection, temperature=0.3
                )
                selection = DimensionSelection.model_validate(result)
                valid_ids = {
                    dim.id for dim in dimensions if dim.id is not None}
                selected_ids = [
                    x for x in selection.dimension_ids if x in valid_ids]
                dropped = [x for x in selection.dimension_ids if x not in valid_ids]
                if dropped:
                    self.logger.warning(
                        "LLM returned invalid dimension ids, dropped",
                        extra={
                            "event": "personality.dimension_select_invalid_ids",
                            "session_id": state["session_id"],
                            "details": {"dropped_ids": dropped},
                        },
                    )
            except Exception as exc:
                self.logger.warning(
                    "Dimension selection failed; using no dimensions",
                    extra={
                        "event": "personality.dimension_select_failed",
                        "session_id": state["session_id"],
                        "details": {"error": str(exc)},
                    },
                )

        self.logger.info(
            "Selected dimensions",
            extra={
                "event": "personality.dimensions_selected",
                "session_id": state["session_id"],
                "details": {
                    "selected_ids": selected_ids,
                    "available_count": len(dimensions),
                },
            },
        )
        return {**state, "selected_dimension_ids": selected_ids}

    def _route_web(self, state: DialogueState) -> str:
        if not self.enable_web_search:
            return "personality"
        return "web" if state.get("needs_web_search") else "personality"

    def _think(self, state: DialogueState) -> DialogueState:
        dimensions = state.get("dimensions", [])
        selected_ids = set(state.get("selected_dimension_ids", []))
        selected_dimensions = [
            dim for dim in dimensions if dim.id in selected_ids]
        selected_text = "\n".join(
            f"- {dim.description}"
            for dim in selected_dimensions
        ) or "（未特别选中）"

        system = f"""你是一个查询改写与内心独白生成助手。请根据用户输入和本次应重点参考的性格维度，生成以下结构化字段：
1. inner_monologue：用一句话描述你当前对用户意图的理解以及你打算如何回应（内心独白）。
2. personality_query：用于检索性格向量库的简短语义查询（性格向量库中有你之前的思路、决定、内心独白、对话等），推荐用例如概念、理论、高层次抽象的用语。
3. memory_query：用于检索记忆向量库的简短语义查询（记忆向量库中有过往对话、其他方式获取的信息，如文档、网页等）。
4. needs_web_search：是否需要联网搜索来获取最新信息或事实验证。
5. search_query：如需要搜索，提炼出的精准搜索关键词；否则留空。

本次应重点参考的性格维度：
{selected_text}
"""
        prompt = f"用户输入：{state['user_input']}\n请输出结构化决策："

        try:
            raw = self.llm.chat_structured(
                system, prompt, response_format=ThinkDecision, temperature=0.3
            )
            decision = ThinkDecision.model_validate(raw)
        except Exception as exc:
            self.logger.warning(
                "Think decision failed; using fallback decision",
                extra={
                    "event": "think.decision_failed",
                    "session_id": state["session_id"],
                    "details": {"error": str(exc)},
                },
            )
            decision = ThinkDecision(
                inner_monologue="",
                personality_query=state["user_input"],
                memory_query=state["user_input"],
                needs_web_search=False,
                search_query=state["user_input"],
            )

        if not self.enable_web_search:
            decision.needs_web_search = False

        self.logger.info(
            "Generated think decision",
            extra={
                "event": "think.decision_generated",
                "session_id": state["session_id"],
                "details": {
                    "personality_query": decision.personality_query,
                    "memory_query": decision.memory_query,
                    "needs_web_search": decision.needs_web_search,
                    "search_query": decision.search_query,
                    "inner_monologue": decision.inner_monologue,
                },
            },
        )
        return {
            **state,
            "retrieval_query": decision.memory_query,
            "inner_monologue": decision.inner_monologue,
            "personality_query": decision.personality_query,
            "memory_query": decision.memory_query,
            "needs_web_search": decision.needs_web_search,
            "search_query": decision.search_query,
        }

    def _retrieve_web(self, state: DialogueState) -> DialogueState:
        if not self.enable_web_search:
            return {**state, "web_docs": []}
        query = state.get("search_query") or state.get(
            "retrieval_query") or state["user_input"]
        docs = self.web_search.search(query)
        self.logger.info(
            "Retrieved web results",
            extra={
                "event": "retrieve.web",
                "session_id": state["session_id"],
                "details": {"query": state["retrieval_query"], "result_count": len(docs)},
            },
        )
        return {**state, "web_docs": docs}

    def _extract_web_pages(self, state: DialogueState) -> DialogueState:
        docs = state.get("web_docs", [])
        if not docs:
            return {**state, "web_docs": []}

        max_pages = settings.web_extract_max_pages
        if max_pages <= 0:
            return {**state, "web_docs": docs}
        extracted = []
        for doc in docs[:max_pages]:
            url = doc.metadata.url
            if not url:
                extracted.append(doc)
                continue
            result = self.page_extractor.extract(url)
            if result:
                real_url = result.url
                new_doc = doc.model_copy(update={
                    # real_url 为空或仍是跳转链接时以正文+标题哈希兜底，
                    # 保证同页同 id（upsert 去重），避免退化为随机 id 重复入库
                    "id": _stable_web_id(real_url, body=result.text, title=doc.metadata.title),
                    "page_content": f"{doc.metadata.title}\n{result.text}",
                    "metadata": doc.metadata.model_copy(update={
                        "url": real_url,
                        "source_url": url,
                        "extracted": True,
                    }),
                })
                extracted.append(new_doc)
            else:
                extracted.append(doc)

        # max_pages 之后的搜索结果不抓正文，但以 snippet 级内容保留，
        # 继续参与后续相关性过滤与 prompt 组装。
        extracted.extend(docs[max_pages:])

        self.logger.info(
            "Extracted web pages",
            extra={
                "event": "retrieve.web_extracted",
                "session_id": state["session_id"],
                "details": {"attempted": len(docs[:max_pages]), "succeeded": sum(1 for d in extracted if d.metadata.extracted), "passthrough": len(docs) - len(docs[:max_pages])},
            },
        )
        return {**state, "web_docs": extracted}

    def _filter_web_docs(self, state: DialogueState) -> DialogueState:
        docs = state.get("web_docs", [])
        if not docs:
            return {**state, "web_docs": []}
        if not settings.enable_web_doc_extraction:
            return {**state, "web_docs": docs}

        filtered = filter_web_docs(self.llm, docs, state["user_input"])

        self.logger.info(
            "Filtered web docs",
            extra={
                "event": "retrieve.web_filtered",
                "session_id": state["session_id"],
                "details": {"input_count": len(docs), "output_count": len(filtered)},
            },
        )
        return {**state, "web_docs": filtered}

    def _retrieve_personality(self, state: DialogueState) -> DialogueState:
        query = state.get("personality_query") or state["user_input"]
        try:
            docs = self.chroma.query_personality(query, top_k=settings.personality_retrieval_top_k)
        except Exception as exc:
            self.logger.warning(
                "Personality retrieval failed; returning empty docs",
                extra={
                    "event": "retrieve.personality_failed",
                    "session_id": state["session_id"],
                    "details": {"error": str(exc)},
                },
            )
            docs = []
        self.logger.info(
            "Retrieved personality docs",
            extra={
                "event": "retrieve.personality",
                "session_id": state["session_id"],
                "details": {"query": state["retrieval_query"], "result_count": len(docs)},
            },
        )
        return {**state, "personality_docs": docs}

    def _retrieve_memories(self, state: DialogueState) -> DialogueState:
        query = state.get("memory_query") or state["user_input"]
        try:
            docs = self.chroma.query_memories(
                query, top_k=settings.memory_retrieval_top_k)
        except Exception as exc:
            self.logger.warning(
                "Memory retrieval failed; returning empty docs",
                extra={
                    "event": "retrieve.memory_failed",
                    "session_id": state["session_id"],
                    "details": {"error": str(exc)},
                },
            )
            docs = []

        recall_doc_ids = [doc.id for doc in docs]
        if recall_doc_ids:
            now = datetime.now(timezone.utc).isoformat()
            try:
                # dialogue 与 web 记忆统一计数：recall_count+1 并刷新
                # last_recalled_at（prune 超时基准）；web 的这两个字段是
                # WebMemoryMetadata extra 字段，用 getattr 容错缺失场景。
                self.chroma.increment_memory_recall(recall_doc_ids)
                # Reflect the updated recall_count in the state docs passed downstream.
                for doc in docs:
                    if doc.id in recall_doc_ids:
                        doc.metadata.recall_count = getattr(doc.metadata, "recall_count", 0) + 1
                        doc.metadata.last_recalled_at = now
            except Exception as exc:
                # 召回计数是辅助信号，失败不影响已检索到的记忆参与本轮对话。
                self.logger.warning(
                    "Memory recall increment failed; keeping retrieved docs",
                    extra={
                        "event": "retrieve.memory_recall_increment_failed",
                        "session_id": state["session_id"],
                        "details": {"error": str(exc), "doc_ids": recall_doc_ids},
                    },
                )
        self.logger.info(
            "Retrieved memories",
            extra={
                "event": "retrieve.memory",
                "session_id": state["session_id"],
                "details": {"query": state["retrieval_query"], "result_count": len(docs)},
            },
        )
        return {**state, "memory_docs": docs}

    def _filter_memory_docs(self, state: DialogueState) -> DialogueState:
        docs = state.get("memory_docs", [])
        if not docs or not settings.enable_memory_relevance_filter:
            return {**state, "memory_docs": docs}

        user_input = state["user_input"]
        indexed_docs = [
            (i, doc.page_content[:1500]) for i, doc in enumerate(docs)
        ]
        items_text = "\n\n".join(
            f"[{idx}] {content}" for idx, content in indexed_docs
        )
        system = "判断下列记忆内容是否与用户输入相关。请为每条记忆返回是否相关。"
        prompt = f"用户输入：{user_input}\n\n记忆列表：\n{items_text}\n\n请返回每条记忆的相关性判断。"

        try:
            result = self.llm.chat_structured(
                system, prompt, response_format=MemoryRelevanceFilterResult, temperature=0.0
            )
            decision = MemoryRelevanceFilterResult.model_validate(result)
            relevant_indices = {
                item.index for item in decision.results if item.is_relevant}
            filtered = [doc for i, doc in enumerate(
                docs) if i in relevant_indices]
        except Exception as exc:
            self.logger.warning(
                "Memory relevance filtering failed; keeping all memory docs",
                extra={
                    "event": "retrieve.memory_filter_failed",
                    "session_id": state["session_id"],
                    "details": {"error": str(exc), "doc_count": len(docs)},
                },
            )
            filtered = docs

        if not filtered and docs:
            filtered = docs

        self.logger.info(
            "Filtered memory docs",
            extra={
                "event": "retrieve.memory_filtered",
                "session_id": state["session_id"],
                "details": {"input_count": len(docs), "output_count": len(filtered)},
            },
        )
        return {**state, "memory_docs": filtered}

    def _assemble_and_generate(self, state: DialogueState) -> DialogueState:
        identity = state.get("identity")
        dimensions = state.get("dimensions", [])
        web_docs = state.get("web_docs", [])
        personality_docs = state.get("personality_docs", [])
        memory_docs = state.get("memory_docs", [])
        messages = state.get("messages", [])
        inner_monologue = state.get("inner_monologue")
        user_identity = state.get("user_identity")

        raw_selected_ids = state.get("selected_dimension_ids")
        selected_ids = set(
            raw_selected_ids if raw_selected_ids is not None
            else [d.id for d in dimensions if d.id is not None]
        )
        dim_text = "\n".join(
            f"- {dim.description}" for dim in dimensions if dim.id in selected_ids
        ) or "（未特别选中）"
        user_identity_text = ""
        if user_identity:
            user_identity_text = (
                f"与你对话的用户身份：{user_identity.name}（{user_identity.role}）。"
                f"{user_identity.description}"
            )

        identity_text = "\n".join(
            part for part in [
                f"你是 {identity.name if identity else 'mr.data'}，{
                    identity.role if identity else '一个对话程序'}。",
                identity.base_prompt if identity else "",
                user_identity_text,
                "你的基础性格自白：",
                dim_text,
                f"你当前的内心独白：{inner_monologue}" if inner_monologue else "",
            ] if part
        )

        user_input_text = f"用户说：{state['user_input']}"

        personality_text = "\n".join(
            f"- [id: {d.id}] [{d.metadata.source_type}] {d.page_content}"
            for d in personality_docs
        )
        memory_text = "\n".join(
            _render_memory_doc(d) for d in memory_docs
        )
        web_text = "\n".join(
            f"- [id: {d.id}] [{d.metadata.title or 'web'}] {d.page_content}"
            for d in web_docs
        )

        format_instruction = (
            "请根据以上内容生成回复，保持性格一致性。\n\n"
            "把回复划分为若干内容块（段落、句子或任意独立观点均可），每个块必须包含：\n"
            "- text：该块的文本内容。\n"
            "- references：该块引用的素材列表；如果该块没有对应素材（纯人格表达），则传空数组 []。\n\n"
            "输出必须是 JSON，格式如下：\n"
            '{\n'
            '  "text": "给用户的完整回复文本",\n'
            '  "blocks": [\n'
            '    {\n'
            '      "text": "内容块文本",\n'
            '      "references": [\n'
            '        {"id": "素材id", "source_type": "素材列表中标注的来源类型（line/evidence/event/web/dialogue）", "summary": "该素材的一句话总结"}\n'
            '      ]\n'
            '    }\n'
            '  ]\n'
            '}\n'
            "引用 id 必须来自上面列出的素材；未出现的素材不要引用。"
        )

        known_refs: dict[str, str] = {}
        for d in web_docs:
            known_refs[d.id] = "web"
        for d in personality_docs:
            known_refs[d.id] = d.metadata.source_type
        for d in memory_docs:
            known_refs[d.id] = d.metadata.source_type

        guidance_text = (
            "你是助手，后续会出现多条 assistant 消息，其中包含给你参考的素材。"
            "请按以下规则使用这些素材生成回复：\n"
            "1. 人格素材：作为你的性格设定和语言风格参考，组织回复时要符合这些设定。\n"
            "2. 记忆素材：作为你和用户的过往对话或已知事实，用于保持上下文连贯。\n"
            "3. 近期对话：当前会话的上下文，用于理解用户当前问题的背景。\n"
            "4. 网络资料：来自网络的事实参考，仅作为回答依据，不要违背你的人格设定。"
        )

        sections = [
            PromptSection("system", guidance_text, 100, "system"),
            PromptSection("identity", identity_text, 90, "system"),
            PromptSection("personality", wrap_section(
                "personality", personality_text), 70, "assistant"),
            PromptSection("memory", wrap_section(
                "memory", memory_text), 60, "assistant"),
            PromptSection("web", wrap_section(
                "web", web_text), 40, "assistant"),
            PromptSection("format", format_instruction, 100, "system"),
        ]

        fitted = self.prompt_assembler.fit_sections(
            sections, settings.llm_context_token_limit)
        base = build_messages(fitted)

        # 改进 74：把最近 N 轮还原为真实 user/assistant 消息序列拼入 llm_messages，
        # 当前输入永远在最后；装不下的最老整轮丢弃。
        def _render_turn(m: DialogueMessage) -> str:
            text = f"{'user' if m.role == 'user' else 'assistant'}: {m.content}"
            if m.inner_monologue:
                text += f"\n  内心独白：{m.inner_monologue}"
            if any(block.references for block in m.blocks):
                text += (
                    f"\n  参考来源：{
                        ', '.join(r.summary for block in m.blocks for r in block.references)}"
                )
            return text

        counter = TokenCounter()
        used = sum(counter.count(m["content"]) for m in base)
        remaining = settings.llm_context_token_limit - used
        remaining -= counter.count(user_input_text)
        remaining = max(remaining, 0)

        turn_messages: list[dict] = []
        kept_turns = 0
        for m in reversed(messages):
            content = _render_turn(m)
            turn_tokens = counter.count(content)
            if turn_tokens > remaining:
                # 预算耗尽：该轮及所有更老的轮整体丢弃，保留最近连续窗口。
                break
            turn_messages.append(
                {"role": "user" if m.role == "user" else "assistant",
                 "content": content})
            remaining -= turn_tokens
            kept_turns += 1
        turn_messages.reverse()

        dropped_turns = len(messages) - kept_turns
        if dropped_turns > 0:
            self.logger.info(
                "History turns trimmed to fit token budget",
                extra={
                    "event": "chat.history_trimmed",
                    "session_id": state["session_id"],
                    "details": {"kept": kept_turns, "dropped": dropped_turns},
                },
            )

        llm_messages = base + turn_messages + [
            {"role": "user", "content": user_input_text}]

        reply = ""
        blocks: list[ReplyBlock] = []
        try:
            raw = self.llm.structured_chat(
                llm_messages, AssistantReply, temperature=0.8)
            decision = AssistantReply.model_validate(raw)
            reply = decision.text
            blocks = [
                ReplyBlock(
                    text=block.text,
                    references=[
                        ReplyReference(
                            id=ref.id,
                            source_type=known_refs.get(
                                ref.id, ref.source_type),
                            summary=ref.summary,
                        )
                        for ref in block.references
                        if ref.id in known_refs
                    ],
                )
                for block in decision.blocks
            ]
        except Exception as exc:
            self.logger.warning(
                "Structured reply generation failed; falling back to plain text",
                extra={
                    "event": "chat.reply_fallback",
                    "session_id": state["session_id"],
                    "details": {"error": str(exc)},
                },
            )
            try:
                fallback_text = self.llm.chat_with_messages(
                    llm_messages, temperature=0.8)
                reply = fallback_text
                blocks = [ReplyBlock(text=fallback_text, references=[])]
            except Exception as fallback_exc:
                self.logger.warning(
                    "Plain text reply fallback also failed; using static fallback",
                    extra={
                        "event": "chat.reply_fallback_failed",
                        "session_id": state["session_id"],
                        "details": {"error": str(fallback_exc)},
                    },
                )
                reply = _STATIC_FALLBACK_REPLY
                blocks = [ReplyBlock(text=_STATIC_FALLBACK_REPLY, references=[])]

        all_refs = []
        seen_ids: set[str] = set()
        for block in blocks:
            for ref in block.references:
                if ref.id and ref.id not in seen_ids:
                    all_refs.append(ref)
                    seen_ids.add(ref.id)

        self.logger.info(
            "Generated reply",
            extra={
                "event": "chat.reply",
                "session_id": state["session_id"],
                "details": {"reply_length": len(reply), "block_count": len(blocks), "reference_count": len(all_refs)},
            },
        )
        return {**state, "reply": reply, "reply_blocks": blocks}

    def _log_dialogue(self, state: DialogueState) -> DialogueState:
        session_id = state["session_id"]
        user_input = state["user_input"]
        reply = state["reply"]
        dimensions = state.get("dimensions", [])
        personality_docs = state.get("personality_docs", [])
        web_docs = state.get("web_docs", [])
        inner_monologue = state.get("inner_monologue")

        # 4 步 PG 写入在同一事务内提交，中途失败整体回滚，避免留下脏数据。
        with self.pg.transaction():
            self.pg.insert_dialogue(
                DialogueLog(session_id=session_id,
                            role="user", content=user_input)
            )
            assistant_metadata = DialogueLogMetadata(
                inner_monologue=inner_monologue,
                blocks=state.get("reply_blocks", []),
            )
            assistant_log_id = self.pg.insert_dialogue(
                DialogueLog(
                    session_id=session_id,
                    role="assistant",
                    content=reply,
                    metadata=assistant_metadata,
                )
            )

            # 记录本次实际选中的维度；仅当 key 缺失（None）时才防御性回退全量，
            # 空列表表示「本轮无维度起作用」，如实写空。
            raw_selected_ids = state.get("selected_dimension_ids")
            selected_ids = set(
                raw_selected_ids if raw_selected_ids is not None
                else [d.id for d in dimensions if d.id is not None]
            )
            self.pg.insert_dialogue_dimension_refs(
                assistant_log_id, [dim.id for dim in dimensions if dim.id in selected_ids])

            # 记录检索到的向量素材（人格素材 + 网络资料）
            vector_refs = [
                DialogueVectorRef(
                    dialogue_log_id=assistant_log_id,
                    vector_doc_id=doc.id,
                    source_type=doc.metadata.source_type,
                    content=doc.page_content,
                    dimension_ids=doc.metadata.dimension_ids,
                )
                for doc in personality_docs
            ]
            for doc in web_docs:
                vector_refs += [
                    DialogueVectorRef(
                        dialogue_log_id=assistant_log_id,
                        vector_doc_id=chunk_id,
                        source_type="web",
                        content=chunk,
                        dimension_ids=[],
                    )
                    for chunk_id, chunk in _web_memory_chunks(doc)
                ]
            self.pg.insert_dialogue_vector_refs(assistant_log_id, vector_refs)

        # 把网络资料作为世界知识分片写入记忆向量库（PG 事务外，逐片失败隔离）。
        # add_memory 幂等：同 id 已存在则跳过（保留 recall_count/added_at 等），
        # 同页重抓正文不变时同 id 原地刷新；正文变化则 base_id 变化、新 id 全新
        # 写入，不会产生孤儿分片。
        retrieval_query = state.get("retrieval_query", user_input)
        retrieved_at = datetime.now(timezone.utc).isoformat()
        for doc in web_docs:
            title = doc.metadata.title
            url = doc.metadata.url
            for i, (chunk_id, chunk) in enumerate(_web_memory_chunks(doc)):
                try:
                    self.chroma.add_memory(
                        session_id="",
                        content=chunk,
                        memory_id=chunk_id,
                        metadata=WebMemoryMetadata(
                            url=url,
                            title=title,
                            retrieved_at=retrieved_at,
                            retrieval_session_id=session_id,
                            query=retrieval_query,
                            chunk_index=i,
                        ),
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Web memory write failed, skipped",
                        extra={
                            "event": "chat.web_memory_write_failed",
                            "session_id": session_id,
                            "details": {"chunk_id": chunk_id, "url": url, "error": str(exc)},
                        },
                    )

        self.logger.info(
            "Logged dialogue turn",
            extra={
                "event": "chat.turn",
                "session_id": session_id,
                "details": {
                    "assistant_log_id": assistant_log_id,
                    "inner_monologue": inner_monologue,
                    "personality_doc_count": len(personality_docs),
                    "web_doc_count": len(web_docs),
                    "block_count": len(state.get("reply_blocks", [])),
                    "reference_count": sum(
                        len(block.references) for block in state.get("reply_blocks", [])
                    ),
                },
            },
        )

        return {**state, "assistant_log_id": assistant_log_id}

    def _load_recent_messages(self, session_id: str) -> list[DialogueMessage]:
        """Load the most recent dialogue turns from Postgres into DialogueState messages."""
        limit = settings.dialogue_state_message_turns * 2
        logs = self.pg.get_recent_dialogues(session_id=session_id, limit=limit)
        messages: list[DialogueMessage] = []
        for log in sorted(logs, key=lambda x: (x.created_at is None, x.created_at or datetime.min, x.id or 0)):
            meta = log.metadata
            if log.role == "user":
                messages.append(DialogueMessage(
                    role="user", content=log.content))
            else:
                messages.append(
                    DialogueMessage(
                        role="assistant",
                        content=log.content,
                        inner_monologue=meta.inner_monologue if meta else None,
                        blocks=meta.blocks if meta else [],
                    )
                )
        return messages

    def chat(self, session_id: str, user_input: str) -> ChatResult:
        self.logger.info(
            "Chat turn started",
            extra={"event": "chat.start", "session_id": session_id,
                   "details": {"user_input": user_input}},
        )
        messages = self._load_recent_messages(session_id)
        state: DialogueState = {
            "session_id": session_id,
            "user_input": user_input,
            "messages": messages,
        }
        try:
            final_state = self.graph.invoke(state)
            return ChatResult(
                text=final_state["reply"],
                blocks=final_state.get("reply_blocks", []),
            )
        except Exception as exc:
            self.logger.exception(
                "Chat turn failed",
                extra={"event": "chat.error", "session_id": session_id,
                       "details": {"error": str(exc)}},
            )
            raise
