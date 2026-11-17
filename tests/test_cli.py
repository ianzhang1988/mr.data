from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mr_data.cli import app
from mr_data.config import settings

runner = CliRunner()


class _FakeChatResult:
    def __init__(self, text: str = "ok"):
        self.text = text
        self.blocks = []


def _sequential_prompt(inputs: list[str]):
    it = iter(inputs)

    def _prompt(*args, **kwargs):
        return next(it)

    return _prompt


class _FakeDialogueGraph:
    def __init__(self, **kwargs):
        # 坑 #6：假 graph 必须携带 pg/chroma/llm 属性，否则引擎回退构造
        # 真实 store（ChromaStore 加载 embedding 模型，分钟级开销）；pg 需有
        # dsn——OfflineScheduler 构造会读 engine.pg.dsn，缺了仍踩降级分支。
        self.pg = SimpleNamespace(dsn="fake-dsn")
        self.chroma = SimpleNamespace()
        self.llm = SimpleNamespace()

    def chat(self, session_id: str, user_input: str):
        return _FakeChatResult(f"reply-to-{user_input}")


class _FakePostgresStore:
    def __init__(self, **kwargs):
        pass

    def init_schema(self) -> None:
        pass

    def seed(self) -> None:
        pass

    def create_session(self, session_id: str | None = None) -> str:
        return session_id or "sess-1"

    def close_session(self, session_id: str) -> None:
        pass

    def update_session_rating(self, session_id: str, score: int, comment: str | None) -> None:
        pass


def test_chat_turn_exception_continues_and_closes_session(monkeypatch):
    chat_calls = []
    fail_once = {"done": False}

    class FakeGraph(_FakeDialogueGraph):
        def chat(self, session_id: str, user_input: str):
            chat_calls.append(user_input)
            if not fail_once["done"]:
                fail_once["done"] = True
                raise RuntimeError("boom")
            return _FakeChatResult(f"reply-to-{user_input}")

    closed = []

    class FakePG(_FakePostgresStore):
        def close_session(self, session_id: str) -> None:
            closed.append(session_id)

    monkeypatch.setattr("mr_data.runtime.DialogueGraph", FakeGraph)
    monkeypatch.setattr("mr_data.runtime.PostgresStore", FakePG)
    monkeypatch.setattr(
        "mr_data.cli.Prompt.ask",
        _sequential_prompt(["hi", "again", "/exit", ""]),
    )

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert chat_calls == ["hi", "again"]
    assert "本轮对话出错" in result.output
    assert closed == ["sess-1"]


def test_chat_rating_failure_still_closes_session(monkeypatch):
    closed = []

    class FakePG(_FakePostgresStore):
        def close_session(self, session_id: str) -> None:
            closed.append(session_id)

        def update_session_rating(self, session_id: str, score: int, comment: str | None) -> None:
            raise RuntimeError("db down")

    monkeypatch.setattr("mr_data.runtime.DialogueGraph", _FakeDialogueGraph)
    monkeypatch.setattr("mr_data.runtime.PostgresStore", FakePG)
    monkeypatch.setattr(
        "mr_data.cli.Prompt.ask",
        _sequential_prompt(["hi", "/exit", "1", "nice"]),
    )

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert closed == ["sess-1"]


def test_serve_command_defaults_and_overrides(monkeypatch):
    """serve 子命令：默认取 settings，--host/--port 可覆盖；装配走 create_app。"""
    calls = {}

    class FakeApp:
        pass

    def fake_create_app(**kwargs):
        calls["create_app"] = kwargs
        return FakeApp()

    def fake_run(app, **kwargs):
        calls["run"] = {"app": app, **kwargs}

    monkeypatch.setattr("mr_data.server.main.create_app", fake_create_app)
    monkeypatch.setattr("mr_data.server.main.uvicorn.run", fake_run)

    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0
    assert calls["run"]["host"] == settings.server_host
    assert calls["run"]["port"] == settings.server_port

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "9999"])
    assert result.exit_code == 0
    assert calls["run"]["host"] == "0.0.0.0"
    assert calls["run"]["port"] == 9999
