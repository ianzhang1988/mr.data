"""FastAPI 应用工厂与端点（chat-http-api 计划 §2/§3/§5）。

语义要点（详见计划文档，此处不重复论证）：
- 节点级降级 ≠ HTTP 失败：降级路径返回 200，由 events 字段承载断言依据（§5）。
- events/log_level 是 opt-in 观测字段（§3.5）：仅 include_events=true 时返回，
  语义为「本会话带 event 标记的日志行的镜像，按 logger 级别过滤」。
- 整轮失败 = 500 chat_turn_failed；会话管理失败区分 4xx/5xx。
- HTTP 无状态：会话归属由路径参数显式给出，server 侧不保留隐式会话游标。
- 全链路同步，FastAPI 同步端点自动落线程池（§6）。
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mr_data.db import PostgresStore
from mr_data.logging import get_logger
from mr_data.models.personality import Session
from mr_data.offline import AttributionEngine, guarded_offline_run
from mr_data.online import DialogueGraph
from mr_data.runtime import Runtime, build_runtime

logger = get_logger("mr_data.server")

_VALID_RATINGS = {-2, -1, 0, 1, 2}


def _error(code: str, message: str, status: int) -> JSONResponse:
    """统一错误形状：code 是稳定契约，message 仅供人读（§3.1）。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def _session_payload(session: Session) -> dict:
    """Session → §3.4 响应形状（时间字段 ISO 8601，created_at 恒存在）。"""
    return {
        "session_id": session.id,
        "status": session.status,
        "rating": session.rating,
        "rating_comment": session.rating_comment,
        "attribution_attempts": session.attribution_attempts,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "closed_at": session.closed_at.isoformat() if session.closed_at else None,
    }


class CreateSessionRequest(BaseModel):
    session_id: Optional[str] = None


class MessageRequest(BaseModel):
    content: Optional[str] = None


class RatingPayload(BaseModel):
    score: Optional[int] = None
    comment: Optional[str] = None


class CloseSessionRequest(BaseModel):
    rating: Optional[RatingPayload] = None


def create_app(
    graph: Optional[DialogueGraph] = None,
    pg: Optional[PostgresStore] = None,
    start_scheduler: Optional[bool] = None,
) -> FastAPI:
    """应用工厂（§2 决策 2）。依赖为 None 时按 TUI 相同默认值装配共享单例；
    非 None 时原样使用（测试经 TestClient 注入 FakeLLM/假 store，零真实基建）。

    start_scheduler：默认（None）仅在默认装配路径启动调度器（镜像 TUI）；
    注入依赖的测试路径默认不启动，避免守护线程噪音，可显式传 True 覆盖。
    """
    startup_warnings: list[str] = []
    if graph is None or pg is None:
        runtime: Optional[Runtime] = build_runtime()
        graph = graph or runtime.graph
        pg = pg or runtime.pg
        startup_warnings = runtime.startup_warnings
        if start_scheduler is None:
            start_scheduler = True
    else:
        runtime = None
        if start_scheduler is None:
            start_scheduler = False

    offline_engine: Optional[AttributionEngine] = None
    try:
        offline_engine = AttributionEngine(
            pg_store=graph.pg, chroma_store=graph.chroma, llm=graph.llm
        )
    except Exception as exc:  # 引擎不可用：/offline 返回 offline_failed
        logger.warning(
            "Offline engine unavailable, /offline will report failure",
            extra={
                "event": "offline.engine_init_failed",
                "details": {"error": repr(exc)},
            },
        )

    if start_scheduler and runtime is not None and runtime.scheduler is not None:
        runtime.start()

    for warning in startup_warnings:
        logger.info(
            "Runtime startup warning",
            extra={"event": "server.startup_warning", "details": {"warning": warning}},
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if runtime is not None:
            runtime.stop()

    app = FastAPI(title="mr.data chat API", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        """存活探针：仅表示进程存活，不做 PG/Chroma 连通检查（§3.8）。"""
        return {"status": "ok"}

    @app.post("/sessions", status_code=201)
    def create_session(body: Optional[CreateSessionRequest] = None) -> JSONResponse:
        requested = body.session_id if body else None
        if requested is not None:
            requested = requested.strip() or None
        if requested is not None and pg.get_session(requested) is not None:
            return _error(
                "session_already_exists",
                f"session {requested!r} already exists",
                409,
            )
        session_id = pg.create_session(requested)
        return JSONResponse(
            status_code=201,
            content={"session_id": session_id, "status": "active"},
        )

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> JSONResponse:
        session = pg.get_session(session_id)
        if session is None:
            return _error("session_not_found", f"session {session_id!r} not found", 404)
        return JSONResponse(content=_session_payload(session))

    @app.post("/sessions/{session_id}/message")
    def post_message(
        session_id: str,
        body: MessageRequest,
        include_events: bool = False,
    ) -> JSONResponse:
        content = body.content
        if content is None or not content.strip():
            return _error(
                "invalid_message", "content is required and must be non-empty", 400
            )
        session = pg.get_session(session_id)
        if session is None:
            return _error("session_not_found", f"session {session_id!r} not found", 404)
        if session.status != "active":
            return _error(
                "session_closed", f"session {session_id!r} is closed", 409
            )
        events: list[dict] = []
        try:
            result = graph.chat(
                session_id,
                content.strip(),
                on_event=events.append if include_events else None,
            )
        except Exception as exc:
            logger.warning(
                "Chat turn failed",
                extra={
                    "event": "server.chat_turn_failed",
                    "session_id": session_id,
                    "details": {"error": str(exc)},
                },
            )
            return _error("chat_turn_failed", str(exc), 500)
        payload: dict = {
            "session_id": session_id,
            "reply": result.text,
            "references": [
                ref.model_dump() for ref in result.references
            ],
        }
        if include_events:
            payload["events"] = events
            payload["log_level"] = logging.getLevelName(
                graph.logger.getEffectiveLevel()
            ).lower()
        return JSONResponse(content=payload)

    @app.post("/sessions/{session_id}/close")
    def close_session(session_id: str, body: Optional[CloseSessionRequest] = None) -> JSONResponse:
        session = pg.get_session(session_id)
        if session is None:
            return _error("session_not_found", f"session {session_id!r} not found", 404)
        if session.status != "active":
            return _error(
                "session_already_closed", f"session {session_id!r} is closed", 409
            )
        rating = body.rating if body else None
        if rating is not None:
            if rating.score not in _VALID_RATINGS:
                return _error(
                    "invalid_rating",
                    f"rating.score must be one of {sorted(_VALID_RATINGS)}",
                    400,
                )
            # 评分写入失败不执行关会话，驱动方可修正后重试（§3.6 执行序）
            try:
                pg.update_session_rating(session_id, rating.score, rating.comment)
            except Exception as exc:
                logger.warning(
                    "Failed to write session rating",
                    extra={
                        "event": "server.rating_failed",
                        "session_id": session_id,
                        "details": {"error": str(exc)},
                    },
                )
                return _error("rating_failed", str(exc), 500)
        try:
            pg.close_session(session_id)
        except Exception as exc:
            return _error("internal_error", str(exc), 500)
        return JSONResponse(content={"session_id": session_id, "status": "closed"})

    @app.post("/offline")
    def run_offline() -> JSONResponse:
        """同步触发一轮离线归因，等待完成后返回（§3.7；Q-6 裁决）。"""
        if offline_engine is None:
            return _error("offline_failed", "offline engine unavailable", 500)
        outcome = guarded_offline_run(offline_engine, graph.pg.dsn, source="http")
        if outcome == "completed":
            return JSONResponse(content={"status": "completed"})
        if outcome in ("busy", "locked"):
            return _error(
                "offline_busy", f"another offline run is in progress ({outcome})", 409
            )
        return _error("offline_failed", "offline attribution run failed", 500)

    return app
