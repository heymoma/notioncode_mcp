"""Shared fakes for the bridge test suite."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from notion_bridge.service import BridgeService
from notion_bridge.settings import Settings
from notion_bridge.state.conversation_segments import ConversationSegmentStore
from notion_bridge.state.turn_affinity import TurnAffinityStore


def build_settings(**overrides: str) -> Settings:
    """Settings that never touch the real account home or a real endpoint."""
    environment = {
        "NOTION_AGENT_HOME": tempfile.mkdtemp(prefix="notion-test-home-"),
        "CODE_ROOT": tempfile.mkdtemp(prefix="notion-test-code-"),
        "NOTION_MCP_RUNTIME_URL": "http://127.0.0.1:1/mcp/test-secret",
        "NOTION_METRICS_ENABLED": "1",
        **overrides,
    }
    return Settings.from_env(environment, project_root=Path(__file__).resolve().parents[2])


def build_service(pool: Any = None, **overrides: str) -> BridgeService:
    """A service wired to in-memory state and an optional fake account pool."""
    service = BridgeService(build_settings(**overrides))
    service.turn_affinities = TurnAffinityStore()
    service.conversation_segments = ConversationSegmentStore()
    # Tests supply their own pool instead of discovering credential files.
    service._pool = pool
    return service


def completion(
    text: str,
    thread_id: str,
    *,
    input_tokens: int = 10,
    output_tokens: int = 2,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        thread_id=thread_id,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeLease:
    """Minimal stand-in for `AccountLease` that records every Notion call."""

    def __init__(self, account_id: str, responder) -> None:
        self.account_id = account_id
        self._responder = responder

    async def __aenter__(self) -> FakeLease:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def run(self, operation, *, retry_operation=None):
        return await operation(self._responder(self.account_id))


class FakePool:
    """Account pool that hands out deterministic accounts and records requests."""

    def __init__(self, responder, account_ids: list[str] | None = None) -> None:
        self._responder = responder
        self._account_ids = account_ids or ["account-a"]
        self.size = len(self._account_ids)
        self.preferred: list[str | None] = []
        self.new_segments = 0

    def lease(self, preferred_account_id: str | None = None) -> FakeLease:
        self.preferred.append(preferred_account_id)
        if preferred_account_id:
            return FakeLease(preferred_account_id, self._responder)
        index = min(self.new_segments, len(self._account_ids) - 1)
        self.new_segments += 1
        return FakeLease(self._account_ids[index], self._responder)

    async def busy_count(self) -> int:
        return 0

    async def status(self) -> dict[str, Any]:
        return {
            "configured": self.size,
            "busy": 0,
            "available": self.size,
            "cooldown": 0,
            "disabled": 0,
            "discovered": self.size,
            "invalid": 0,
            "invalid_accounts": [],
            "duplicates": 0,
            "ignored_surplus": 0,
            "maximum": 10,
            "global_retry_after": 0,
            "accounts": [],
        }

    async def aclose(self) -> None:
        return None
