"""Chat HTTP 交互 API（chat-http-api 计划）：REST 驱动完整对话生命周期。

端点契约见 doc/chat-http-api-plan.md §3（错误码全集、请求/响应形状）。
server 与 TUI 同为薄壳：对话收敛到 DialogueGraph.chat()，装配收敛到
runtime.build_runtime()。仅绑定回环的本地工具，v1 不做鉴权（§1）。
"""

from .app import create_app

__all__ = ["create_app"]
