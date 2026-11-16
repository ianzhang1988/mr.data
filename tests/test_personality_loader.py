import json

import pytest

from mr_data.config import settings
from mr_data.db.personality_loader import load_personality_pack
from mr_data.models import PersonalityPack


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


@pytest.mark.parametrize("write_bad_json", [False, True], ids=["missing_file", "invalid_json"])
def test_load_personality_pack_fallback(tmp_path, monkeypatch, write_bad_json):
    path = tmp_path / "bad.json"
    if write_bad_json:
        path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(settings, "personality_file", str(path))
    pack = load_personality_pack()
    assert pack.identity.name == "Data"


def test_personality_pack_validation_requires_identity():
    with pytest.raises(ValueError):
        PersonalityPack.model_validate({"dimensions": []})
