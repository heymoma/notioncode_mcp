"""Client for the local coding-tools MCP runtime.

The previous implementation re-read `runtime/.env` from disk, opened a fresh
connection pool and replayed the MCP handshake on every single tool call. This
keeps one pooled client for the process lifetime and resolves the endpoint once,
at startup, through `Settings`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .. import metrics
from ..diagnostics import exception_fields, log_event

log = logging.getLogger("uvicorn.error.notion_bridge")

_JSON_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}
LIST_TOOLS = "listTools"


class RuntimeToolsUnavailable(RuntimeError):
    """The coding-tools MCP endpoint is not configured for this deployment."""


def parse_mcp_payload(body: str) -> dict[str, Any]:
    """Decode a JSON-RPC reply that may arrive as plain JSON or as one SSE event."""
    for line in body.splitlines():
        if line.startswith("data: "):
            value = json.loads(line[6:])
            if isinstance(value, dict):
                return value
    value = json.loads(body)
    if not isinstance(value, dict):
        raise RuntimeError("MCP returned a non-object response")
    return value


class RuntimeToolClient:
    def __init__(
        self,
        endpoint: str | None,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers=_JSON_HEADERS,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one MCP tool and return its raw JSON result."""
        if not self.endpoint:
            raise RuntimeToolsUnavailable(
                "The coding-tools MCP runtime is not configured; "
                "set NOTION_MCP_RUNTIME_URL or MCP_PATH_SECRET"
            )
        http = self._http()
        listing = name == LIST_TOOLS
        try:
            init = await http.post(self.endpoint, json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "notion-bridge", "version": "2.0"},
                },
            })
            init.raise_for_status()
            await http.post(
                self.endpoint,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            response = await http.post(self.endpoint, json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list" if listing else "tools/call",
                "params": {} if listing else {"name": name, "arguments": arguments},
            })
            response.raise_for_status()
            payload = parse_mcp_payload(response.text)
        except Exception as error:
            metrics.registry.increment(
                metrics.TOOL_CALLS,
                help_text="Coding-runtime MCP tool calls, by tool and outcome.",
                labels={"tool": name, "outcome": "transport_error"},
            )
            log_event(
                log,
                "runtime_tool_transport_failed",
                level=logging.WARNING,
                tool=name,
                **exception_fields(error),
            )
            raise
        if "error" in payload:
            metrics.registry.increment(
                metrics.TOOL_CALLS,
                help_text="Coding-runtime MCP tool calls, by tool and outcome.",
                labels={"tool": name, "outcome": "error"},
            )
            raise RuntimeError(str(payload["error"]))
        metrics.registry.increment(
            metrics.TOOL_CALLS,
            help_text="Coding-runtime MCP tool calls, by tool and outcome.",
            labels={"tool": name, "outcome": "ok"},
        )
        return json.dumps(payload.get("result", {}), ensure_ascii=False)

    async def call_or_error_json(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool, encoding any failure as an MCP-shaped error result.

        The planner loop must always be able to report *something* back to the
        model; a transport failure is a tool result, not a dead conversation.
        """
        try:
            return await self.call(name, arguments)
        except Exception as error:
            return json.dumps({"isError": True, "error": str(error)}, ensure_ascii=False)
