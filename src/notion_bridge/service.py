"""The long-lived objects that make up one running bridge.

Grouping them behind a single object is what makes an always-on process
manageable: the account pool can be rebuilt without restarting the service, and
shutdown has one place to drain and release everything it owns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import metrics
from .accounts.pool import NotionAccountPool, PoolLimits, build_account_pool
from .diagnostics import log_event
from .notion.models import pin_explicit_model_selection, resolve_model
from .planner.runtime_tools import RuntimeToolClient
from .settings import Settings
from .state.conversation_segments import ConversationSegmentStore
from .state.turn_affinity import TurnAffinityStore

log = logging.getLogger("uvicorn.error.notion_bridge")

DRAIN_TIMEOUT_SECONDS = 30.0


class AccountReloadBusy(RuntimeError):
    """A reload was requested while Notion sessions are still serving turns."""

    def __init__(self, busy: int) -> None:
        super().__init__(
            f"{busy} Notion session(s) are still in use; retry once they finish"
        )
        self.busy = busy


class BridgeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = time.time()
        self.turn_affinities = TurnAffinityStore(
            ttl=settings.turn_affinity_ttl_seconds,
            maximum=settings.turn_affinity_max_entries,
        )
        self.conversation_segments = ConversationSegmentStore(
            settings.conversation_state_path,
            ttl=settings.conversation_ttl_seconds,
            maximum=settings.conversation_max_entries,
        )
        self.runtime_tools = RuntimeToolClient(
            settings.mcp_runtime_url,
            timeout_seconds=settings.runtime_tool_timeout_seconds,
        )
        self._pool: NotionAccountPool | None = None
        self._reload_guard = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        pin_explicit_model_selection()
        metrics.registry.set_gauge(
            metrics.UP_SINCE,
            self.started_at,
            help_text="Unix timestamp of the current bridge process start.",
        )
        self._pool = await asyncio.to_thread(self._build_pool)
        status = await self._pool.status()
        log_event(
            log,
            "account_pool_started",
            configured=status["configured"],
            available=status["available"],
            invalid=status["invalid"],
            duplicates=status["duplicates"],
            ignored_surplus=status["ignored_surplus"],
            maximum=status["maximum"],
        )
        if self.settings.publicly_bound:
            log_event(
                log,
                "bridge_bound_publicly",
                level=logging.WARNING,
                host=self.settings.host,
            )
        if not self.runtime_tools.configured:
            log_event(log, "coding_tools_unconfigured", level=logging.WARNING)

    def _build_pool(self) -> NotionAccountPool:
        return build_account_pool(
            self.settings.account_home,
            limits=PoolLimits.from_settings(self.settings),
            max_accounts=self.settings.max_accounts,
        )

    async def aclose(self) -> None:
        pool = self._pool
        if pool is not None:
            drained = await pool.drain(DRAIN_TIMEOUT_SECONDS)
            if not drained:
                log_event(
                    log,
                    "shutdown_drain_timeout",
                    level=logging.WARNING,
                    timeout_seconds=DRAIN_TIMEOUT_SECONDS,
                )
            await pool.aclose()
        self._pool = None
        await self.runtime_tools.aclose()

    # -- accounts ----------------------------------------------------------

    @property
    def pool(self) -> NotionAccountPool | None:
        return self._pool

    @property
    def has_accounts(self) -> bool:
        return self._pool is not None and self._pool.size > 0

    async def reload_accounts(self) -> dict[str, Any]:
        """Pick up added, removed or re-authenticated account files in place.

        Without this, adding a Notion session means restarting the service and
        dropping every conversation binding that lives in memory.
        """
        async with self._reload_guard:
            current = self._pool
            if current is not None:
                busy = await current.busy_count()
                if busy:
                    raise AccountReloadBusy(busy)
            replacement = await asyncio.to_thread(self._build_pool)
            self._pool = replacement
            if current is not None:
                await current.aclose()
            status = await replacement.status()
            log_event(
                log,
                "account_pool_reloaded",
                configured=status["configured"],
                available=status["available"],
                invalid=status["invalid"],
                duplicates=status["duplicates"],
            )
            return status

    # -- helpers -----------------------------------------------------------

    def resolve_model(self, model: str | None) -> str:
        return resolve_model(
            model,
            default_model=self.settings.default_model,
            forced_model=self.settings.forced_model,
        )

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    async def account_status(self) -> dict[str, Any]:
        if self._pool is None:
            return {
                "configured": 0,
                "busy": 0,
                "available": 0,
                "cooldown": 0,
                "disabled": 0,
                "discovered": 0,
                "invalid": 0,
                "invalid_accounts": [],
                "duplicates": 0,
                "ignored_surplus": 0,
                "maximum": self.settings.max_accounts,
                "global_retry_after": 0,
                "accounts": [],
            }
        return await self._pool.status()

    async def state_status(self) -> dict[str, Any]:
        turn_affinity = await self.turn_affinities.status()
        conversation = await self.conversation_segments.status()
        metrics.registry.set_gauge(
            metrics.TURN_AFFINITIES,
            turn_affinity["active"],
            help_text="Codex turns currently bound to a Notion account.",
        )
        metrics.registry.set_gauge(
            metrics.CONVERSATIONS,
            conversation["active"],
            help_text="Codex conversations bound to a Notion thread segment.",
        )
        return {"turn_affinity": turn_affinity, "conversation_segments": conversation}
