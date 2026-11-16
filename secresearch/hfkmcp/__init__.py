"""HFK MCP Server：把 SecResearch 与 ExprDiag 暴露成 Hermes 可调用的工具。"""

from .server import SERVER_NAME, SERVER_VERSION, handle, main

__all__ = ["SERVER_NAME", "SERVER_VERSION", "handle", "main"]
