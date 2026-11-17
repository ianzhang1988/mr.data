import json
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore
from mr_data.db.chunking import chunk_dialogue_logs
from mr_data.db.chroma import (
    dialogue_chunk_memory_id,
    evidence_doc_id,
    event_doc_id,
)
from mr_data.llm import LLMClient
from mr_data.llm.tokenizer import TokenCounter
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

# 会话退出评分（-2..2）的文案映射，用于归因 prompt 展示
_RATING_LABELS = {-2: "很差", -1: "差", 0: "一般", 1: "好", 2: "很好"}


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


def purge_dimension_vectors(pg: PostgresStore, chroma: ChromaStore, dimension_id: int) -> None:
    """清除某维度在 Chroma personality 集合的全部向量子文档（两阶段：
    先按 PG refs 追踪删除，再兜底扫描 metadata dimension_ids 残留）。
    模块级函数，引擎淘汰与维度编辑模式删除共用（改进 90 抽取）。"""
    # Remove docs tracked via Postgres refs first.
    refs = pg.get_dialogue_vector_refs_by_dimension(dimension_id)
    doc_ids = list({ref.vector_doc_id for ref in refs})
    if doc_ids:
        chroma.delete_personality_events(doc_ids)
    pg.delete_dialogue_vector_refs_by_dimension(dimension_id)

    # Fallback: remove any remaining personality docs referencing this dimension
    # (e.g. evidence added without a Postgres ref or manually seeded lines).
    remaining = chroma.get_personality_event_ids_by_dimension(dimension_id)
    if remaining:
        chroma.delete_personality_events(remaining)


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

    def run(self, batch_size: Optional[int] = None) -> None:
        limit = batch_size or settings.offline_batch_size

        sessions = self.pg.list_closed_sessions_with_unprocessed(limit=limit)
        if not sessions:
            self.logger.info("No closed sessions with unprocessed dialogues to attribute.")
            print("No closed sessions with unprocessed dialogues to attribute.")

        total_sessions = 0
        total_deltas = 0
        failed_sessions = 0
        overlap = settings.offline_attribution_batch_overlap_lines
        per_session_limit = settings.offline_max_session_logs
        for session in sessions or []:
            # 拆批取数（改进 82）：本批 = 最新至多 per_session_limit 条未处理行
            # （DESC 最新优先）；上文回顾 = 已处理行中按时间紧邻本批上方的
            # overlap 条（单独查询——与大窗口同取会让窗口被已处理行占满、
            # 深部未处理行永远取不到）。上文行仅供 LLM 理解上下文，
            # 不重复归因、不可作 target。
            logs = self.pg.get_recent_dialogues(
                session_id=session.id,
                unprocessed_only=True,
                limit=per_session_limit,
            )
            if not logs:
                continue
            context_logs: list[DialogueLog] = []
            if overlap > 0 and logs[0].id is not None:
                context_logs = self.pg.get_adjacent_processed_dialogues(
                    session.id, logs[0].created_at, logs[0].id, overlap
                )

            # 批号 x/y：p/u 为会话级已处理/未处理总数。死信落葬会缩小 u，
            # y 为近似值（调查文档已注明）；x = p // limit + 1。
            processed_count = self.pg.count_dialogues(session.id)
            unprocessed_count = self.pg.count_dialogues(
                session.id, unprocessed_only=True
            )
            total_batches = (
                processed_count + unprocessed_count + per_session_limit - 1
            ) // per_session_limit
            batch_index = processed_count // per_session_limit + 1
            batched = total_batches > 1
            if unprocessed_count > per_session_limit:
                self.logger.info(
                    "Session exceeds per-session log limit, will be attributed in batches",
                    extra={
                        "event": "offline.session_logs_split",
                        "session_id": session.id,
                        "details": {
                            "batch_size": len(logs),
                            "limit": per_session_limit,
                            "total_batches": total_batches,
                        },
                    },
                )

            result = self._attribute_session(
                session.id,
                logs,
                context_logs=context_logs or None,
                batch_index=batch_index if batched else None,
                total_batches=total_batches if batched else None,
            )
            if result is None:
                attempts = self._count_failure_and_dead_letter(session.id, logs)
                if attempts < settings.offline_max_attribution_attempts:
                    self.logger.info(
                        "Session attribution failed, will retry next run",
                        extra={
                            "event": "offline.session_failed",
                            "session_id": session.id,
                            "details": {"attempts": attempts},
                        },
                    )
                continue
            try:
                with self.pg.transaction():
                    applied, pending_events, pending_purge = self._apply(result, session.id, logs)
                    for log in logs:
                        self.pg.mark_dialogue_processed(log.id)
            except Exception:
                # 事务已回滚（transaction() 自动 rollback），会话保持 unprocessed，
                # 下次 run() 重试；Chroma 写/删均在提交成功后执行，回滚时不产生
                # 任何 Chroma 副作用，无需自愈。
                attempts = self._count_failure_and_dead_letter(session.id, logs)
                if attempts < settings.offline_max_attribution_attempts:
                    self.logger.warning(
                        "Session apply failed and rolled back, will retry next run",
                        extra={
                            "event": "offline.apply_failed",
                            "session_id": session.id,
                            "details": {"attempts": attempts},
                        },
                    )
                failed_sessions += 1
                continue

            self._flush_post_commit_chroma(session.id, logs, pending_events, pending_purge)

            total_sessions += 1
            total_deltas += applied

        pruned_count = self.chroma.prune_stale_memories(
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
                    "failed_sessions": failed_sessions,
                    "pruned_memories": pruned_count,
                },
            },
        )
        print(f"Processed {total_sessions} closed sessions.")

    def _count_failure_and_dead_letter(
        self, session_id: str, logs: list[DialogueLog]
    ) -> int:
        """归因失败计数 +1 并返回累计次数；达 offline_max_attribution_attempts
        时把本批实际进过 transcript 的日志置为已处理（死信分片级落葬，
        改进 82）——不再 session 级连坐把从未取过的日志集体落葬。

        logs 为本批进过 transcript 的日志；attempts 仍是会话级：达阈值后
        后续每个分片失败一次即被即时落葬（调查文档 B 节已记录该边界）。
        never_in_transcript 字段保留以稳日志 schema，分片级落葬后恒为 0。"""
        attempts = self.pg.increment_attribution_attempts(session_id)
        if attempts >= settings.offline_max_attribution_attempts:
            marked = self.pg.mark_dialogues_processed(
                [log.id for log in logs if log.id is not None]
            )
            self.logger.warning(
                "Session attribution attempts exhausted, dead-lettered",
                extra={
                    "event": "offline.session_dead_lettered",
                    "session_id": session_id,
                    "details": {
                        "attempts": attempts,
                        "marked_dialogues": marked,
                        "in_transcript": marked,
                        "never_in_transcript": 0,
                    },
                },
            )
        return attempts

    def _build_transcript_lines(self, logs: list[DialogueLog]) -> list[str]:
        sorted_logs = sorted(logs, key=lambda x: (x.created_at is None, x.created_at or datetime.min, x.id or 0))
        lines = []
        for log in sorted_logs:
            label = "user" if log.role == "user" else "assistant"
            if log.id is not None:
                label += f"[#{log.id}]"
            monologue = log.metadata.inner_monologue if log.metadata else None
            if log.role == "assistant" and monologue:
                label += f"（内心独白：{monologue}）"
            lines.append(f"{label}: {log.content}")
        return lines

    def _build_transcript(self, logs: list[DialogueLog]) -> str:
        return "\n".join(self._build_transcript_lines(logs))

    def _fit_transcript_lines(
        self, lines: list[str], budget: int
    ) -> tuple[list[str], int]:
        """把 transcript 行裁剪进 token 预算：头尾保留、中间砍并插省略标记。

        返回 (保留行列表含省略标记, 省略行数)。行是 [#id] 完整性的单位，
        整行保留/整行丢弃，不做行内切割。标记本身的 token 也占预算；
        预算极小连头几行都装不下时至少保留第一行，不抛异常。
        """
        if not lines:
            return [], 0
        counter = TokenCounter()
        total = sum(counter.count(line) for line in lines)
        if total <= budget:
            return list(lines), 0

        # 先按上限位数预留标记 token，数字定稿后若仍超预算再从尾部丢行兜底。
        marker_reserve = counter.count("……（中间省略 000000 条消息）……") + 16
        head_budget = max(1, (budget - marker_reserve) // 2)
        kept_head: list[str] = []
        head_used = 0
        for line in lines:
            cost = counter.count(line)
            if not kept_head:
                # 退化情形：首行自身即超预算也至少保留第一行。
                kept_head.append(line)
                head_used += cost
                continue
            if head_used + cost > head_budget:
                break
            kept_head.append(line)
            head_used += cost

        remaining = budget - head_used - marker_reserve
        kept_tail: list[str] = []
        tail_used = 0
        if remaining > 0:
            for line in reversed(lines[len(kept_head):]):
                cost = counter.count(line)
                if kept_tail and tail_used + cost > remaining:
                    break
                kept_tail.append(line)
                tail_used += cost
            kept_tail.reverse()

        omitted = len(lines) - len(kept_head) - len(kept_tail)
        # 标记自身占预算：超了就从尾部丢行兜底（头部保底第一行不动）。
        while omitted > 0 and kept_tail:
            marker = f"……（中间省略 {omitted} 条消息）……"
            if head_used + tail_used + counter.count(marker) <= budget:
                break
            tail_used -= counter.count(kept_tail.pop())
            omitted += 1
        if omitted <= 0:
            return list(lines), 0
        return kept_head + [f"……（中间省略 {omitted} 条消息）……"] + kept_tail, omitted

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

        rating_text = ""
        session = self.pg.get_session(session_id)
        if session and session.rating is not None:
            rating_label = _RATING_LABELS.get(session.rating, "未知")
            rating_text = f"用户评分：{session.rating}（{rating_label}）"
            if session.rating_comment:
                rating_text += f"\n用户评论：{session.rating_comment}"
            rating_text = f"用户对本会话的评价：\n{rating_text}"

        parts = [
            f"""{identity_text}

本次会话中实际激活的性格维度：
{activated_text}

其余活跃的性格维度（新增维度前请先对照去重）：
{remaining_text}""".strip()
        ]
        if rating_text:
            parts.append(rating_text)
        return "\n\n".join(parts)

    def _attribute_session(
        self,
        session_id: str,
        logs: list[DialogueLog],
        context_logs: Optional[list[DialogueLog]] = None,
        batch_index: Optional[int] = None,
        total_batches: Optional[int] = None,
    ) -> Optional[AttributionResult]:
        batched = total_batches is not None and total_batches > 1
        context = self._build_context(session_id)
        lines = self._build_transcript_lines(logs)
        if context_logs:
            # 上文回顾段（改进 82 B1）：context 行时间晚于本批，附在批次行
            # 之后；不参与归因计数、不可作 target（id 校验白名单只用本批行）。
            lines = lines + [
                "（以下为上文回顾：已在此前批次完成归因，仅供理解上下文，不要对其再次归因）"
            ] + self._build_transcript_lines(context_logs)

        system = """你是对话归因分析器。请阅读下面提供的完整会话记录（assistant 行附该轮的内心独白），并结合人设、性格维度分组（本次会话激活/其余活跃），完成以下任务：
1. 判断哪些性格维度促成了成功或失败。
2. 如果维度已存在，给出其 dimension_id（整数）；仅当所有既有维度都明显无法涵盖该性格表现时才新建维度：给出 new_dimension_description（第一人称描述性自白）和 new_dimension_reason（为什么既有维度都不能覆盖它，须引用具体对话证据）；新建前必须先逐条对照“其余活跃的性格维度”列表，拿不准就归入既有维度。
3. 给出每个维度的 delta_success、delta_failure 和变化原因 reason。
4. 针对每个维度变化，提取 0-N 条关键证据片段 evidence_snippets（原始对话、内心独白原文），并说明该证据与基础性格的关系 relation_to_personality（例如：体现、强化、违背、修正）。
5. 如果某条对话值得记录为长期人格事件，请写 event_summary；否则留空。
6. 使用 target_dialogue_log_id 标注该归因主要对应的 assistant 回复日志 ID（必须取自 transcript 中 assistant 行的 [#编号]，没有明确对应时留空）。
7. 最终输出的 deltas 最多 3 条：只保留与本会话有强关联的维度变化，按关联强度降序排列；证据不足、牵强的关联不要输出。

用户对本会话的评分与评论（如有）是最直接的成败信号：低分或批评性评论应更严格地归因失败维度；评论若点名某种性格表现，优先对照既有维度归因；高分且评论肯定时可强化对应维度的成功计数。没有评价时按对话内容自行判断。

请严格按 JSON 格式返回，不要输出任何其他内容：
{"deltas": [{"dimension_id": 1, "new_dimension_description": null, "new_dimension_reason": null, "delta_success": 1, "delta_failure": 0, "reason": "...", "event_summary": "...", "evidence_snippets": ["..."], "relation_to_personality": "体现", "target_dialogue_log_id": 123}]}
"""
        if batched:
            # B3 分片语义标注 + B4 会话级信号规则（措辞定稿见
            # review/truncation-survey.md「B4 prompt 定稿」，逐字采用）。
            system = system.replace(
                "请阅读下面提供的完整会话记录",
                f"请阅读下面提供的本会话记录（第 {batch_index}/{total_batches} 批，非完整会话）",
            )
            system = system.replace(
                "用户对本会话的评分与评论（如有）是最直接的成败信号："
                "低分或批评性评论应更严格地归因失败维度；评论若点名某种性格表现，"
                "优先对照既有维度归因；高分且评论肯定时可强化对应维度的成功计数。"
                "没有评价时按对话内容自行判断。",
                f"用户对本会话的评分与评论（如有）是针对整个会话的总体评价，"
                f"本批（第 {batch_index}/{total_batches} 批）只是其中一段。使用规则：\n"
                "- 评分/评论用于校准判断倾向：差评背景下，对本批 assistant 表现中的"
                "可疑迹象从宽认定为失败迹象；好评背景下，对本批的优秀表现从宽认定成功。\n"
                "- 但成败计数必须锚定本批对话中的具体事件：reason 中应能指出本批记录里"
                "对应的那一轮对话。仅因评分而输出、本批对话中没有对应表现的维度计数，"
                "不要输出——导致该评价的表现若在其他批次，由对应批次归因。\n"
                "- 同理，高分且评论肯定时，只有本批中有具体优秀表现才强化成功计数。",
            )
        # transcript token 预算保护（改进 75）：长文粘贴型会话可能拼出远超
        # 模型上下文的 prompt，超上下文重试无效 → 会话死信。按预算头尾裁剪。
        counter = TokenCounter()
        if batched:
            header_plain = f"本次会话记录（第 {batch_index}/{total_batches} 批）："
            header_omitted = (
                f"本次会话记录（第 {batch_index}/{total_batches} 批，中间有省略）："
            )
        else:
            header_plain = "本次会话记录："
            header_omitted = "本次会话记录（中间有省略）："
        template_const = f"\n\n{header_plain}\n\n\n请按 JSON 格式返回归因结果。"
        budget = (
            settings.llm_context_token_limit
            - counter.count(system)
            - counter.count(context)
            - counter.count(template_const)
            - 256  # slack：覆盖截断后 system 措辞变长与计量误差
        )
        budget = max(budget, 16)
        fitted_lines, omitted = self._fit_transcript_lines(lines, budget)
        transcript = "\n".join(fitted_lines)
        if omitted > 0:
            if batched:
                system = system.replace(
                    f"请阅读下面提供的本会话记录（第 {batch_index}/{total_batches} 批，非完整会话）",
                    f"请阅读下面提供的本会话记录（第 {batch_index}/{total_batches} 批，"
                    f"非完整会话，中间部分已省略，共 {omitted} 条）",
                )
            else:
                system = system.replace(
                    "请阅读下面提供的完整会话记录",
                    f"请阅读下面提供的会话记录（中间部分已省略，共 {omitted} 条）",
                )
            prompt = f"""{context}

{header_omitted}
{transcript}

请按 JSON 格式返回归因结果。"""
            # 头/尾保留行数：标记行前的为头部，其后为尾部。
            marker_idx = next(
                i for i, line in enumerate(fitted_lines) if "中间省略" in line
            )
            kept_head = marker_idx
            kept_tail = len(fitted_lines) - marker_idx - 1
            self.logger.info(
                "Session transcript truncated to fit token budget",
                extra={
                    "event": "offline.transcript_truncated",
                    "session_id": session_id,
                    "details": {
                        "original_lines": len(lines),
                        "kept_head": kept_head,
                        "kept_tail": kept_tail,
                        "omitted": omitted,
                        "budget": budget,
                    },
                },
            )
        else:
            prompt = f"""{context}

{header_plain}
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
        if len(parsed.deltas) > settings.offline_max_deltas_per_session:
            dropped = parsed.deltas[settings.offline_max_deltas_per_session :]
            self.logger.warning(
                "Session deltas truncated to max per session",
                extra={
                    "event": "offline.deltas_truncated",
                    "session_id": session_id,
                    "details": {
                        "kept": settings.offline_max_deltas_per_session,
                        "dropped": len(dropped),
                        "dropped_refs": [
                            f"dim:{d.dimension_id}"
                            if d.dimension_id is not None
                            else f"new:{(d.new_dimension_description or '')[:40]}"
                            for d in dropped
                        ],
                    },
                },
            )
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

    def _apply(
        self, result: AttributionResult, session_id: str, logs: list[DialogueLog]
    ) -> tuple[int, list[PersonalityEvent], list[int]]:
        # 不再提供 fallback target：无有效 target 时证据/事件不写向量库，
        # 避免 doc id 哈希原料与 source_id 被 "None" 污染（改进 54）。
        # Chroma 副作用只收集不执行，由 run() 在 PG 事务提交成功后统一 flush。
        sorted_logs = sorted(logs, key=lambda x: (x.created_at is None, x.created_at or datetime.min, x.id or 0))

        applied = 0
        pending_events: list[PersonalityEvent] = []
        pending_purge: list[int] = []
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
                    doc_id = evidence_doc_id(target_log_id, dim_id, snippet)
                    pending_events.append(
                        PersonalityEvent(
                            id=doc_id,
                            content=snippet,
                            context=context,
                            speaker="assistant",
                            dimension_ids=[dim_id],
                            source_type="evidence",
                            source_id=str(target_log_id),
                        )
                    )
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
                pending_events.append(
                    PersonalityEvent(
                        id=event_doc_id(target_log_id, dim_id, delta.event_summary),
                        content=delta.event_summary,
                        dimension_ids=[dim_id],
                        source_type="event",
                        source_id=str(target_log_id),
                    )
                )
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
            if dim and not dim.core:
                total = dim.success_count + dim.failure_count
                ratio = dim.failure_count / total if total > 0 else 0.0
                if (
                    total >= settings.failure_min_samples
                    and ratio > settings.failure_ratio_threshold
                ):
                    self.pg.deactivate_dimension(dim_id)
                    pending_purge.append(dim_id)
                    self.logger.info(
                        "Deactivated dimension due to failure threshold",
                        extra={
                            "event": "offline.dimension_deactivated",
                            "session_id": session_id,
                            "details": {
                                "dimension_id": dim_id,
                                "success_count": dim.success_count,
                                "failure_count": dim.failure_count,
                                "ratio": ratio,
                            },
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
        return applied, pending_events, pending_purge

    def _flush_post_commit_chroma(
        self,
        session_id: str,
        logs: list[DialogueLog],
        events: list[PersonalityEvent],
        purge_dimension_ids: list[int],
    ) -> None:
        for event in events:
            try:
                self.chroma.add_personality_event(event)
            except Exception as exc:
                self.logger.warning(
                    "Chroma personality event write failed after PG commit",
                    extra={
                        "event": "offline.chroma_write_failed",
                        "session_id": session_id,
                        "details": {"doc_id": event.id, "source_type": event.source_type, "error": str(exc)},
                    },
                )

        self._persist_session_memories(session_id, logs)

        for dim_id in purge_dimension_ids:
            try:
                self._purge_dimension_vectors(dim_id)
            except Exception as exc:
                self.logger.warning(
                    "Dimension vector purge failed after PG commit",
                    extra={
                        "event": "offline.dimension_purge_failed",
                        "session_id": session_id,
                        "details": {"dimension_id": dim_id, "error": str(exc)},
                    },
                )

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
        for idx, chunk in enumerate(chunks):
            try:
                self.chroma.add_memory(
                    session_id,
                    chunk.content,
                    memory_id=dialogue_chunk_memory_id(
                        session_id,
                        chunk.first_dialogue_log_id,
                        chunk.last_dialogue_log_id,
                        chunk.content,
                    ),
                    metadata=DialogueMemoryMetadata(
                        session_id=session_id,
                        chunk_index=chunk.chunk_index,
                        first_dialogue_log_id=chunk.first_dialogue_log_id,
                        last_dialogue_log_id=chunk.last_dialogue_log_id,
                        added_at=now,
                    ),
                )
            except Exception as exc:
                self.logger.warning(
                    "Session memory persist failed after PG commit",
                    extra={
                        "event": "offline.memory_persist_failed",
                        "session_id": session_id,
                        "details": {"chunk_index": idx, "error": str(exc)},
                    },
                )

    def _build_evidence_context(self, logs: list[DialogueLog], target_log_id: int) -> str:
        """Build a short transcript context around the target dialogue log."""
        sorted_logs = sorted(logs, key=lambda x: (x.created_at is None, x.created_at or datetime.min, x.id or 0))
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
        purge_dimension_vectors(self.pg, self.chroma, dimension_id)
