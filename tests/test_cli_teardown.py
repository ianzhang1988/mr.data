from typer.testing import CliRunner

from mr_data.cli import app
from mr_data.config import settings

runner = CliRunner()


def _fake_data_dirs(tmp_path, monkeypatch):
    """Point pgembed/chroma data dirs at temp dirs containing marker files."""
    pgembed_dir = tmp_path / "pgembed"
    chroma_dir = tmp_path / "chroma"
    for d in (pgembed_dir, chroma_dir):
        d.mkdir(parents=True)
        (d / "marker.txt").write_text("data")
    monkeypatch.setattr(settings, "pgembed_data_dir", str(pgembed_dir))
    monkeypatch.setattr(settings, "chroma_persist_dir", str(chroma_dir))
    return pgembed_dir, chroma_dir


def test_teardown_removes_data_dirs(tmp_path, monkeypatch):
    pgembed_dir, chroma_dir = _fake_data_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "postgres_dsn", "")

    result = runner.invoke(app, ["teardown"], input="y\n")

    assert result.exit_code == 0
    assert not pgembed_dir.exists()
    assert not chroma_dir.exists()


def test_teardown_refuses_with_external_dsn(tmp_path, monkeypatch):
    pgembed_dir, chroma_dir = _fake_data_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://user:pass@localhost/mrdata")

    result = runner.invoke(app, ["teardown"], input="y\n")

    assert result.exit_code == 1
    assert "teardown 仅支持 pgembed 嵌入式模式" in result.output
    assert pgembed_dir.exists()
    assert chroma_dir.exists()


def test_teardown_refuses_unsafe_empty_target(tmp_path, monkeypatch):
    """改进 100：删除目标为空串（Path("") = CWD）时拒绝执行，不能 rmtree CWD。"""
    _, chroma_dir = _fake_data_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "postgres_dsn", "")
    monkeypatch.setattr(settings, "pgembed_data_dir", "")

    result = runner.invoke(app, ["teardown", "--force"])

    assert result.exit_code == 1
    assert "teardown 拒绝执行" in result.output
    assert chroma_dir.exists()
