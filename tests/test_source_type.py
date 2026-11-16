"""source_type 值域校验：Literal 别名是素材来源类型的唯一事实来源。"""

import pytest
from pydantic import ValidationError

from mr_data.models import DialogueVectorRef, PersonalityEvent

# DialogueVectorRef 五值含 dialogue（改进 88 在线管道登记 memory_docs），
# PersonalityEvent 三值不含 web/dialogue（web 属于 memories 集合）。
_DIALOGUE_VECTOR_REF_TYPES = ("line", "event", "evidence", "web", "dialogue")
_PERSONALITY_EVENT_TYPES = ("line", "event", "evidence")


def _build(model, source_type: str):
    if model is PersonalityEvent:
        return PersonalityEvent(content="x", source_type=source_type)
    return DialogueVectorRef(
        dialogue_log_id=1, vector_doc_id="d1", source_type=source_type, content="x",
    )


@pytest.mark.parametrize(
    "model,accepted,rejected",
    [
        pytest.param(
            PersonalityEvent,
            _PERSONALITY_EVENT_TYPES,
            # web 属于 memories 集合、dialogue 属于 dialogue 集合，均不写入 personality
            ("web", "dialogue"),
            id="personality_event",
        ),
        pytest.param(
            DialogueVectorRef,
            _DIALOGUE_VECTOR_REF_TYPES,
            # 五值之外的来源类型仍被拒绝（如集合名误用）
            ("personality",),
            id="dialogue_vector_ref",
        ),
    ],
)
def test_source_type_values(model, accepted, rejected):
    for source_type in accepted:
        assert _build(model, source_type).source_type == source_type
    for source_type in rejected:
        with pytest.raises(ValidationError):
            _build(model, source_type)
