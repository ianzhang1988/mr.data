import json
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore
from mr_data.db.chroma import (
    dialogue_chunk_memory_id,
    evidence_doc_id,
    event_doc_id,
)
from mr_data.llm import LLMClient
from mr_data.logging import get_logger
from mr_data.models import (
    AdjustmentLog,
    DialogueLog,
    DialogueMemoryMetadata,
    DialogueVectorRef,
    DimensionDedupResult,
    PersonalityEvent,
    PersonalityDimension,
)


class DimensionDelta(BaseModel):
    dimension_id: Optional[int] = Field(default=None, description="已有维度 ID；为空且给出 new_dimension_description 时新建")
    new_dimension_description: Optional[str] = Field(default=None, description="新建维度时的第一人称描述性自白；仅当所有既有维度都无法对应时填写，与 dimension_id 二选一")
    new_dimension_reason: Optional[str] = Field(default=None, description="新建维度时必填：为什么既有维度都无法涵盖该性格表现，须引用具体对话证据")
    delta_success: int = Field(default=0, ge=0, description="成功计数增加量")
    delta_failure: int = Field(default=0, ge=0, description="失败计数增加量")
    reason: str = Field(description="变化原因")
    event_summary: Optional[str] = Field(default=None, description="如果这条对话值得记录为人格事件，请写一句话摘要；否则留空")
    evidence_snippets: list[str] = Field(default_factory=list, description="支撑该归因的关键对话或思考片段")
    relation_to_personality: Optional[str] = Field(default=None, description="证据与基础性格的关系，例如：体现、强化、违背、修正")
    target_dialogue_log_id: Optional[int] = Field(default=None, description="该归因主要对应的 assistant 回复日志 ID")


class AttributionResult(BaseModel):
    deltas: list[DimensionDelta] = Field(default_factory=list)


def chunk_dialogue_logs(
    logs: list[DialogueLog], max_chars: int, overlap_lines: int
) -> list[dict]:
    """把会话日志按字符预算分段，段间保留 overlap_lines 行重叠。
    返回 [{"content", "chunk_index", "first_log_id", "last_log_id"}]。"""
    sorted_logs = sorted(logs, key=lambda x: x.created_at or 0)
    lines = [
        (f"{'user' if log.role == 'user' else 'assistant'}: {log.content}", log.id)
        for log in sorted_logs
    ]

    chunks: list[dict] = []
    cur_lines: list[str] = []  # 当前段的全部行（含 overlap 前缀）
    cur_ids: list[Any] = []  # 当前段实际覆盖的 log id（不含 overlap 行）
    cur_chars = 0  # 实际覆盖行的字符预算（不含 overlap 行）

    for line, log_id in lines:
        # 当前段已有覆盖行且加入下一行会超预算时关闭当前段；
        # 当前段为空（或仅剩 overlap 行）时该行直接成段，不截断内容。
        if cur_ids and cur_chars + len(line) > max_chars:
            chunks.append(
                {
                    "content": "\n".join(cur_lines),
                    "chunk_index": len(chunks),
                    "first_log_id": cur_ids[0],
                    "last_log_id": cur_ids[-1],
                }
            )
            cur_lines = cur_lines[-overlap_lines:] if overlap_lines > 0 else []
            cur_ids = []
            cur_chars = 0
        cur_lines.append(line)
        cur_ids.append(log_id)
        cur_chars += len(line)

    if cur_ids:
        chunks.append(
            {
                "content": "\n".join(cur_lines),
                "chunk_index": len(chunks),
                "first_log_id": cur_ids[0],
                "last_log_id": cur_ids[-1],
            }
        )
    return chunks


class AttributionEngine:
    def __init__(
        self,
        pg_store: Optional[PostgresStore] = None,
        chroma_store: Optional[ChromaStore] = None,
        llm: Optional[LLMClient] = None,
    ):
        self.pg = pg_store or PostgresStore()
        self.chroma = chroma_store or ChromaStore()
        self.llm = llm or LLMClient()
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
            if result is None:
                self.logger.info(
                    "Session attribution failed, will retry next run",
                    extra={"event": "offline.session_failed", "session_id": session.id},
                )
                continue
            with self.pg.transaction():
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
            if log.id is not None:
                label += f"[#{log.id}]"
            monologue = log.metadata.inner_monologue if log.metadata else None
            if log.role == "assistant" and monologue:
                label += f"（内心独白：{monologue}）"
            lines.append(f"{label}: {log.content}")
        return "\n".join(lines)

    def _build_context(self, session_id: str) -> str:
        identity = self.pg.get_identity()
        dimensions = self.pg.list_dimensions(active_only=True)

        activated_ids = set(self.pg.list_session_dimension_ids(session_id))

        def render(dims: list) -> str:
            return "\n".join(
                f"- [{dim.id}] {dim.description} (成功 {dim.success_count} / 失败 {dim.failure_count})"
                for dim in dims
            )

        activated = [d for d in dimensions if d.id in activated_ids]
        remaining = [d for d in dimensions if d.id not in activated_ids]
        activated_text = render(activated) if activated else "（无记录）"
        remaining_text = render(remaining) if remaining else "（无）"

        identity_text = ""
        if identity:
            identity_text = f"""
你的名字：{identity.name}
你的角色：{identity.role}
你的人设说明：
{identity.base_prompt}
""".strip()

        return f"""{identity_text}

本次会话中实际激活的性格维度：
{activated_text}

其余活跃的性格维度（新增维度前请先对照去重）：
{remaining_text}
""".strip()

    def _attribute_session(self, session_id: str, logs: list[DialogueLog]) -> Optional[AttributionResult]:
        transcript = self._build_transcript(logs)
        context = self._build_context(session_id)

        system = """你是对话归因分析器。请阅读下面提供的完整会话记录（assistant 行附该轮的内心独白），并结合人设、性格维度分组（本次会话激活/其余活跃），完成以下任务：
1. 判断哪些性格维度促成了成功或失败。
2. 如果维度已存在，给出其 dimension_id（整数）；仅当所有既有维度都明显无法涵盖该性格表现时才新建维度：给出 new_dimension_description（第一人称描述性自白）和 new_dimension_reason（为什么既有维度都不能覆盖它，须引用具体对话证据）；新建前必须先逐条对照“其余活跃的性格维度”列表，拿不准就归入既有维度。
3. 给出每个维度的 delta_success、delta_failure 和变化原因 reason。
4. 针对每个维度变化，提取 0-N 条关键证据片段 evidence_snippets（原始对话、内心独白原文），并说明该证据与基础性格的关系 relation_to_personality（例如：体现、强化、违背、修正）。
5. 如果某条对话值得记录为长期人格事件，请写 event_summary；否则留空。
6. 使用 target_dialogue_log_id 标注该归因主要对应的 assistant 回复日志 ID（必须取自 transcript 中 assistant 行的 [#编号]，没有明确对应时留空）。
7. 最终输出的 deltas 最多 3 条：只保留与本会话有强关联的维度变化，按关联强度降序排列；证据不足、牵强的关联不要输出。

请严格按 JSON 格式返回，不要输出任何其他内容：
{"deltas": [{"dimension_id": 1, "new_dimension_description": null, "new_dimension_reason": null, "delta_success": 1, "delta_failure": 0, "reason": "...", "event_summary": "...", "evidence_snippets": ["..."], "relation_to_personality": "体现", "target_dialogue_log_id": 123}]}
"""
        prompt = f"""{context}

本次会话记录：
{transcript}

请按 JSON 格式返回归因结果。"""

        try:
            result = self.llm.chat_structured(
                system, prompt, response_format=AttributionResult, temperature=0.2
            )
            parsed = AttributionResult.model_validate(result)
        except Exception:
            self.logger.warning(
                "Failed to parse attribution response",
                extra={
                    "event": "offline.parse_error",
                    "session_id": session_id,
                },
            )
            return None

        # 事务外：新维度重合判断（防维度膨胀），随后按配置截断为最强 N 条，
        # 最后校验 LLM 返回的数据库相关 id（防幻觉 id 触发外键违规/串号污染）。
        self._dedup_new_dimensions(session_id, parsed)
        parsed.deltas = parsed.deltas[: settings.offline_max_deltas_per_session]
        self._validate_delta_ids(session_id, logs, parsed)
        return parsed

    def _validate_delta_ids(
        self, session_id: str, logs: list[DialogueLog], result: AttributionResult
    ) -> None:
        """校验 LLM 返回的数据库相关 id，非法 id 就地置 None 并记 warning。

        - dimension_id 必须在活跃维度白名单内（否则 adjustment_logs 的外键
          会在事务内炸掉整个离线批处理）；
        - target_dialogue_log_id 必须是本会话 assistant 行的日志 id（防幻觉、
          防指向 user 行、防跨 session 串号）。
        """
        assistant_ids = {
            log.id for log in logs if log.role == "assistant" and log.id is not None
        }
        active_dim_ids = {
            d.id for d in self.pg.list_dimensions(active_only=True) if d.id is not None
        }
        for delta in result.deltas:
            if delta.dimension_id is not None and delta.dimension_id not in active_dim_ids:
                self.logger.warning(
                    "LLM returned unknown dimension id, discarded",
                    extra={
                        "event": "offline.invalid_dimension_id",
                        "session_id": session_id,
                        "details": {"dimension_id": delta.dimension_id},
                    },
                )
                delta.dimension_id = None
            if (
                delta.target_dialogue_log_id is not None
                and delta.target_dialogue_log_id not in assistant_ids
            ):
                self.logger.warning(
                    "LLM returned invalid target dialogue log id, discarded",
                    extra={
                        "event": "offline.invalid_target_log_id",
                        "session_id": session_id,
                        "details": {"target_dialogue_log_id": delta.target_dialogue_log_id},
                    },
                )
                delta.target_dialogue_log_id = None

    def _dedup_new_dimensions(self, session_id: str, result: AttributionResult) -> None:
        """对候选新维度做一次批量 LLM 重合判断（事务外调用）。

        命中既有活跃维度时把 delta 改挂到该维度（改写 dimension_id、清空
        new_dimension_*）；LLM 调用失败或返回幻觉 id 时按“允许新建”降级，
        不阻断离线批处理。
        """
        candidates = [
            delta
            for delta in result.deltas
            if delta.dimension_id is None and delta.new_dimension_description
        ]
        if not candidates:
            return
        if not settings.enable_dimension_dedup:
            self.logger.debug(
                "Dimension dedup disabled by config",
                extra={"event": "offline.dimension_dedup_skipped", "session_id": session_id},
            )
            return

        dimensions: list[PersonalityDimension] = self.pg.list_dimensions(active_only=True)
        valid_ids = {d.id for d in dimensions if d.id is not None}
        if not valid_ids:
            return

        candidate_blocks = []
        for idx, delta in enumerate(candidates):
            evidence = "；".join(delta.evidence_snippets[:3]) or "（无）"
            candidate_blocks.append(
                f"[{idx}] 描述：{delta.new_dimension_description}\n"
                f"    新增理由：{delta.new_dimension_reason or '（未给出）'}\n"
                f"    证据：{evidence}"
            )
        dimension_lines = "\n".join(
            f"- [{dim.id}] {dim.description} (成功 {dim.success_count} / 失败 {dim.failure_count})"
            for dim in dimensions
            if dim.id is not None
        )

        system = """你是性格维度归并判断器。给你若干“候选新维度”和一份“既有活跃维度”列表，请逐一判断候选是否与某个既有维度语义重合（描述的是同一种性格特质，只是措辞不同也算重合）。
判断从严：只有明显重合才给出 matched_dimension_id；确实涵盖不了才允许新建（matched_dimension_id 为 null）。
请严格按 JSON 格式返回，不要输出任何其他内容：
{"matches": [{"candidate_index": 0, "matched_dimension_id": null}]}
"""
        prompt = f"""既有活跃维度：
{dimension_lines}

候选新维度：
{chr(10).join(candidate_blocks)}

请按 JSON 格式返回判断结果。"""

        try:
            raw = self.llm.chat_structured(
                system, prompt, response_format=DimensionDedupResult, temperature=0.0
            )
            dedup = DimensionDedupResult.model_validate(raw)
        except Exception:
            self.logger.warning(
                "Dimension dedup LLM call failed, fallback to allowing new dimensions",
                extra={"event": "offline.dimension_dedup_failed", "session_id": session_id},
            )
            return

        for match in dedup.matches:
            if match.matched_dimension_id is None:
                continue
            if not 0 <= match.candidate_index < len(candidates):
                continue
            if match.matched_dimension_id not in valid_ids:
                self.logger.warning(
                    "Dimension dedup returned unknown dimension id, ignored",
                    extra={
                        "event": "offline.dimension_dedup_invalid_id",
                        "session_id": session_id,
                        "details": {"matched_dimension_id": match.matched_dimension_id},
                    },
                )
                continue
            delta = candidates[match.candidate_index]
            self.logger.info(
                "New dimension merged into existing one",
                extra={
                    "event": "offline.dimension_dedup_merged",
                    "session_id": session_id,
                    "details": {
                        "matched_dimension_id": match.matched_dimension_id,
                        "description": delta.new_dimension_description,
                    },
                },
            )
            delta.dimension_id = match.matched_dimension_id
            delta.new_dimension_description = None
            delta.new_dimension_reason = None

    def _apply(self, result: AttributionResult, session_id: str, logs: list[DialogueLog]) -> int:
        # 不再提供 fallback target：无有效 target 时证据/事件不写向量库，
        # 避免 doc id 哈希原料与 source_id 被 "None" 污染（改进 54）。
        sorted_logs = sorted(logs, key=lambda x: x.created_at or 0)

        applied = 0
        for delta in result.deltas:
            dim_id = delta.dimension_id
            if dim_id is None and delta.new_dimension_description:
                dim_id = self.pg.insert_dimension(delta.new_dimension_description)
                self.logger.info(
                    "New personality dimension created",
                    extra={
                        "event": "offline.dimension_created",
                        "session_id": session_id,
                        "details": {
                            "dimension_id": dim_id,
                            "description": delta.new_dimension_description,
                            "new_dimension_reason": delta.new_dimension_reason,
                        },
                    },
                )

            if dim_id is None:
                continue

            target_log_id = delta.target_dialogue_log_id

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
                    event_summary=delta.event_summary,
                )
            )
            applied += 1

            # 下面两个代码块的作用时“发育”性格向量库
            #

            # Persist evidence snippets to personality collection.
            # 这里筛选的是有价值对话，通过搜索对话上下文，返回agent当时的回答，作为有价值的参考
            if delta.evidence_snippets and target_log_id is not None:
                context = self._build_evidence_context(logs, target_log_id)
                for snippet in delta.evidence_snippets:
                    event = PersonalityEvent(
                        id=evidence_doc_id(target_log_id, dim_id, snippet),
                        content=snippet,
                        context=context,
                        speaker="assistant",
                        dimension_ids=[dim_id],
                        source_type="evidence",
                        source_id=str(target_log_id),
                    )
                    doc_id = self.chroma.add_personality_event(event)
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
            elif delta.evidence_snippets:
                self.logger.info(
                    "Evidence snippets skipped: no valid target dialogue log id",
                    extra={
                        "event": "offline.evidence_skipped_no_target",
                        "session_id": session_id,
                        "details": {"dimension_id": dim_id},
                    },
                )

            # Persist high-level event summary if provided.
            # 这里是对pg中存在的维度，记录了维度的实际使用过程，作为后续性格的参考
            if delta.event_summary and target_log_id is not None:
                event = PersonalityEvent(
                    id=event_doc_id(target_log_id, dim_id, delta.event_summary),
                    content=delta.event_summary,
                    dimension_ids=[dim_id],
                    source_type="event",
                    source_id=str(target_log_id),
                )
                self.chroma.add_personality_event(event)
            elif delta.event_summary:
                self.logger.info(
                    "Event summary skipped: no valid target dialogue log id",
                    extra={
                        "event": "offline.evidence_skipped_no_target",
                        "session_id": session_id,
                        "details": {"dimension_id": dim_id},
                    },
                )

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

        # Persist session dialogue chunks to the memory vector store for future recall.
        self._persist_session_memories(session_id, sorted_logs)

        self.logger.info(
            "Session attribution applied",
            extra={
                "event": "offline.session_processed",
                "session_id": session_id,
                "details": {"delta_count": applied},
            },
        )
        return applied

    def _persist_session_memories(self, session_id: str, logs: list[DialogueLog]) -> None:
        """Chunk session dialogue logs and persist them to the memory vector store."""
        if not logs:
            return
        chunks = chunk_dialogue_logs(
            logs,
            settings.memory_dialogue_chunk_chars,
            settings.memory_dialogue_chunk_overlap_lines,
        )
        now = datetime.now(timezone.utc).isoformat()
        for chunk in chunks:
            self.chroma.add_memory(
                session_id,
                chunk["content"],
                memory_id=dialogue_chunk_memory_id(
                    session_id,
                    chunk["first_log_id"],
                    chunk["last_log_id"],
                    chunk["content"],
                ),
                metadata=DialogueMemoryMetadata(
                    session_id=session_id,
                    chunk_index=chunk["chunk_index"],
                    first_dialogue_log_id=chunk["first_log_id"],
                    last_dialogue_log_id=chunk["last_log_id"],
                    added_at=now,
                ),
            )

    def _build_evidence_context(self, logs: list[DialogueLog], target_log_id: int) -> str:
        """Build a short transcript context around the target dialogue log."""
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
