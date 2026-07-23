from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ThinkDecision(BaseModel):
    """LLM 在 think 节点中做出的结构化决策。"""

    inner_monologue: str = Field(
        description="用一句话描述你当前对用户意图的理解以及你打算如何回应（内心独白）"
    )
    personality_query: str = Field(
        description="根据用户输入，生成一个用于检索性格向量库的简短语义查询（例如概念、理论、高层次抽象）"
    )
    memory_query: str = Field(
        description="生成一个用于检索记忆向量库的简短语义查询，记忆向量库中有过往对话、其他方式获取的信息（如文档、网页等）"
    )
    needs_web_search: bool = Field(
        description="是否需要联网搜索来获取最新信息或事实验证"
    )
    search_query: Optional[str] = Field(
        default=None,
        description="如需要搜索，提炼出的精准搜索关键词；否则留空",
    )


class DimensionSelection(BaseModel):
    """性格维度选择结果。"""

    dimension_ids: list[int] = Field(
        default_factory=list,
        description="从可选维度中选出最应当起作用的一个或多个维度 ID",
    )


class WebRelevanceItem(BaseModel):
    """单条网络资料的相关性判断。"""

    index: int = Field(description="文档在输入列表中的序号（从 0 开始）")
    is_relevant: bool = Field(description="该资料是否与用户输入相关")


class WebRelevanceFilterResult(BaseModel):
    """批量网络资料相关性过滤结果。"""

    results: list[WebRelevanceItem] = Field(
        default_factory=list,
        description="每条网络资料的相关性判断结果",
    )


class MemoryRelevanceItem(BaseModel):
    """单条记忆的相关性判断。"""

    index: int = Field(description="文档在输入列表中的序号（从 0 开始）")
    is_relevant: bool = Field(description="该记忆是否与用户输入相关")


class MemoryRelevanceFilterResult(BaseModel):
    """批量记忆相关性过滤结果。"""

    results: list[MemoryRelevanceItem] = Field(
        default_factory=list,
        description="每条记忆的相关性判断结果",
    )


class FixedIdentity(BaseModel):
    id: Optional[int] = None
    name: str
    role: str
    base_prompt: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UserIdentity(BaseModel):
    id: Optional[int] = None
    name: str
    role: str
    description: str
    is_default: bool = False
    is_protected: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PersonalityDimension(BaseModel):
    id: Optional[int] = None
    description: str
    core: bool = False
    success_count: int = 0
    failure_count: int = 0
    active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Session(BaseModel):
    id: str
    status: str = "active"  # 'active' | 'closed'
    created_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


class DialogueLog(BaseModel):
    id: Optional[int] = None
    session_id: str
    role: str  # 'user' | 'assistant'
    content: str
    evaluation_score: Optional[int] = None  # -1, 0, 1
    evaluation_feedback: Optional[str] = None
    processed_for_attribution: bool = False
    created_at: Optional[datetime] = None


class DialogueDimensionRef(BaseModel):
    id: Optional[int] = None
    dialogue_log_id: int
    dimension_id: int
    created_at: Optional[datetime] = None


class DialogueVectorRef(BaseModel):
    id: Optional[int] = None
    dialogue_log_id: int
    vector_doc_id: str
    source_type: str  # 'line' | 'event' | 'web'
    content: str
    dimension_ids: list[int] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class AdjustmentLog(BaseModel):
    id: Optional[int] = None
    dimension_id: int
    session_id: Optional[str] = None
    delta_success: int = 0
    delta_failure: int = 0
    reason: str
    dialogue_log_id: Optional[int] = None
    created_at: Optional[datetime] = None


class PersonalityEvent(BaseModel):
    id: Optional[str] = None
    content: str  # agent 台词 / utterance（注入 prompt 时使用）
    context: Optional[str] = None  # 前置场景 / 多轮上下文（仅用于向量 embedding）
    speaker: Optional[str] = None  # 说话者标识，默认 assistant
    dimension_ids: list[int] = Field(default_factory=list)
    source_type: str = "line"  # 'line' | 'event' | 'evidence' | 'web'
    source_id: Optional[str] = None


class PersonalitySampleLine(BaseModel):
    content: str
    context: Optional[str] = None
    speaker: str = "assistant"
    dimension_descriptions: list[str] = Field(default_factory=list)


class PersonalityPack(BaseModel):
    identity: FixedIdentity
    dimensions: list[PersonalityDimension]
    sample_lines: list[PersonalitySampleLine] = Field(default_factory=list)
