"""chat 内嵌的离线归因定时调度（改进 85）。

线程模型：chat REPL 主线程阻塞等待用户输入；本模块在独立守护线程中按
配置 cron 串（offline_schedule_cron，5 段标准格式，UTC）定时执行
AttributionEngine.run()。归因执行不占主线程，两线程互不等待。

竞争状态保护（双层）：
- 进程内：模块级 _RUN_LOCK 非阻塞获取，包住「advisory lock + run」全程
  （调度线程自身顺序执行天然不自叠，此锁防御未来 /newsession 等
  同进程异步触发路径与本任务复用同一护栏）；
- 跨进程：PostgreSQL advisory lock（pg_try_advisory_lock），防守护线程
  与另一终端手动执行的 `mr-data offline` 并发归因（否则重复归因/
  死信计数错乱）。锁为会话级，由专用连接持有整个 run 期间，退出显式
  unlock + 关连接兜底释放。手动 offline 命令走同一把锁。
"""

import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

import psycopg
from croniter import croniter

from mr_data.config import settings
from mr_data.logging import get_logger

logger = get_logger("mr_data.offline")

# 离线归因全局互斥 advisory lock 键（PG advisory lock 为数据库级 bigint，
# 与表无关）：同一时刻只允许一个离线归因 run（跨进程）。
OFFLINE_ADVISORY_LOCK_KEY = 7788990011

# 进程内护栏：同一进程内同一时间只允许一个离线 run。
_RUN_LOCK = threading.Lock()


@contextmanager
def advisory_lock(dsn: str, key: int) -> Iterator[bool]:
    """会话级 PG advisory lock：获得则持有到退出，yield 是否获得。

    锁绑定专用连接生命周期：退出时显式 unlock，连接关闭为兜底释放。
    调用方负责根据返回值决定是否执行临界区并记录跳过日志。
    """
    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = bool(cur.fetchone()[0])
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
                conn.commit()
            except Exception:
                pass  # 连接关闭即释放，解锁失败不掩盖临界区异常
    finally:
        conn.close()


def next_fire(cron_expr: str, after: Optional[datetime] = None) -> datetime:
    """cron 串（5 段标准格式，UTC）在 after 之后的下一次触发时间。

    非法表达式统一抛 ValueError（启动时快速失败/降级用）。
    """
    base = after or datetime.now(timezone.utc)
    try:
        return croniter(cron_expr, base).get_next(datetime)
    except Exception as exc:
        raise ValueError(f"非法 cron 串: {cron_expr!r}") from exc


class OfflineScheduler:
    """按 cron 串在守护线程中定时执行离线归因的调度器。

    tick() 供定时循环与测试直接调用；start()/stop() 管理守护线程
    （daemon=True：进程退出不悬挂；stop() 唤醒睡眠中的循环并限时 join）。
    """

    def __init__(
        self,
        engine,
        cron_expr: Optional[str] = None,
        dsn: Optional[str] = None,
        stop_event: Optional[threading.Event] = None,
    ):
        self._engine = engine
        self._cron_expr = (
            cron_expr if cron_expr is not None else settings.offline_schedule_cron
        )
        self._dsn = dsn if dsn is not None else engine.pg.dsn
        self._stop_event = stop_event or threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.logger = get_logger("mr_data.offline")

    @property
    def cron_expr(self) -> str:
        return self._cron_expr

    def validate(self) -> None:
        """校验 cron 串合法性，非法抛 ValueError。"""
        next_fire(self._cron_expr, datetime.now(timezone.utc))

    def tick(self) -> None:
        """执行一轮归因（双层互斥护栏内）；所有失败路径只记日志不抛出。"""
        if not _RUN_LOCK.acquire(blocking=False):
            self.logger.info(
                "Offline run already in progress in this process, skipping tick",
                extra={"event": "offline.schedule_skipped_busy"},
            )
            return
        try:
            with advisory_lock(self._dsn, OFFLINE_ADVISORY_LOCK_KEY) as acquired:
                if not acquired:
                    self.logger.info(
                        "Another offline attribution is running (manual offline "
                        "or another scheduler), skipping tick",
                        extra={"event": "offline.schedule_skipped_locked"},
                    )
                    return
                try:
                    self._engine.run()
                except Exception as exc:
                    self.logger.warning(
                        "Scheduled offline run failed",
                        extra={
                            "event": "offline.schedule_run_failed",
                            "details": {"error": repr(exc)},
                        },
                    )
                    return
            self.logger.info(
                "Scheduled offline run completed",
                extra={"event": "offline.schedule_completed"},
            )
        finally:
            _RUN_LOCK.release()

    def _run_loop(self) -> None:
        try:
            nxt = next_fire(self._cron_expr, datetime.now(timezone.utc))
        except ValueError as exc:
            self.logger.warning(
                "Invalid offline schedule cron, scheduler not started",
                extra={
                    "event": "offline.schedule_invalid_cron",
                    "details": {"cron": self._cron_expr, "error": str(exc)},
                },
            )
            return
        self.logger.info(
            "Offline attribution scheduler started",
            extra={
                "event": "offline.schedule_started",
                "details": {
                    "cron": self._cron_expr,
                    "next_fire": nxt.isoformat(),
                },
            },
        )
        while not self._stop_event.is_set():
            wait_s = max(0.0, (nxt - datetime.now(timezone.utc)).total_seconds())
            # stop_event 提前唤醒 = 退出；睡到点 = 触发一轮
            if self._stop_event.wait(wait_s):
                break
            self.tick()
            try:
                nxt = next_fire(self._cron_expr, datetime.now(timezone.utc))
            except ValueError:  # 运行期改坏配置：记日志退出循环，不影响 chat
                self.logger.warning(
                    "Offline schedule cron became invalid, scheduler stopped",
                    extra={
                        "event": "offline.schedule_invalid_cron",
                        "details": {"cron": self._cron_expr},
                    },
                )
                break

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self._run_loop,
            name="mr-data-offline-scheduler",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
