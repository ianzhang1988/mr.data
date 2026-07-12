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
    name: str
    current_value: float = Field(default=0.0, ge=-1.0, le=1.0)
    success_count: int = 0
    failure_count: int = 0
    failure_threshold: int = 5
    active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DialogueLog(BaseModel):
    id: Optional[int] = None
    session_id: str
    role: str  # 'user' | 'assistant'
    content: str
    evaluation_score: Optional[int] = None  # -1, 0, 1
    evaluation_feedback: Optional[str] = None
    processed_for_attribution: bool = False
    created_at: Optional[datetime] = None


class AdjustmentLog(BaseModel):
    id: Optional[int] = None
    dimension_name: str
    delta_value: float = 0.0
    delta_success: int = 0
    delta_failure: int = 0
    reason: str
    dialogue_log_id: Optional[int] = None
    created_at: Optional[datetime] = None


class PersonalityEvent(BaseModel):
    id: Optional[str] = None
    content: str
    dimension_tags: list[str] = Field(default_factory=list)
    source_type: str = "line"  # 'line' | 'event'
    source_id: Optional[str] = None
