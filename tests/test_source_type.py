"""source_type 值域校验：Literal 别名是素材来源类型的唯一事实来源。"""

import pytest
from pydantic import ValidationError

from mr_data.models import DialogueVectorRef, PersonalityEvent


def test_personality_event_rejects_memory_source_type():
    # web 属于 memories 集合，不应写入 personality 集合
    with pytest.raises(ValidationError):
        PersonalityEvent(content="x", source_type="web")
    with pytest.raises(ValidationError):
        PersonalityEvent(content="x", source_type="dialogue")


def test_personality_event_accepts_personality_source_types():
    for source_type in ("line", "event", "evidence"):
        event = PersonalityEvent(content="x", source_type=source_type)
        assert event.source_type == source_type


def test_dialogue_vector_ref_rejects_dialogue_source_type():
    # dialogue 分块记忆不回写 dialogue_vector_refs
    with pytest.raises(ValidationError):
        DialogueVectorRef(
            dialogue_log_id=1, vector_doc_id="d1",
            source_type="dialogue", content="x",
        )
    with pytest.raises(ValidationError):
        DialogueVectorRef(
            dialogue_log_id=1, vector_doc_id="d1",
            source_type="personality", content="x",
        )


def test_dialogue_vector_ref_accepts_ref_source_types():
    for source_type in ("line", "event", "evidence", "web"):
        ref = DialogueVectorRef(
            dialogue_log_id=1, vector_doc_id="d1",
            source_type=source_type, content="x",
        )
        assert ref.source_type == source_type
