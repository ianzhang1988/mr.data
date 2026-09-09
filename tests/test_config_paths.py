"""路径配置默认值锚定项目根的测试。"""

from pathlib import Path

import pytest

from mr_data.config import Settings, _find_project_root

PROJECT_ROOT = _find_project_root()

_PATH_ENV_VARS = [
    "MR_DATA_DATA_DIR",
    "MR_DATA_PGEMBED_DATA_DIR",
    "MR_DATA_CHROMA_PERSIST_DIR",
    "MR_DATA_PERSONALITY_FILE",
    "MR_DATA_LOG_DIR",
]


@pytest.fixture
def clean_path_env(monkeypatch):
    """清除路径相关环境变量，避免受运行环境影响。"""
    for var in _PATH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def make_settings():
    # _env_file=None 避免受本地 .env 干扰
    return Settings(_env_file=None)


def test_find_project_root_finds_repo_root():
    root = _find_project_root()
    assert (root / "pyproject.toml").is_file()
    assert root == Path(__file__).resolve().parents[1]


def test_defaults_anchor_to_project_root(clean_path_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 从任意 CWD 运行，默认值应不变
    s = make_settings()
    assert s.data_dir == str(PROJECT_ROOT / "data")
    assert s.pgembed_data_dir == str(PROJECT_ROOT / "data" / "pgembed")
    assert s.chroma_persist_dir == str(PROJECT_ROOT / "data" / "chroma")
    assert s.personality_file == str(PROJECT_ROOT / "data" / "personalities" / "data.json")
    assert s.log_dir == str(PROJECT_ROOT / "logs")


def test_data_dir_override_propagates(clean_path_env, tmp_path):
    custom = tmp_path / "custom_data"
    clean_path_env.setenv("MR_DATA_DATA_DIR", str(custom))
    s = make_settings()
    assert s.data_dir == str(custom)
    assert s.pgembed_data_dir == str(custom / "pgembed")
    assert s.chroma_persist_dir == str(custom / "chroma")
    assert s.personality_file == str(custom / "personalities" / "data.json")
    # log_dir 不跟随 data_dir，仍锚定项目根
    assert s.log_dir == str(PROJECT_ROOT / "logs")


def test_explicit_child_overrides_data_dir(clean_path_env, tmp_path):
    custom_data = tmp_path / "custom_data"
    explicit_chroma = tmp_path / "explicit_chroma"
    clean_path_env.setenv("MR_DATA_DATA_DIR", str(custom_data))
    clean_path_env.setenv("MR_DATA_CHROMA_PERSIST_DIR", str(explicit_chroma))
    s = make_settings()
    assert s.chroma_persist_dir == str(explicit_chroma)
    # 其余子项仍跟随 data_dir
    assert s.pgembed_data_dir == str(custom_data / "pgembed")
    assert s.personality_file == str(custom_data / "personalities" / "data.json")


def test_explicit_relative_value_preserved(clean_path_env):
    clean_path_env.setenv("MR_DATA_LOG_DIR", "./mylogs")
    s = make_settings()
    # 显式相对值原样保留（相对 CWD）
    assert s.log_dir == "./mylogs"
