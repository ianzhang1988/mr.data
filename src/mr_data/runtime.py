"""通用运行环境装配层（chat-http-api 计划 v1.4/§8）。

graph.py 是通用对话引擎，本模块是通用运行环境装配：TUI（cli.chat）与
HTTP server 共用同一个 build_runtime() 入口，杜绝装配序列双写漂移。
本模块不承担任何交互职责；启动告警文案经 Runtime.startup_warnings 接驳
（TUI 逐字 rprint / server 记日志，§8.3 裁决）。
"""

from dataclasses import dataclass, field
from typing import Optional

from mr_data.config import settings
from mr_data.db import PostgresStore
from mr_data.logging import get_logger
from mr_data.offline import AttributionEngine, OfflineScheduler
from mr_data.online import DialogueGraph

logger = get_logger("mr_data.runtime")


@dataclass
class Runtime:
    """装配产物 + start/stop 生命周期（对应 TUI 的进入 try 前与 finally）。

    startup_warnings：装配期降级告警文案（cron 非法 / 调度器初始化失败），
    消费方式由调用方决定（TUI rprint / server 记结构化日志）。
    """

    graph: DialogueGraph
    pg: PostgresStore
    offline_engine: Optional[AttributionEngine]
    scheduler: Optional[OfflineScheduler]
    startup_warnings: list[str] = field(default_factory=list)

    def start(self) -> None:
        if self.scheduler is not None:
            self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler is not None:
            self.scheduler.stop()


def build_runtime(enable_web_search: Optional[bool] = None) -> Runtime:
    """装配运行环境（语义以 chat-http-api 计划 §8.1 第 1 条为准绳）。

    序列：DialogueGraph(enable_web_search=<flag>) → PostgresStore() +
    init_schema() + seed() → AttributionEngine（复用 graph 的 store/llm，
    避免重复加载 embedding 模型）→ OfflineScheduler.validate()。
    调度器降级语义：cron 非法或初始化失败 → scheduler=None，只告警不阻断。
    """
    graph = DialogueGraph(enable_web_search=enable_web_search)
    pg = PostgresStore()
    pg.init_schema()
    pg.seed()

    offline_engine: Optional[AttributionEngine] = None
    scheduler: Optional[OfflineScheduler] = None
    startup_warnings: list[str] = []
    try:
        offline_engine = AttributionEngine(
            pg_store=graph.pg, chroma_store=graph.chroma, llm=graph.llm
        )
    except Exception as exc:
        logger.warning(
            "Failed to construct offline attribution engine",
            extra={
                "event": "offline.engine_init_failed",
                "details": {"error": repr(exc)},
            },
        )
        offline_engine = None
        scheduler = None
    if offline_engine is not None:
        try:
            scheduler = OfflineScheduler(offline_engine)
            scheduler.validate()
        except ValueError as exc:
            logger.warning(
                "Invalid offline schedule cron, scheduler disabled",
                extra={
                    "event": "offline.schedule_invalid_cron",
                    "details": {"error": str(exc)},
                },
            )
            startup_warnings.append(
                f"[yellow]离线归因定时调度的 cron 串非法（{settings.offline_schedule_cron!r}），"
                "定时调度未启用。[/yellow]"
            )
            scheduler = None
        except Exception as exc:
            # 注：此类故障今日 TUI 只记日志、不 rprint；为守 §8.1「TUI 可见行为
            # 逐字不变」，此处维持原样——server 侧可经 offline.schedule_init_failed
            # 结构化日志事件拿到同一信息，不进 startup_warnings。
            logger.warning(
                "Failed to initialize offline scheduler, scheduler disabled",
                extra={
                    "event": "offline.schedule_init_failed",
                    "details": {"error": repr(exc)},
                },
            )
            scheduler = None
    return Runtime(
        graph=graph,
        pg=pg,
        offline_engine=offline_engine,
        scheduler=scheduler,
        startup_warnings=startup_warnings,
    )
