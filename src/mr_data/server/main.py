"""uvicorn 启动入口（chat-http-api 计划 §7）：mr-data serve 内部调用。"""

import uvicorn

from mr_data.config import settings

from .app import create_app


def serve(host: str | None = None, port: int | None = None) -> None:
    """装配共享单例并启动 HTTP server；进程退出 = Runtime.stop()（lifespan）。"""
    app = create_app()
    uvicorn.run(
        app,
        host=host or settings.server_host,
        port=port or settings.server_port,
    )
