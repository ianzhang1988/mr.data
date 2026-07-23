import json
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore
from mr_data.llm import LLMClient
from mr_data.logging import get_logger, read_session_events
from mr_data.models import AdjustmentLog, DialogueLog, DialogueVectorRef, PersonalityEvent


class DimensionDelta(BaseModel):
    dimension_id: Optional[int] = Field(default=None, description="已有维度 ID；为空时按 description 新建")
    description: Optional[str] = Field(default=None, description="新建维度时的描述；与 dimension_id 二选一")
    delta_success: int = Field(default=0, ge=0, description="成功计数增加量")
    delta_failure: int = Field(default=0, ge=0, description="失败计数增加量")
    reason: str = Field(description="变化原因")
    event_summary: Optional[str] = Field(default=None, description="如果这条对话值得记录为人格事件，请写一句话摘要；否则留空")
    evidence_snippets: list[str] = Field(default_factory=list, description="支撑该归因的关键对话或思考片段")
    relation_to_personality: Optional[str] = Field(default=None, description="证据与基础性格的关系，例如：体现、强化、违背、修正")
    target_dialogue_log_id: Optional[int] = Field(default=None, description="该归因主要对应的 assistant 回复日志 ID")


class AttributionResult(BaseModel):
    deltas: list[DimensionDelta] = Field(default_factory=list)


class AttributionEngine:
    def __init__(
        self,
        pg_store: Optional[PostgresStore] = None,
        chroma_store: Optional[ChromaStore] = None,
        llm: Optional[LLMClient] = None,
        log_dir: Optional[str] = None,
    ):
        self.pg = pg_store or PostgresStore()
        self.chroma = chroma_store or ChromaStore()
        self.llm = llm or LLMClient()
        self.log_dir = log_dir
        self.logger = get_logger("mr_data.offline")

    def run(self, lookback_days: Optional[int] = None, batch_size: Optional[int] = None) -> None:
        limit = batch_size or settings.offline_batch_size

        sessions = self.pg.list_closed_sessions_with_unprocessed(limit=limit)
        if not sessions:
            self.logger.info("No closed sessions with unprocessed dialogues to attribute.")
            print("No closed sessions with unprocessed dialogues to attribute.")
            return

        total_sessions = 0
        total_deltas = 0
        for session in sessions:
            logs = self.pg.get_recent_dialogues(
                session_id=session.id,
                unprocessed_only=True,
                limit=settings.offline_max_session_logs,
                lookback_days=lookback_days,
            )
            if not logs:
                continue

            result = self._attribute_session(session.id, logs)
            applied = self._apply(result, session.id, logs)

            for log in logs:
                self.pg.mark_dialogue_processed(log.id)

            total_sessions += 1
            total_deltas += applied

        pruned_count = self.chroma.prune_stale_dialogue_memories(
            settings.memory_dialogue_retention_days,
            settings.memory_min_recall_count,
        )

        self.logger.info(
            "Offline attribution completed",
            extra={
                "event": "offline.completed",
                "details": {
                    "session_count": total_sessions,
                    "delta_count": total_deltas,
                    "pruned_memories": pruned_count,
                },
            },
        )
        print(f"Processed {total_sessions} closed sessions.")

    def _build_transcript(self, logs: list[DialogueLog]) -> str:
        sorted_logs = sorted(logs, key=lambda x: x.created_at or 0)
        lines = []
        for log in sorted_logs:
            label = "user" if log.role == "user" else "assistant"
            lines.append(f"{label}: {log.content}")
        return "\n".join(lines)

    def _load_session_thoughts(self, session_id: str) -> list[dict[str, Any]]:
        return read_session_events(session_id, event_prefix="think.", log_dir=self.log_dir)

    def _build_context(self, session_id: str, transcript: str) -> str:
        identity = self.pg.get_identity()
        dimensions = self.pg.list_dimensions(active_only=True)

        dim_text = "\n".join(
            f"- [{dim.id}] {dim.description} (成功 {dim.success_count} / 失败 {dim.failure_count})"
            for dim in dimensions
        )

        # Retrieve relevant historical personality materials using the transcript as query.
        query = transcript[:500] if transcript else session_id
        retrieved = self.chroma.query_personality(query, top_k=settings.personality_retrieval_top_k)
        personality_text = "\n".join(
            f"- [{', '.join(str(d) for d in doc['metadata'].get('dimension_ids', []))}] {doc['page_content']}"
            for doc in retrieved
        )

        # Load assistant thinking process from structured logs.
        thoughts = self._load_session_thoughts(session_id)
        thought_text = "\n".join(
            f"- [{t.get('event')}] {t.get('details', {})}"
            for t in thoughts
        )

        identity_text = ""
        if identity:
            identity_text = f"""
你的名字：{identity.name}
你的角色：{identity.role}
你的人设说明：
{identity.base_prompt}
""".strip()

        return f"""{identity_text}

当前活跃的性格维度：
{dim_text}

与本次会话相关的人格素材（来自向量库）：
{personality_text if personality_text else '（暂无）'}

助手在本次会话中的思考过程（检索查询、内心独白等）：
{thought_text if thought_text else '（暂无）'}
""".strip()

    def _attribute_session(self, session_id: str, logs: list[DialogueLog]) -> AttributionResult:
        transcript = self._build_transcript(logs)
        context = self._build_context(session_id, transcript)

        system = """你是对话归因分析器。请阅读下面提供的完整会话记录，并结合人设、当前性格维度、历史人格素材以及助手的思考过程，完成以下任务：
1. 判断哪些性格维度促成了成功或失败。
2. 如果维度已存在，给出其 dimension_id（整数）；如果是新维度，给出 description（描述性自白）。
3. 给出每个维度的 delta_success、delta_failure 和变化原因 reason。
4. 针对每个维度变化，提取 0-N 条关键证据片段 evidence_snippets（原始对话、思考过程原文），并说明该证据与基础性格的关系 relation_to_personality（例如：体现、强化、违背、修正）。
5. 如果某条对话值得记录为长期人格事件，请写 event_summary；否则留空。
6. 使用 target_dialogue_log_id 标注该归因主要对应的 assistant 回复日志 ID（可选）。

请严格按 JSON 格式返回，不要输出任何其他内容：
{"deltas": [{"dimension_id": 1, "description": null, "delta_success": 1, "delta_failure": 0, "reason": "...", "event_summary": "...", "evidence_snippets": ["..."], "relation_to_personality": "体现", "target_dialogue_log_id": 123}]}
"""
        prompt = f"""{context}

本次会话记录：
{transcript}

请按 JSON 格式返回归因结果。"""

        try:
            result = self.llm.structured_chat(
                system, prompt, response_format=AttributionResult, temperature=0.2
            )
            return AttributionResult.model_validate(result)
        except Exception:
            self.logger.warning(
                "Failed to parse attribution response",
                extra={
                    "event": "offline.parse_error",
                    "session_id": session_id,
                },
            )
            return AttributionResult()

    def _apply(self, result: AttributionResult, session_id: str, logs: list[DialogueLog]) -> int:
        # Precompute a fallback assistant log id for evidence that lacks an explicit target.
        sorted_logs = sorted(logs, key=lambda x: x.created_at or 0)
        fallback_assistant_id: Optional[int] = None
        for log in reversed(sorted_logs):
            if log.role == "assistant" and log.id is not None:
                fallback_assistant_id = log.id
                break

        applied = 0
        for delta in result.deltas:
            dim_id = delta.dimension_id
            if dim_id is None and delta.description:
                dim_id = self.pg.insert_dimension(delta.description)

            if dim_id is None:
                continue

            target_log_id = delta.target_dialogue_log_id or fallback_assistant_id

            self.pg.update_dimension(
                dim_id,
                delta_success=delta.delta_success,
                delta_failure=delta.delta_failure,
            )
            self.pg.insert_adjustment(
                AdjustmentLog(
                    dimension_id=dim_id,
                    session_id=session_id,
                    delta_success=delta.delta_success,
                    delta_failure=delta.delta_failure,
                    reason=delta.reason,
                    dialogue_log_id=target_log_id,
                )
            )
            applied += 1

            # Persist evidence snippets to personality collection.
            if delta.evidence_snippets:
                context = self._build_evidence_context(logs, target_log_id)
                for snippet in delta.evidence_snippets:
                    event = PersonalityEvent(
                        content=snippet,
                        context=context,
                        speaker="assistant",
                        dimension_ids=[dim_id],
                        source_type="evidence",
                        source_id=str(target_log_id),
                    )
                    doc_id = self.chroma.add_personality_event(event)
                    if target_log_id is not None:
                        self.pg.insert_dialogue_vector_refs(
                            target_log_id,
                            [
                                DialogueVectorRef(
                                    dialogue_log_id=target_log_id,
                                    vector_doc_id=doc_id,
                                    source_type="evidence",
                                    content=snippet,
                                    dimension_ids=[dim_id],
                                )
                            ],
                        )

            # Persist high-level event summary if provided.
            if delta.event_summary:
                event = PersonalityEvent(
                    content=delta.event_summary,
                    dimension_ids=[dim_id],
                    source_type="event",
                    source_id=str(target_log_id),
                )
                self.chroma.add_personality_event(event)

            # Check pruning threshold. Core dimensions stay active for stability.
            dim = self.pg.get_dimension(dim_id)
            if dim and not dim.core and dim.failure_count >= settings.failure_threshold:
                self.pg.deactivate_dimension(dim_id)
                self._purge_dimension_vectors(dim_id)
                self.logger.info(
                    "Deactivated dimension due to failure threshold",
                    extra={
                        "event": "offline.dimension_deactivated",
                        "session_id": session_id,
                        "details": {"dimension_id": dim_id, "failure_count": dim.failure_count},
                    },
                )

        # Persist session dialogue turns to the memory vector store for future recall.
        now = datetime.now(timezone.utc).isoformat()
        for log in sorted_logs:
            content = f"{log.role}: {log.content}"
            self.chroma.add_memory(
                session_id,
                content,
                metadata={
                    "source_type": "dialogue",
                    "session_id": session_id,
                    "dialogue_log_id": log.id,
                    "role": log.role,
                    "recall_count": 0,
                    "added_at": now,
                    "last_recalled_at": "",
                },
            )

        self.logger.info(
            "Session attribution applied",
            extra={
                "event": "offline.session_processed",
                "session_id": session_id,
                "details": {"delta_count": applied},
            },
        )
        return applied

    def _build_evidence_context(self, logs: list[DialogueLog], target_log_id: Optional[int]) -> str:
        """Build a short transcript context around the target dialogue log."""
        if target_log_id is None:
            return self._build_transcript(logs)
        sorted_logs = sorted(logs, key=lambda x: x.created_at or 0)
        try:
            idx = next(i for i, log in enumerate(sorted_logs) if log.id == target_log_id)
        except StopIteration:
            return self._build_transcript(logs)
        start = max(0, idx - 2)
        end = min(len(sorted_logs), idx + 2)
        lines = []
        for log in sorted_logs[start:end]:
            label = "user" if log.role == "user" else "assistant"
            lines.append(f"{label}: {log.content}")
        return "\n".join(lines)

    def _purge_dimension_vectors(self, dimension_id: int) -> None:
        # Remove docs tracked via Postgres refs first.
        refs = self.pg.get_dialogue_vector_refs_by_dimension(dimension_id)
        doc_ids = list({ref.vector_doc_id for ref in refs})
        if doc_ids:
            self.chroma.delete_personality_events(doc_ids)
        self.pg.delete_dialogue_vector_refs_by_dimension(dimension_id)

        # Fallback: remove any remaining personality docs referencing this dimension
        # (e.g. evidence added without a Postgres ref or manually seeded lines).
        remaining = self.chroma.get_personality_event_ids_by_dimension(dimension_id)
        if remaining:
            self.chroma.delete_personality_events(remaining)
