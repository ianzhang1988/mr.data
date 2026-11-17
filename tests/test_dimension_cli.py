"""改进 90 CLI/chat 入口：dimension Typer subgroup 与 chat /dimension 分派。"""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import mr_data.interactive.dimensions as dimmode
from mr_data.cli import app

runner = CliRunner()


@pytest.mark.usefixtures("reset_pg_state")
def test_dimension_list_oneshot(pg):
    result = runner.invoke(app, ["dimension", "list"])
    assert result.exit_code == 0
    dim = next(d for d in pg.list_dimensions())
    assert dim.description[:10] in result.output  # 具体维度行出现（按终端宽截断，取前缀）


@pytest.mark.usefixtures("reset_pg_state")
def test_dimension_edit_oneshot_force(pg):
    dim_id = pg.insert_dimension("待编辑描述")
    result = runner.invoke(
        app, ["dimension", "edit", str(dim_id), "--description", "改后描述", "--force"]
    )
    assert result.exit_code == 0
    assert "已更新" in result.output
    assert pg.get_dimension(dim_id).description == "改后描述"


@pytest.mark.usefixtures("reset_pg_state")
def test_dimension_edit_oneshot_rejects_core(pg):
    core = next(d for d in pg.list_dimensions() if d.core)
    result = runner.invoke(
        app, ["dimension", "edit", str(core.id), "--description", "篡改", "--force"]
    )
    assert result.exit_code == 1
    assert "read-only" in result.output
    assert pg.get_dimension(core.id).description != "篡改"


@pytest.mark.usefixtures("reset_pg_state")
def test_dimension_edit_oneshot_requires_description(pg):
    dim_id = pg.insert_dimension("保持我")
    result = runner.invoke(app, ["dimension", "edit", str(dim_id)])
    assert result.exit_code == 1
    assert "--description" in result.output
    assert pg.get_dimension(dim_id).description == "保持我"


def test_dimension_no_subcommand_enters_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "mr_data.cli._run_dimension_mode", lambda pg=None: calls.append(pg)
    )
    result = runner.invoke(app, ["dimension"])
    assert result.exit_code == 0
    assert len(calls) == 1

    # edit 无参数与无子命令同路径（_run_dimension_mode），不再单列用例


# --- chat 分派 ---

class _FakeChatResult:
    text = "ok"
    blocks = []


class _FakeGraph:
    def __init__(self, **kwargs):
        # 坑 #6：假 graph 必须携带 pg/chroma/llm 属性，否则引擎回退构造
        # 真实 store（ChromaStore 加载 embedding 模型，分钟级开销）；pg 需有
        # dsn——OfflineScheduler 构造会读 engine.pg.dsn，缺了仍踩降级分支。
        self.pg = SimpleNamespace(dsn="fake-dsn")
        self.chroma = SimpleNamespace()
        self.llm = SimpleNamespace()

    def chat(self, session_id, user_input):
        return _FakeChatResult()


class _FakePG:
    def __init__(self, **kwargs):
        pass

    def init_schema(self):
        pass

    def seed(self):
        pass

    def create_session(self, session_id=None):
        return session_id or "sess-1"

    def close_session(self, session_id):
        pass

    def update_session_rating(self, session_id, score, comment=None):
        pass

    def get_current_user_identity(self):
        return None


def _sequential_prompt(inputs):
    it = iter(inputs)

    def _prompt(*args, **kwargs):
        return next(it, "")

    return _prompt


def test_chat_dimension_dispatches_to_mode(monkeypatch):
    calls = []

    def fake_mode(store, *, chroma=None, input_fn=None, entry="chat", status_bar=True):
        calls.append((chroma, entry))

    monkeypatch.setattr("mr_data.runtime.DialogueGraph", _FakeGraph)
    monkeypatch.setattr("mr_data.runtime.PostgresStore", _FakePG)
    monkeypatch.setattr(dimmode, "run_dimension_mode", fake_mode)
    monkeypatch.setattr(
        "mr_data.cli.Prompt.ask", _sequential_prompt(["/dimension", "/exit", ""])
    )

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][1] == "chat"


def test_chat_help_mentions_dimension(monkeypatch):
    monkeypatch.setattr("mr_data.runtime.DialogueGraph", _FakeGraph)
    monkeypatch.setattr("mr_data.runtime.PostgresStore", _FakePG)
    monkeypatch.setattr(
        "mr_data.cli.Prompt.ask", _sequential_prompt(["/help", "/exit", ""])
    )

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert "/dimension" in result.output
