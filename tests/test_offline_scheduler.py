"""离线归因定时调度测试（改进 85）：cron 解析、双层竞争护栏、chat 内嵌生命周期。

- cron 串解析：next_fire 触发点计算与非法串拒绝；
- 进程内护栏：_RUN_LOCK 被占时 tick 跳过（offline.schedule_skipped_busy）；
- 跨进程护栏：PG advisory lock 被外部持有时 tick 跳过
  （offline.schedule_skipped_locked）；手动 offline 命令同锁互斥；
- 守护线程：start/stop 生命周期，stop 唤醒睡眠循环；
- chat 集成：启动拉起调度、退出停止；非法 cron 串降级不阻断 chat。
"""

import logging
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import pytest
from typer.testing import CliRunner

from mr_data.cli import app
from mr_data.db import PostgresStore
from mr_data.offline import (
    OFFLINE_ADVISORY_LOCK_KEY,
    OfflineScheduler,
    next_fire,
)
from mr_data.offline.scheduler import _RUN_LOCK

from conftest import capture_events, open_log_propagation

runner = CliRunner()

_NOW = datetime(2026, 11, 15, 10, 24, 30, tzinfo=timezone.utc)


class _FakeEngine:
    """最小 AttributionEngine 替身：只记录 run 调用，可配置抛异常。"""

    def __init__(self, dsn: str = "", fail: bool = False):
        self.pg = SimpleNamespace(dsn=dsn)
        self.run_calls = 0
        self.fail = fail

    def run(self):
        self.run_calls += 1
        if self.fail:
            raise RuntimeError("boom")


# ---------- cron 解析（纯函数，不依赖 PG） ----------


def test_next_fire_hourly():
    # 包装层契约：默认 cron 串能算出下一触发点（解析矩阵归 croniter 自测）
    assert next_fire("0 * * * *", _NOW) == datetime(
        2026, 11, 15, 11, 0, tzinfo=timezone.utc
    )


def test_invalid_cron_raises():
    # 契约：非法串归一为 ValueError（供启动时快速失败/降级）
    with pytest.raises(ValueError):
        next_fire("not-a-cron", _NOW)


# ---------- tick：进程内护栏 / advisory lock / 异常隔离 ----------


def test_tick_runs_engine_once(pg_available, reset_pg_state, temp_log_dir, caplog, monkeypatch):
    pg = PostgresStore()
    engine = _FakeEngine(dsn=pg.dsn)
    scheduler = OfflineScheduler(engine, cron_expr="0 * * * *")
    open_log_propagation(caplog, monkeypatch, "mr_data.offline")

    scheduler.tick()

    assert engine.run_calls == 1
    assert len(capture_events(caplog, "offline.schedule_completed")) == 1


def test_tick_skipped_when_inprocess_locked(
    pg_available, reset_pg_state, temp_log_dir, caplog, monkeypatch
):
    pg = PostgresStore()
    engine = _FakeEngine(dsn=pg.dsn)
    scheduler = OfflineScheduler(engine, cron_expr="0 * * * *")
    open_log_propagation(caplog, monkeypatch, "mr_data.offline")

    _RUN_LOCK.acquire()
    try:
        scheduler.tick()
    finally:
        _RUN_LOCK.release()

    assert engine.run_calls == 0
    assert len(capture_events(caplog, "offline.schedule_skipped_busy")) == 1


def _hold_advisory_lock(pg: PostgresStore):
    """独立连接持有离线归因 advisory lock（会话级，连接关闭即释放）。"""
    conn = psycopg.connect(pg.dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (OFFLINE_ADVISORY_LOCK_KEY,))
        assert cur.fetchone()[0], "前置条件：advisory lock 应能获得"
    return conn


def test_tick_skipped_when_advisory_locked(
    pg_available, reset_pg_state, temp_log_dir, caplog, monkeypatch
):
    pg = PostgresStore()
    blocker = _hold_advisory_lock(pg)
    try:
        engine = _FakeEngine(dsn=pg.dsn)
        scheduler = OfflineScheduler(engine, cron_expr="0 * * * *")
        open_log_propagation(caplog, monkeypatch, "mr_data.offline")

        scheduler.tick()

        assert engine.run_calls == 0
        assert len(capture_events(caplog, "offline.schedule_skipped_locked")) == 1
    finally:
        blocker.close()


def test_tick_failure_logged_not_raised(
    pg_available, reset_pg_state, temp_log_dir, caplog, monkeypatch
):
    pg = PostgresStore()
    engine = _FakeEngine(dsn=pg.dsn, fail=True)
    scheduler = OfflineScheduler(engine, cron_expr="0 * * * *")
    open_log_propagation(caplog, monkeypatch, "mr_data.offline")

    scheduler.tick()  # 不向上抛

    assert engine.run_calls == 1
    assert len(capture_events(caplog, "offline.schedule_run_failed")) == 1
    assert capture_events(caplog, "offline.schedule_completed") == []


# ---------- 守护线程生命周期 ----------


def test_scheduler_stop_wakes_sleeping_loop(
    pg_available, reset_pg_state, temp_log_dir, caplog, monkeypatch
):
    pg = PostgresStore()
    engine = _FakeEngine(dsn=pg.dsn)
    scheduler = OfflineScheduler(engine, cron_expr="0 * * * *")
    open_log_propagation(caplog, monkeypatch, "mr_data.offline")

    thread = scheduler.start()
    scheduler.stop()  # stop_event 应唤醒睡眠中的 wait 并限时 join

    assert not thread.is_alive()
    assert engine.run_calls == 0  # 只启动即停止，不应触发归因
    assert len(capture_events(caplog, "offline.schedule_started")) == 1


# ---------- CLI：手动 offline 与调度同锁互斥 ----------


def test_cli_offline_skips_when_locked(
    pg_available, reset_pg_state, temp_log_dir, monkeypatch
):
    pg = PostgresStore()
    blocker = _hold_advisory_lock(pg)
    created = []

    class FakeEngine:
        def __init__(self, pg_store=None):
            created.append(self)
            self.ran = False

        def run(self):
            self.ran = True

    monkeypatch.setattr("mr_data.cli.AttributionEngine", FakeEngine)
    try:
        result = runner.invoke(app, ["offline"])
    finally:
        blocker.close()

    assert result.exit_code == 1
    assert "跳过" in result.output
    assert created and not created[0].ran  # 持锁冲突：未执行归因


# ---------- CLI：chat 内嵌调度生命周期 ----------


class _FakeDialogueGraph:
    def __init__(self, **kwargs):
        # chat() 会从 graph 取 pg/chroma/llm 复用给调度引擎
        self.pg = SimpleNamespace(dsn="fake")
        self.chroma = SimpleNamespace()
        self.llm = SimpleNamespace()

    def chat(self, session_id: str, user_input: str):
        return SimpleNamespace(text="ok", blocks=[])


class _FakePostgresStore:
    def __init__(self, **kwargs):
        pass

    def init_schema(self) -> None:
        pass

    def seed(self) -> None:
        pass

    def create_session(self, session_id=None) -> str:
        return session_id or "sess-1"

    def close_session(self, session_id: str) -> None:
        pass


def _sequential_prompt(inputs: list[str]):
    it = iter(inputs)

    def _prompt(*args, **kwargs):
        return next(it)

    return _prompt


def _patch_chat_deps(monkeypatch, scheduler_cls):
    monkeypatch.setattr("mr_data.cli.OfflineScheduler", scheduler_cls)
    monkeypatch.setattr(
        "mr_data.cli.AttributionEngine",
        lambda **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr("mr_data.cli.DialogueGraph", _FakeDialogueGraph)
    monkeypatch.setattr("mr_data.cli.PostgresStore", _FakePostgresStore)
    monkeypatch.setattr("mr_data.cli.Prompt.ask", _sequential_prompt(["/exit", ""]))


def test_chat_starts_and_stops_scheduler(monkeypatch, caplog, temp_log_dir):
    calls = {"started": 0, "stopped": 0}

    class FakeScheduler:
        def __init__(self, engine):
            self.cron_expr = "0 * * * *"

        def validate(self):
            pass

        def start(self):
            calls["started"] += 1
            return threading.Thread()

        def stop(self):
            calls["stopped"] += 1

        def next_fire(self):
            return datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc)

    _patch_chat_deps(monkeypatch, FakeScheduler)

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert calls == {"started": 1, "stopped": 1}
    assert "离线归因定时任务已启动" in result.output


def test_chat_invalid_cron_degrades_gracefully(monkeypatch, caplog, temp_log_dir):
    class FakeScheduler:
        def __init__(self, engine):
            pass

        def validate(self):
            raise ValueError("非法 cron 串: 'bad-cron'")

        def start(self):  # pragma: no cover - 不应被调用
            raise AssertionError("invalid cron 不应 start")

        def stop(self):  # pragma: no cover - 不应被调用
            raise AssertionError("invalid cron 不应 stop")

    _patch_chat_deps(monkeypatch, FakeScheduler)
    open_log_propagation(caplog, monkeypatch, "mr_data.cli")

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0  # 不阻断 chat
    assert "定时调度未启用" in result.output
    assert len(capture_events(caplog, "offline.schedule_invalid_cron")) == 1


# ---------- CLI：/newsession 异步触发离线归因（改进 86） ----------


def _patch_newsession_deps(monkeypatch, engine, dsn: str):
    """chat 依赖打桩：graph.pg.dsn 指向真 pg（供 advisory lock），
    AttributionEngine 返回记录型替身；输入 /newsession + 空评分 + /exit + 空评分。"""

    class FakeGraph:
        def __init__(self, **kwargs):
            self.pg = SimpleNamespace(dsn=dsn)
            self.chroma = SimpleNamespace()
            self.llm = SimpleNamespace()

        def chat(self, session_id: str, user_input: str):
            return SimpleNamespace(text="ok", blocks=[])

    class FakeScheduler:
        """只验接线，不拉真实调度线程。"""

        cron_expr = "0 * * * *"

        def __init__(self, engine):
            pass

        def validate(self):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def next_fire(self):
            return datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("mr_data.cli.OfflineScheduler", FakeScheduler)
    monkeypatch.setattr("mr_data.cli.AttributionEngine", lambda **kwargs: engine)
    monkeypatch.setattr("mr_data.cli.DialogueGraph", FakeGraph)
    monkeypatch.setattr("mr_data.cli.PostgresStore", _FakePostgresStore)
    monkeypatch.setattr(
        "mr_data.cli.Prompt.ask", _sequential_prompt(["/newsession", "", "/exit", ""])
    )


@pytest.mark.parametrize("hold_lock", [False, True])
def test_newsession_async_run_respects_lock(
    pg_available, reset_pg_state, temp_log_dir, monkeypatch, hold_lock
):
    """改进 86：/newsession 关闭会话后后台执行一轮异步归因；
    advisory lock 被持有时跳过（不阻断 chat，新会话照常开始）。"""
    pg = PostgresStore()
    blocker = _hold_advisory_lock(pg) if hold_lock else None
    engine = _FakeEngine(dsn=pg.dsn)
    _patch_newsession_deps(monkeypatch, engine, pg.dsn)
    try:
        result = runner.invoke(app, ["chat"])
    finally:
        if blocker is not None:
            blocker.close()

    assert result.exit_code == 0
    assert engine.run_calls == (0 if hold_lock else 1)
    assert "New session: sess-1" in result.output  # 新会话照常开始
    if hold_lock:
        assert "跳过" in result.output
    else:
        assert "离线归因在后台完成" in result.output
