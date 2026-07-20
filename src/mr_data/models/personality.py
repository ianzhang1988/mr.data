from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class FixedIdentity(BaseModel):
    id: Optional[int] = None
    name: str
    role: str
    base_prompt: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PersonalityDimension(BaseModel):
    id: Optional[int] = None
    description: str
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
