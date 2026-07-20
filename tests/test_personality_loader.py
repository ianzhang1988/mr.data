import json

import pytest

from mr_data.config import settings
from mr_data.db.personality_loader import (
    DEFAULT_IDENTITY,
    DEFAULT_PERSONALITY_PACK,
    load_personality_pack,
)
from mr_data.models import PersonalityPack


def test_default_pack_is_data():
    assert DEFAULT_IDENTITY.name == "Data"
    assert "企业号" in DEFAULT_IDENTITY.role
    assert len(DEFAULT_PERSONALITY_PACK.dimensions) > 0
    assert any(dim.core for dim in DEFAULT_PERSONALITY_PACK.dimensions)


def test_load_personality_pack_from_file(tmp_path, monkeypatch):
    custom = {
        "identity": {
            "name": "TestBot",
            "role": "测试角色",
            "base_prompt": "你是测试机器人。",
        },
        "dimensions": [
            {"description": "我喜欢测试。", "core": True},
            {"description": "我讨厌 bug。", "core": False},
        ],
        "sample_lines": [
            {
                "content": "正在运行测试。",
                "context": "用户要求测试时。",
                "speaker": "TestBot",
                "dimension_descriptions": ["我喜欢测试。"],
            }
        ],
    }
    file_path = tmp_path / "test_personality.json"
    file_path.write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(settings, "personality_file", str(file_path))
    pack = load_personality_pack()
    assert pack.identity.name == "TestBot"
    assert pack.dimensions[0].description == "我喜欢测试。"
    assert pack.dimensions[0].core is True
    assert len(pack.sample_lines) == 1


def test_load_personality_pack_fallback_on_missing_file(tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(settings, "personality_file", str(missing))
    pack = load_personality_pack()
    assert pack.identity.name == "Data"


def test_load_personality_pack_fallback_on_invalid_json(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(settings, "personality_file", str(bad))
    pack = load_personality_pack()
    assert pack.identity.name == "Data"


def test_personality_pack_validation_requires_identity():
    with pytest.raises(ValueError):
        PersonalityPack.model_validate({"dimensions": []})
