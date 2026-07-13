from typing import Annotated, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore
from mr_data.llm import LLMClient
from mr_data.models import (
    DialogueLog,
    DialogueVectorRef,
    FixedIdentity,
    PersonalityDimension,
)
from mr_data.online.web_search import WebSearchTool


def _merge_docs(old: list[dict], new: list[dict]) -> list[dict]:
    return old + new


class DialogueState(TypedDict, total=False):
    session_id: str
    user_input: str
    identity: Optional[FixedIdentity]
    dimensions: list[PersonalityDimension]
    retrieval_query: str
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
    ):
        self.pg = pg_store or PostgresStore()
        self.chroma = chroma_store or ChromaStore()
        self.llm = llm or LLMClient()
        self.web_search = web_search or WebSearchTool()
        self.enable_web_search = (
            enable_web_search if enable_web_search is not None else settings.enable_web_search
        )
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(DialogueState)

        builder.add_node("load_personality", self._load_personality)
        builder.add_node("think", self._think)
        builder.add_node("retrieve_web", self._retrieve_web)
        builder.add_node("retrieve_personality", self._retrieve_personality)
        builder.add_node("retrieve_memories", self._retrieve_memories)
        builder.add_node("assemble_and_generate", self._assemble_and_generate)
        builder.add_node("log_dialogue", self._log_dialogue)

        builder.set_entry_point("load_personality")
        builder.add_edge("load_personality", "think")
        builder.add_edge("think", "retrieve_web")
        builder.add_edge("retrieve_web", "retrieve_personality")
        builder.add_edge("retrieve_personality", "retrieve_memories")
        builder.add_edge("retrieve_memories", "assemble_and_generate")
        builder.add_edge("assemble_and_generate", "log_dialogue")
        builder.add_edge("log_dialogue", END)

        return builder.compile()

    def _load_personality(self, state: DialogueState) -> DialogueState:
        identity = self.pg.get_identity()
        dimensions = self.pg.list_dimensions(active_only=True)
        return {
            **state,
            "identity": identity,
            "dimensions": dimensions,
        }

    def _think(self, state: DialogueState) -> DialogueState:
        system = "你是一个查询改写助手。将用户输入改写为适合向量检索的简短语义查询。"
        prompt = f"用户输入：{state['user_input']}\n请输出一个用于检索人格素材、记忆和网络资料的语义查询（一句话）："
        query = self.llm.chat(system, prompt, temperature=0.3).strip().replace("\n", " ")
        if not query:
            query = state["user_input"]
        return {**state, "retrieval_query": query}

    def _retrieve_web(self, state: DialogueState) -> DialogueState:
        if not self.enable_web_search:
            return {**state, "web_docs": []}
        docs = self.web_search.search(state["retrieval_query"])
        return {**state, "web_docs": docs}

    def _retrieve_personality(self, state: DialogueState) -> DialogueState:
        docs = self.chroma.query_personality(state["retrieval_query"], top_k=5)
        return {**state, "personality_docs": docs}

    def _retrieve_memories(self, state: DialogueState) -> DialogueState:
        docs = self.chroma.query_memories(state["retrieval_query"], session_id=state["session_id"], top_k=5)
        return {**state, "memory_docs": docs}

    def _assemble_and_generate(self, state: DialogueState) -> DialogueState:
        identity = state.get("identity")
        dimensions = state.get("dimensions", [])
        web_docs = state.get("web_docs", [])
        personality_docs = state.get("personality_docs", [])
        memory_docs = state.get("memory_docs", [])

        dim_text = "\n".join(
            f"- {dim.description} (成功{dim.success_count} / 失败{dim.failure_count})"
            for dim in dimensions
        )
        web_text = "\n".join(
            f"- [{d['metadata'].get('title', 'web')}] {d['page_content']}"
            for d in web_docs
        )
        personality_text = "\n".join(
            f"- [{d['metadata'].get('source_type', 'line')}] {d['page_content']}"
            for d in personality_docs
        )
        memory_text = "\n".join(f"- {d['page_content']}" for d in memory_docs)

        system = f"""你是 {identity.name if identity else 'mr.data'}，{identity.role if identity else '一个对话程序'}。
{identity.base_prompt if identity else ''}

你的基础性格自白：
{dim_text}

与你人格相关的素材：
{personality_text}

与当前会话相关的记忆：
{memory_text}

来自网络的资料（仅供参考，不要违背你的人格设定）：
{web_text}

请根据以上性格自白、人格素材、记忆和网络资料生成回复，保持性格一致性。
"""
        prompt = f"用户说：{state['user_input']}\n请回复："
        reply = self.llm.chat(system, prompt, temperature=0.8)
        return {**state, "reply": reply}

    def _log_dialogue(self, state: DialogueState) -> DialogueState:
        session_id = state["session_id"]
        user_input = state["user_input"]
        reply = state["reply"]
        dimensions = state.get("dimensions", [])
        personality_docs = state.get("personality_docs", [])
        web_docs = state.get("web_docs", [])

        self.pg.insert_dialogue(
            DialogueLog(session_id=session_id, role="user", content=user_input)
        )
        assistant_log_id = self.pg.insert_dialogue(
            DialogueLog(session_id=session_id, role="assistant", content=reply)
        )

        # 记录加载的基础维度
        self.pg.insert_dialogue_dimension_refs(assistant_log_id, [dim.id for dim in dimensions if dim.id])

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

        return {**state, "assistant_log_id": assistant_log_id}

    def chat(self, session_id: str, user_input: str) -> str:
        state: DialogueState = {
            "session_id": session_id,
            "user_input": user_input,
        }
        final_state = self.graph.invoke(state)
        return final_state["reply"]
