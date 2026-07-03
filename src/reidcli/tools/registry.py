"""Tool registry: registers tools, lists definitions, dispatches calls.

Policy gating is owned by each tool (via ctx.policy.check_path / check_command /
evaluate) so action-specific checks happen in one place. The registry only routes
and converts exceptions to ToolResult failures.
"""
from __future__ import annotations

from typing import Any

from reidcli.diagnostics.logger import get_logger
from reidcli.tools.base import BaseTool, ToolContext, ToolDefinition, ToolResult

log = get_logger("reidcli.tools")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._schema_cache: list[dict[str, Any]] | None = None

    def register(self, tool: BaseTool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"tool '{name}' already registered")
        self._tools[name] = tool
        self._schema_cache = None
        log.debug("registered tool: %s", name)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [t.definition for t in self._tools.values()]

    def schemas(self) -> list[dict[str, Any]]:
        if self._schema_cache is None:
            self._schema_cache = [t.schema() for t in self._tools.values()]
        return self._schema_cache

    def dispatch(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.fail(f"unknown tool: {name}")
        try:
            return tool.execute(args, ctx)
        except Exception as exc:  # noqa: BLE001 - tools must not crash the runtime
            log.exception("tool %s raised", name)
            return ToolResult.fail(f"tool '{name}' crashed: {exc}")
