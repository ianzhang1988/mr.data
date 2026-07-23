import json
from datetime import datetime, timezone
from typing import Annotated, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore
from mr_data.llm import LLMClient
from mr_data.logging import get_logger
from mr_data.models import (
    DialogueLog,
    DialogueVectorRef,
    FixedIdentity,
    UserIdentity,
    PersonalityDimension,
    ThinkDecision,
    DimensionSelection,
    WebRelevanceFilterResult,
    MemoryRelevanceFilterResult,
)
from mr_data.online.page_extract import PageExtractor
from mr_data.online.web_search import WebSearchTool


def _merge_docs(old: list[dict], new: list[dict]) -> list[dict]:
    seen = set()
    merged = []
    for doc in old + new:
        doc_id = doc.get("id")
        if doc_id is None or doc_id not in seen:
            merged.append(doc)
            if doc_id is not None:
                seen.add(doc_id)
    return merged


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
    web_docs: Annotated[list[dict], _merge_docs]
    personality_docs: Annotated[list[dict], _merge_docs]
    memory_docs: Annotated[list[dict], _merge_docs]
    reply: str
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
        fallback_ids = [dim.id for dim in dimensions if dim.id is not None]
        selected_ids = fallback_ids[:]

        if dimensions:
            dim_text = "\n".join(
                f"- [{dim.id}] {dim.description}"
                for dim in dimensions
                if dim.id is not None
            )
            system = "你是一个性格维度选择助手。请根据用户输入，从下列维度中选出最应当起作用的一个或多个维度。"
            prompt = f"用户输入：{state['user_input']}\n\n可选维度：\n{dim_text}\n\n请返回维度 ID 列表。"
            try:
                result = self.llm.structured_chat(
                    system, prompt, response_format=DimensionSelection, temperature=0.3
                )
                selection = DimensionSelection.model_validate(result)
                valid_ids = {dim.id for dim in dimensions if dim.id is not None}
                selected_ids = [x for x in selection.dimension_ids if x in valid_ids]
            except Exception:
                pass

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
        except Exception:
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
            url = doc.get("metadata", {}).get("url")
            if not url:
                extracted.append(doc)
                continue
            text = self.page_extractor.extract(url)
            if text:
                new_doc = {
                    **doc,
                    "page_content": f"{doc.get('metadata', {}).get('title', '')}\n{text}",
                    "metadata": {
                        **doc.get("metadata", {}),
                        "extracted": True,
                    },
                }
                extracted.append(new_doc)
            else:
                extracted.append(doc)

        self.logger.info(
            "Extracted web pages",
            extra={
                "event": "retrieve.web_extracted",
                "session_id": state["session_id"],
                "details": {"attempted": len(docs[:max_pages]), "succeeded": sum(1 for d in extracted if d.get("metadata", {}).get("extracted"))},
            },
        )
        return {**state, "web_docs": extracted}

    def _filter_web_docs(self, state: DialogueState) -> DialogueState:
        docs = state.get("web_docs", [])
        if not docs:
            return {**state, "web_docs": []}

        user_input = state["user_input"]
        indexed_docs = [
            (i, doc.get("page_content", "")[:2000]) for i, doc in enumerate(docs)
        ]
        items_text = "\n\n".join(
            f"[{idx}] {content}" for idx, content in indexed_docs
        )
        system = "判断下列网络资料是否与用户输入相关。请为每条资料返回是否相关。"
        prompt = f"用户输入：{user_input}\n\n资料列表：\n{items_text}\n\n请返回每条资料的相关性判断。"

        try:
            result = self.llm.structured_chat(
                system, prompt, response_format=WebRelevanceFilterResult, temperature=0.0
            )
            decision = WebRelevanceFilterResult.model_validate(result)
            relevant_indices = {item.index for item in decision.results if item.is_relevant}
            filtered = [doc for i, doc in enumerate(docs) if i in relevant_indices]
        except Exception:
            filtered = docs

        # Fallback: if the LLM filtered out everything, keep the original docs.
        if not filtered and docs:
            filtered = docs

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
        docs = self.chroma.query_personality(query, top_k=5)
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
        docs = self.chroma.query_memories(
            query, top_k=settings.memory_retrieval_top_k)

        dialogue_doc_ids = [
            doc["id"] for doc in docs
            if doc.get("metadata", {}).get("source_type") == "dialogue"
        ]
        if dialogue_doc_ids:
            now = datetime.now(timezone.utc).isoformat()
            self.chroma.increment_memory_recall(dialogue_doc_ids)
            # Reflect the updated recall_count in the state docs passed downstream.
            for doc in docs:
                if doc.get("id") in dialogue_doc_ids:
                    doc["metadata"]["recall_count"] = doc["metadata"].get(
                        "recall_count", 0) + 1
                    doc["metadata"]["last_recalled_at"] = now
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
            (i, doc.get("page_content", "")[:1500]) for i, doc in enumerate(docs)
        ]
        items_text = "\n\n".join(
            f"[{idx}] {content}" for idx, content in indexed_docs
        )
        system = "判断下列记忆内容是否与用户输入相关。请为每条记忆返回是否相关。"
        prompt = f"用户输入：{user_input}\n\n记忆列表：\n{items_text}\n\n请返回每条记忆的相关性判断。"

        try:
            result = self.llm.structured_chat(
                system, prompt, response_format=MemoryRelevanceFilterResult, temperature=0.0
            )
            decision = MemoryRelevanceFilterResult.model_validate(result)
            relevant_indices = {item.index for item in decision.results if item.is_relevant}
            filtered = [doc for i, doc in enumerate(docs) if i in relevant_indices]
        except Exception:
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
        inner_monologue = state.get("inner_monologue")

        dim_text = "\n".join(
            f"- {dim.description} (成功{dim.success_count} / 失败{dim.failure_count})"
            for dim in dimensions
        )
        web_text = "\n".join(
            f"- [{d['metadata'].get('title', 'web')}] {d['page_content']}"
            for d in web_docs
        )
        personality_text = "\n".join(
            f"- [{d['metadata'].get('source_type', 'line')
                  }] {d['page_content']}"
            for d in personality_docs
        )
        memory_text = "\n".join(f"- {d['page_content']}" for d in memory_docs)
        monologue_text = f"\n你当前的内心独白：{
            inner_monologue}\n" if inner_monologue else ""

        user_identity = state.get("user_identity")
        user_identity_text = ""
        if user_identity:
            user_identity_text = (
                f"\n与你对话的用户身份：{user_identity.name}（{user_identity.role}）。"
                f"{user_identity.description}\n"
            )

        system = f"""你是 {identity.name if identity else 'mr.data'}，{identity.role if identity else '一个对话程序'}。
{identity.base_prompt if identity else ''}{user_identity_text}

你的基础性格自白：
{dim_text}

与你人格相关的素材：
{personality_text}

与当前会话相关的记忆：
{memory_text}
{monologue_text}
来自网络的资料（仅供参考，不要违背你的人格设定）：
{web_text}

请根据以上性格自白、人格素材、记忆、内心独白和网络资料生成回复，保持性格一致性。
"""
        prompt = f"用户说：{state['user_input']}\n请回复："
        reply = self.llm.chat(system, prompt, temperature=0.8)
        self.logger.info(
            "Generated reply",
            extra={
                "event": "chat.reply",
                "session_id": state["session_id"],
                "details": {"reply_length": len(reply)},
            },
        )
        return {**state, "reply": reply}

    def _log_dialogue(self, state: DialogueState) -> DialogueState:
        session_id = state["session_id"]
        user_input = state["user_input"]
        reply = state["reply"]
        dimensions = state.get("dimensions", [])
        personality_docs = state.get("personality_docs", [])
        web_docs = state.get("web_docs", [])
        inner_monologue = state.get("inner_monologue")

        self.pg.insert_dialogue(
            DialogueLog(session_id=session_id, role="user", content=user_input)
        )
        assistant_log_id = self.pg.insert_dialogue(
            DialogueLog(session_id=session_id, role="assistant", content=reply)
        )

        # 记录加载的基础维度
        self.pg.insert_dialogue_dimension_refs(
            assistant_log_id, [dim.id for dim in dimensions if dim.id])

        # 记录检索到的向量素材（人格素材 + 网络资料）
        vector_refs = [
            DialogueVectorRef(
                dialogue_log_id=assistant_log_id,
                vector_doc_id=doc["id"],
                source_type=doc["metadata"].get("source_type", "line"),
                content=doc["page_content"],
                dimension_ids=doc["metadata"].get("dimension_ids", []),
            )
            for doc in personality_docs
        ]
        vector_refs += [
            DialogueVectorRef(
                dialogue_log_id=assistant_log_id,
                vector_doc_id=doc["id"],
                source_type="web",
                content=doc["page_content"],
                dimension_ids=[],
            )
            for doc in web_docs
        ]
        self.pg.insert_dialogue_vector_refs(assistant_log_id, vector_refs)

        # 写入记忆向量库
        self.chroma.add_memory(session_id, f"用户：{user_input}")
        self.chroma.add_memory(session_id, f"助手：{reply}")

        # 把网络资料作为世界知识写入记忆向量库
        retrieval_query = state.get("retrieval_query", user_input)
        retrieved_at = datetime.now(timezone.utc).isoformat()
        for doc in web_docs:
            metadata = doc.get("metadata", {})
            title = metadata.get("title", "")
            url = metadata.get("url", "")
            content = doc.get("page_content", "")
            memory_content = f"[网络资料] {title}\n{url}\n{content[:800]}"
            self.chroma.add_memory(
                session_id,
                memory_content,
                metadata={
                    "session_id": session_id,
                    "source_type": "web",
                    "url": url,
                    "title": title,
                    "retrieved_at": retrieved_at,
                    "query": retrieval_query,
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
                },
            },
        )

        return {**state, "assistant_log_id": assistant_log_id}

    def chat(self, session_id: str, user_input: str) -> str:
        self.logger.info(
            "Chat turn started",
            extra={"event": "chat.start", "session_id": session_id,
                   "details": {"user_input": user_input}},
        )
        state: DialogueState = {
            "session_id": session_id,
            "user_input": user_input,
        }
        try:
            final_state = self.graph.invoke(state)
            return final_state["reply"]
        except Exception as exc:
            self.logger.exception(
                "Chat turn failed",
                extra={"event": "chat.error", "session_id": session_id,
                       "details": {"error": str(exc)}},
            )
            raise
