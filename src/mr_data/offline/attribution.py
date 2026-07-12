import json
from typing import Optional

from pydantic import BaseModel, Field

from mr_data.config import settings
from mr_data.db import PostgresStore, ChromaStore
from mr_data.llm import LLMClient
from mr_data.models import AdjustmentLog, DialogueLog, PersonalityEvent


class DimensionDelta(BaseModel):
    dimension_id: Optional[int] = Field(default=None, description="已有维度 ID；为空时按 description 新建")
    description: Optional[str] = Field(default=None, description="新建维度时的描述；与 dimension_id 二选一")
    delta_success: int = Field(default=0, ge=0, description="成功计数增加量")
    delta_failure: int = Field(default=0, ge=0, description="失败计数增加量")
    reason: str = Field(description="变化原因")
    event_summary: Optional[str] = Field(default=None, description="如果这条对话值得记录为人格事件，请写一句话摘要；否则留空")


class AttributionResult(BaseModel):
    deltas: list[DimensionDelta] = Field(default_factory=list)


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

    def run(self, lookback_days: Optional[int] = None, batch_size: Optional[int] = None) -> None:
        lookback = lookback_days or settings.offline_lookback_days
        limit = batch_size or settings.offline_batch_size

        logs = self.pg.get_recent_dialogues(
            unprocessed_only=True,
            lookback_days=lookback,
            limit=limit,
        )
        if not logs:
            print("No unprocessed dialogues to attribute.")
            return

        # Group assistant replies with preceding user message
        pairs = self._pair_dialogues(logs)
        for user_log, assistant_log in pairs:
            result = self._attribute_pair(user_log, assistant_log)
            self._apply(result, assistant_log.id)
            self.pg.mark_dialogue_processed(user_log.id)
            self.pg.mark_dialogue_processed(assistant_log.id)

        print(f"Processed {len(pairs)} dialogue pairs.")

    def _pair_dialogues(self, logs: list[DialogueLog]) -> list[tuple[DialogueLog, DialogueLog]]:
        sorted_logs = sorted(logs, key=lambda x: x.created_at or 0)
        pairs = []
        for i in range(len(sorted_logs) - 1):
            if sorted_logs[i].role == "user" and sorted_logs[i + 1].role == "assistant":
                pairs.append((sorted_logs[i], sorted_logs[i + 1]))
        return pairs

    def _attribute_pair(self, user_log: DialogueLog, assistant_log: DialogueLog) -> AttributionResult:
        system = """你是对话归因分析器。给定一段用户输入和助手回复，以及评估反馈：
1. 判断哪些性格维度促成了成功或失败。
2. 如果维度已存在，给出其 dimension_id（整数）；如果是新维度，给出 description（描述性自白）。
3. 给出每个维度的 delta_success、delta_failure 和原因。
4. 如果这条对话提炼出值得记住的人格事件（例如新的口头禅、态度转变、重要经验），请写 event_summary；否则留空。
"""
        prompt = f"""用户输入：{user_log.content}
助手回复：{assistant_log.content}
评估分数：{assistant_log.evaluation_score if assistant_log.evaluation_score is not None else '无'}
评估反馈：{assistant_log.evaluation_feedback if assistant_log.evaluation_feedback else '无'}

请按 JSON 格式返回：{{"deltas": [{{"dimension_id": 1, "description": null, "delta_success": 1, "delta_failure": 0, "reason": "...", "event_summary": "..."}}]}}
"""
        raw = self.llm.chat(system, prompt, temperature=0.2)
        try:
            data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
            return AttributionResult(**data)
        except Exception:
            return AttributionResult()

    def _apply(self, result: AttributionResult, dialogue_id: Optional[int]) -> None:
        for delta in result.deltas:
            dim_id = delta.dimension_id
            if dim_id is None and delta.description:
                dim_id = self.pg.insert_dimension(delta.description)

            if dim_id is None:
                continue

            self.pg.update_dimension(
                dim_id,
                delta_success=delta.delta_success,
                delta_failure=delta.delta_failure,
            )
            self.pg.insert_adjustment(
                AdjustmentLog(
                    dimension_id=dim_id,
                    delta_success=delta.delta_success,
                    delta_failure=delta.delta_failure,
                    reason=delta.reason,
                    dialogue_log_id=dialogue_id,
                )
            )

            # Check pruning
            dim = self.pg.get_dimension(dim_id)
            if dim and dim.failure_count >= settings.failure_threshold:
                self.pg.deactivate_dimension(dim_id)

            # Persist meaningful events to personality collection
            if delta.event_summary:
                event = PersonalityEvent(
                    content=delta.event_summary,
                    dimension_ids=[dim_id],
                    source_type="event",
                    source_id=str(dialogue_id),
                )
                self.chroma.add_personality_event(event)
