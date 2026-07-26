"""Notion session pool: balancing, affinity, failover and circuit breaking."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx
from notion_agent_cli.exceptions import (
    ErrorCode,
    NotionAgentError,
    retry_policy_for,
)
from notion_agent_cli.provider import NotionAgentClient

from .. import metrics
from ..diagnostics import exception_fields, log_event
from ..settings import MAX_SUPPORTED_ACCOUNTS, Settings

MAX_ACCOUNTS = MAX_SUPPORTED_ACCOUNTS
MAX_REASONING_EFFORT = "high"
log = logging.getLogger("uvicorn.error.notion_pool")

_LOCAL_ERROR_CODES = {
    ErrorCode.EMPTY_PROMPT,
    ErrorCode.INVALID_CALLBACK,
    ErrorCode.ACCOUNT_MISSING,
    ErrorCode.ACCOUNT_MALFORMED,
    ErrorCode.ACCOUNT_INVALID,
    ErrorCode.WORKSPACE_AMBIGUOUS,
    ErrorCode.WORKSPACE_EMPTY,
    ErrorCode.THREAD_STATE_MISSING,
    ErrorCode.THREAD_STATE_MALFORMED,
}

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PoolLimits:
    """Timing and failure policy, taken from settings or overridden in tests."""

    inference_timeout_seconds: float = 180.0
    transient_cooldown_seconds: int = 30
    denial_cooldown_seconds: int = 300
    circuit_failure_window_seconds: int = 30
    circuit_failure_threshold: int = 3
    reasoning_effort: str = MAX_REASONING_EFFORT

    @classmethod
    def from_settings(cls, settings: Settings) -> PoolLimits:
        return cls(
            inference_timeout_seconds=settings.inference_timeout_seconds,
            transient_cooldown_seconds=settings.transient_cooldown_seconds,
            denial_cooldown_seconds=settings.denial_cooldown_seconds,
            circuit_failure_window_seconds=settings.circuit_failure_window_seconds,
            circuit_failure_threshold=settings.circuit_failure_threshold,
            reasoning_effort=settings.reasoning_effort,
        )


def reasoning_effort_client(effort: str) -> type[NotionAgentClient]:
    """Build a client class that pins Notion's reasoning effort on every call."""

    class PinnedEffortNotionAgentClient(NotionAgentClient):
        def _prepare_call(self, *args: Any, **kwargs: Any):
            prep = super()._prepare_call(*args, **kwargs)
            transcript = prep.body.get("transcript")
            if not isinstance(transcript, list):
                raise RuntimeError("Notion inference request has no transcript")
            for item in transcript:
                if not isinstance(item, dict) or item.get("type") != "config":
                    continue
                config = item.get("value")
                if not isinstance(config, dict):
                    raise RuntimeError("Notion inference request has no config value")
                config["reasoningEffort"] = effort
                return prep
            raise RuntimeError("Notion inference request has no config block")

    return PinnedEffortNotionAgentClient


MaxEffortNotionAgentClient = reasoning_effort_client(MAX_REASONING_EFFORT)


class AccountPoolExhausted(RuntimeError):
    """Every configured Notion account failed for one operation."""


class AccountPoolCoolingDown(AccountPoolExhausted):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"All Notion accounts are cooling down; retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass(slots=True)
class _AccountSlot:
    number: int
    client: NotionAgentClient
    account_id: str = ""
    credential_mtime: float = 0
    busy: bool = False
    disabled: bool = False
    cooldown_until: float = 0
    last_assigned_at: float = 0
    assignments: int = 0
    successes: int = 0
    failures: int = 0
    last_error_code: str = ""


def is_failover_error(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPError):
        return True
    if isinstance(error, NotionAgentError):
        return error.code not in _LOCAL_ERROR_CODES
    return False


def discover_account_paths(account_home: Path) -> list[Path]:
    legacy = account_home / "notion_account.json"
    accounts_dir = account_home / "accounts"
    paths = [legacy] if legacy.is_file() else []
    if accounts_dir.is_dir():
        paths.extend(sorted(path for path in accounts_dir.glob("*.json") if path.is_file()))
    return paths


def build_account_pool(
    account_home: Path,
    *,
    limits: PoolLimits | None = None,
    max_accounts: int = MAX_ACCOUNTS,
) -> NotionAccountPool:
    """Load every discoverable account file into a ready-to-use pool.

    Malformed, duplicate and surplus account files are reported through the
    pool status instead of raising. An unattended service must still start and
    serve the accounts that are valid.
    """
    limits = limits or PoolLimits()
    client_class = reasoning_effort_client(limits.reasoning_effort)
    account_paths = discover_account_paths(account_home)
    clients: list[NotionAgentClient] = []
    account_ids: list[str] = []
    invalid_accounts = 0
    duplicate_accounts = 0
    skipped_accounts = 0
    token_fingerprints: set[str] = set()
    user_fingerprints: set[str] = set()
    invalid_details: list[dict[str, str]] = []

    for account_path in account_paths:
        if account_path == account_home / "notion_account.json":
            thread_state_dir = account_home / "threads"
        else:
            path_key = hashlib.sha256(str(account_path.resolve()).encode()).hexdigest()[:16]
            thread_state_dir = account_home / "account-threads" / path_key
        client = client_class(account_path, thread_state_dir=thread_state_dir)
        try:
            account = client.load_account()
            if not isinstance(account.token_v2, str) or not account.token_v2.strip():
                raise ValueError("token_v2 must be a non-empty string")
            if not isinstance(account.user_id, str) or not account.user_id.strip():
                raise ValueError("user_id must be a non-empty string")
            fingerprint = hashlib.sha256(account.token_v2.encode()).hexdigest()
            user_fingerprint = hashlib.sha256(account.user_id.encode()).hexdigest()
        except (NotionAgentError, OSError, TypeError, ValueError, AttributeError) as error:
            invalid_accounts += 1
            invalid_details.append({
                "file": account_path.name,
                "reason": (
                    error.code if isinstance(error, NotionAgentError)
                    else type(error).__name__
                ),
            })
            continue
        if fingerprint in token_fingerprints or user_fingerprint in user_fingerprints:
            duplicate_accounts += 1
            continue
        if len(clients) >= max_accounts:
            # Refusing to boot here would crash-loop the whole service for a
            # purely additive mistake, so the surplus is reported instead.
            skipped_accounts += 1
            continue
        token_fingerprints.add(fingerprint)
        user_fingerprints.add(user_fingerprint)
        clients.append(client)
        account_ids.append(fingerprint[:16])

    if skipped_accounts:
        log_event(
            log,
            "account_pool_surplus_ignored",
            level=logging.WARNING,
            maximum=max_accounts,
            ignored=skipped_accounts,
        )

    return NotionAccountPool(
        clients,
        account_ids=account_ids,
        state_path=account_home / "pool-state.json",
        discovered_accounts=len(account_paths),
        invalid_accounts=invalid_accounts,
        duplicate_accounts=duplicate_accounts,
        invalid_details=invalid_details,
        skipped_accounts=skipped_accounts,
        limits=limits,
        maximum=max_accounts,
    )


class NotionAccountPool:
    def __init__(
        self,
        clients: list[NotionAgentClient],
        *,
        account_ids: list[str] | None = None,
        state_path: Path | None = None,
        discovered_accounts: int | None = None,
        invalid_accounts: int = 0,
        duplicate_accounts: int = 0,
        invalid_details: list[dict[str, str]] | None = None,
        skipped_accounts: int = 0,
        limits: PoolLimits | None = None,
        maximum: int = MAX_ACCOUNTS,
    ) -> None:
        if len(clients) > maximum:
            raise ValueError(f"At most {maximum} Notion clients are supported")
        ids = account_ids or [f"account-{index + 1:02d}" for index in range(len(clients))]
        if len(ids) != len(clients):
            raise ValueError("account_ids must match the number of clients")
        self.limits = limits or PoolLimits()
        self.maximum = maximum
        self._slots = [
            _AccountSlot(
                number=index + 1,
                client=client,
                account_id=ids[index],
                credential_mtime=(
                    client.account_path.stat().st_mtime
                    if getattr(client, "account_path", None)
                    and client.account_path.exists()
                    else 0
                ),
            )
            for index, client in enumerate(clients)
        ]
        self._condition = asyncio.Condition()
        self._state_path = state_path
        self._global_cooldown_until = 0.0
        self._recent_failures: deque[tuple[float, str, str]] = deque()
        self.discovered_accounts = (
            len(clients) if discovered_accounts is None else discovered_accounts
        )
        self.invalid_accounts = invalid_accounts
        self.duplicate_accounts = duplicate_accounts
        self.invalid_details = invalid_details or []
        self.skipped_accounts = skipped_accounts
        self._load_state()
        self._publish_metrics()

    @property
    def size(self) -> int:
        return len(self._slots)

    @property
    def inference_timeout_seconds(self) -> float:
        return self.limits.inference_timeout_seconds

    def lease(self, preferred_account_id: str | None = None) -> AccountLease:
        return AccountLease(self, preferred_account_id=preferred_account_id)

    async def busy_count(self) -> int:
        async with self._condition:
            return sum(slot.busy for slot in self._slots)

    async def status(self) -> dict[str, Any]:
        async with self._condition:
            return self._status_locked()

    def _status_locked(self) -> dict[str, Any]:
        now = time.time()
        return {
            "configured": self.size,
            "busy": sum(slot.busy for slot in self._slots),
            "available": sum(
                not slot.busy and not slot.disabled and slot.cooldown_until <= now
                for slot in self._slots
            ),
            "cooldown": sum(
                not slot.disabled and slot.cooldown_until > now for slot in self._slots
            ),
            "disabled": sum(slot.disabled for slot in self._slots),
            "discovered": self.discovered_accounts,
            "invalid": self.invalid_accounts,
            "invalid_accounts": self.invalid_details,
            "duplicates": self.duplicate_accounts,
            "ignored_surplus": self.skipped_accounts,
            "maximum": self.maximum,
            "global_retry_after": max(0, round(self._global_cooldown_until - now)),
            "accounts": [
                {
                    "id": slot.account_id,
                    "file": (
                        getattr(slot.client, "account_path", None).name
                        if getattr(slot.client, "account_path", None) else "memory"
                    ),
                    "state": (
                        "disabled" if slot.disabled else
                        "busy" if slot.busy else
                        "cooldown" if slot.cooldown_until > now else
                        "ready"
                    ),
                    "retry_after": max(0, round(slot.cooldown_until - now)),
                    "assignments": slot.assignments,
                    "successes": slot.successes,
                    "failures": slot.failures,
                    "last_error": slot.last_error_code or None,
                }
                for slot in self._slots
            ],
        }

    def _publish_metrics(self) -> None:
        now = time.time()
        states = {
            "ready": sum(
                not slot.disabled and not slot.busy and slot.cooldown_until <= now
                for slot in self._slots
            ),
            "busy": sum(slot.busy for slot in self._slots),
            "cooldown": sum(
                not slot.disabled and slot.cooldown_until > now for slot in self._slots
            ),
            "disabled": sum(slot.disabled for slot in self._slots),
        }
        for state, count in states.items():
            metrics.registry.set_gauge(
                metrics.ACCOUNTS,
                count,
                help_text="Configured Notion accounts by scheduling state.",
                labels={"state": state},
            )

    async def aclose(self) -> None:
        await asyncio.gather(
            *(slot.client.aclose() for slot in self._slots),
            return_exceptions=True,
        )

    async def drain(self, timeout_seconds: float = 30.0) -> bool:
        """Wait until no lease is active so a restart cannot kill a live turn."""
        deadline = time.monotonic() + timeout_seconds
        async with self._condition:
            while any(slot.busy for slot in self._slots):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return not any(slot.busy for slot in self._slots)
        return True

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            log.warning("Ignoring malformed account pool state at %s", self._state_path)
            return
        saved_accounts = state.get("accounts", {}) if isinstance(state, dict) else {}
        if not isinstance(saved_accounts, dict):
            return
        for slot in self._slots:
            saved = saved_accounts.get(slot.account_id)
            if not isinstance(saved, dict):
                continue
            slot.last_assigned_at = float(saved.get("last_assigned_at", 0) or 0)
            slot.assignments = int(saved.get("assignments", 0) or 0)
            slot.successes = int(saved.get("successes", 0) or 0)
            slot.failures = int(saved.get("failures", 0) or 0)
            credential_unchanged = (
                float(saved.get("credential_mtime", 0) or 0) == slot.credential_mtime
            )
            if credential_unchanged:
                slot.cooldown_until = float(saved.get("cooldown_until", 0) or 0)
                slot.disabled = bool(saved.get("disabled", False))
            slot.last_error_code = str(saved.get("last_error_code", "") or "")

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        state = {
            "version": 1,
            "accounts": {
                slot.account_id: {
                    "last_assigned_at": slot.last_assigned_at,
                    "assignments": slot.assignments,
                    "successes": slot.successes,
                    "failures": slot.failures,
                    "cooldown_until": slot.cooldown_until,
                    "disabled": slot.disabled,
                    "last_error_code": slot.last_error_code,
                    "credential_mtime": slot.credential_mtime,
                }
                for slot in self._slots
            },
        }
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{os.getpid()}.tmp"
        )
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self._state_path)
        except OSError as error:
            log.warning("Could not persist account scheduler state: %s", error)
            temporary.unlink(missing_ok=True)

    async def _persist(self) -> None:
        """Write scheduler state off the event loop.

        The pool saves on every acquire, success and failure. Doing that inline
        put a synchronous disk write on the hot path of every single request.
        """
        await asyncio.to_thread(self._save_state)

    async def _acquire(
        self,
        excluded: set[int],
        preferred_account_id: str | None = None,
    ) -> _AccountSlot:
        if not self._slots:
            raise AccountPoolExhausted("No valid Notion accounts are configured")
        if len(excluded) >= len(self._slots):
            raise AccountPoolExhausted("All configured Notion accounts have failed")

        async with self._condition:
            while True:
                now = time.time()
                if self._global_cooldown_until > now:
                    retry_after = max(1, round(self._global_cooldown_until - now))
                    log_event(
                        log,
                        "circuit_breaker_active",
                        level=logging.WARNING,
                        retry_after=retry_after,
                    )
                    raise AccountPoolCoolingDown(retry_after)
                eligible = [
                    slot for index, slot in enumerate(self._slots)
                    if index not in excluded
                    and not slot.busy
                    and not slot.disabled
                    and slot.cooldown_until <= now
                ]
                selected = next(
                    (slot for slot in eligible if slot.account_id == preferred_account_id),
                    None,
                )
                if selected is None and eligible:
                    selected = min(
                        eligible,
                        key=lambda slot: (
                            slot.last_assigned_at, slot.assignments, slot.number,
                        ),
                    )
                if selected is not None:
                    selected.busy = True
                    selected.last_assigned_at = now
                    selected.assignments += 1
                    self._publish_metrics()
                    selection = (
                        "affinity" if selected.account_id == preferred_account_id
                        else "failover" if excluded
                        else "balanced"
                    )
                    log_event(
                        log,
                        "account_selected",
                        account_number=selected.number,
                        account_id=selected.account_id,
                        account_file=self._account_file(selected),
                        selection=selection,
                        attempt=len(excluded) + 1,
                        assignments=selected.assignments,
                    )
                    chosen = selected
                    break
                waiting_busy = any(
                    index not in excluded and slot.busy and not slot.disabled
                    for index, slot in enumerate(self._slots)
                )
                if waiting_busy:
                    await self._condition.wait()
                    continue
                retry_times = [
                    slot.cooldown_until for index, slot in enumerate(self._slots)
                    if index not in excluded
                    and not slot.disabled
                    and slot.cooldown_until > now
                ]
                if retry_times:
                    retry_after = max(1, round(min(retry_times) - now))
                    log_event(
                        log,
                        "account_pool_cooling_down",
                        level=logging.WARNING,
                        retry_after=retry_after,
                    )
                    raise AccountPoolCoolingDown(retry_after)
                log_event(log, "account_pool_exhausted", level=logging.ERROR)
                raise AccountPoolExhausted("No usable Notion accounts are available")
        await self._persist()
        return chosen

    @staticmethod
    def _account_file(slot: _AccountSlot) -> str:
        account_path = getattr(slot.client, "account_path", None)
        return account_path.name if account_path is not None else "memory"

    async def _release(self, slot: _AccountSlot) -> None:
        async with self._condition:
            slot.busy = False
            self._publish_metrics()
            self._condition.notify_all()

    async def _record_success(self, slot: _AccountSlot, duration_ms: int) -> None:
        async with self._condition:
            slot.successes += 1
            slot.last_error_code = ""
            log_event(
                log,
                "account_request_succeeded",
                account_number=slot.number,
                account_id=slot.account_id,
                account_file=self._account_file(slot),
                duration_ms=duration_ms,
                successes=slot.successes,
            )
        await self._persist()

    async def _record_failure(
        self,
        slot: _AccountSlot,
        error: Exception,
        duration_ms: int,
    ) -> None:
        now = time.time()
        code = error.code if isinstance(error, NotionAgentError) else type(error).__name__
        retryable: bool | None = None
        retry_after: int | None = None
        subtype = ""
        if isinstance(error, NotionAgentError):
            retryable, retry_after = retry_policy_for(error.code)
            if error.retryable is not None:
                retryable = error.retryable
                if error.retryable is False:
                    retry_after = self.limits.denial_cooldown_seconds
            subtype = error.subtype or ""
        if isinstance(error, httpx.HTTPError):
            retryable, retry_after = True, self.limits.transient_cooldown_seconds
        async with self._condition:
            slot.failures += 1
            slot.last_error_code = f"{code}:{subtype}" if subtype else str(code)
            if code in {ErrorCode.AUTH_INVALID, ErrorCode.PREMIUM_REQUIRED}:
                slot.disabled = True
                applied_delay = 0
            else:
                delay = retry_after
                if delay is None:
                    delay = (
                        self.limits.denial_cooldown_seconds if retryable is False
                        else self.limits.transient_cooldown_seconds
                    )
                applied_delay = max(0, delay)
                slot.cooldown_until = max(slot.cooldown_until, now + applied_delay)
            signature = slot.last_error_code
            self._recent_failures.append((now, slot.account_id, signature))
            window = self.limits.circuit_failure_window_seconds
            while self._recent_failures and self._recent_failures[0][0] < now - window:
                self._recent_failures.popleft()
            matching_accounts = {
                account_id
                for _, account_id, failure_signature in self._recent_failures
                if failure_signature == signature
            }
            circuit_opened = (
                len(matching_accounts) >= self.limits.circuit_failure_threshold
            )
            if circuit_opened:
                self._global_cooldown_until = max(
                    self._global_cooldown_until,
                    now + (retry_after or self.limits.transient_cooldown_seconds),
                )
                metrics.registry.increment(
                    metrics.CIRCUIT_OPENED,
                    help_text="Times the shared Notion circuit breaker opened.",
                )
            self._publish_metrics()
            log_event(
                log,
                "account_request_failed",
                level=logging.WARNING,
                account_number=slot.number,
                account_id=slot.account_id,
                account_file=self._account_file(slot),
                duration_ms=duration_ms,
                failures=slot.failures,
                disabled=slot.disabled,
                cooldown_seconds=applied_delay,
                **exception_fields(error),
            )
            if circuit_opened:
                log_event(
                    log,
                    "circuit_breaker_opened",
                    level=logging.WARNING,
                    failure_signature=signature,
                    matching_accounts=len(matching_accounts),
                    retry_after=max(0, round(self._global_cooldown_until - now)),
                )
        await self._persist()


class AccountLease:
    def __init__(
        self,
        pool: NotionAccountPool,
        *,
        preferred_account_id: str | None = None,
    ) -> None:
        self._pool = pool
        self._preferred_account_id = preferred_account_id
        self._slot: _AccountSlot | None = None
        self._attempted: set[int] = set()
        self._failures: list[str] = []

    @property
    def client(self) -> NotionAgentClient:
        if self._slot is None:
            raise RuntimeError("Notion account lease is not active")
        return self._slot.client

    @property
    def account_id(self) -> str:
        if self._slot is None:
            raise RuntimeError("Notion account lease is not active")
        return self._slot.account_id

    async def __aenter__(self) -> AccountLease:
        self._slot = await self._pool._acquire(
            self._attempted,
            preferred_account_id=self._preferred_account_id,
        )
        self._attempted.add(self._slot.number - 1)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._slot is not None:
            await self._pool._release(self._slot)
            self._slot = None

    async def run(
        self,
        operation: Callable[[NotionAgentClient], Awaitable[T]],
        *,
        retry_operation: Callable[[NotionAgentClient], Awaitable[T]] | None = None,
    ) -> T:
        active_operation = operation
        timeout = self._pool.inference_timeout_seconds
        while True:
            started_at = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    active_operation(self.client),
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                if self._slot is not None:
                    log_event(
                        log,
                        "account_request_cancelled",
                        account_number=self._slot.number,
                        account_id=self._slot.account_id,
                        account_file=self._pool._account_file(self._slot),
                        duration_ms=round((time.monotonic() - started_at) * 1000),
                    )
                raise
            except asyncio.TimeoutError as error:
                if self._slot is not None:
                    log_event(
                        log,
                        "account_request_timed_out",
                        level=logging.WARNING,
                        account_number=self._slot.number,
                        account_id=self._slot.account_id,
                        account_file=self._pool._account_file(self._slot),
                        duration_ms=round((time.monotonic() - started_at) * 1000),
                        timeout_seconds=timeout,
                    )
                raise TimeoutError(
                    f"Notion inference exceeded {timeout:g} seconds"
                ) from error
            except Exception as error:
                if not is_failover_error(error):
                    if self._slot is not None:
                        log_event(
                            log,
                            "account_request_rejected",
                            level=logging.WARNING,
                            account_number=self._slot.number,
                            account_id=self._slot.account_id,
                            account_file=self._pool._account_file(self._slot),
                            duration_ms=round((time.monotonic() - started_at) * 1000),
                            **exception_fields(error),
                        )
                    raise
                await self._switch_account(
                    error,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                )
                if retry_operation is not None:
                    active_operation = retry_operation
                continue
            if self._slot is not None:
                await self._pool._record_success(
                    self._slot,
                    round((time.monotonic() - started_at) * 1000),
                )
            return result

    async def _switch_account(self, error: Exception, *, duration_ms: int) -> None:
        if self._slot is None:
            raise RuntimeError("Notion account lease is not active")
        code = error.code if isinstance(error, NotionAgentError) else type(error).__name__
        self._failures.append(f"account {self._slot.number}: {code}")
        previous_number = self._slot.number
        previous_id = self._slot.account_id
        previous_file = self._pool._account_file(self._slot)
        await self._pool._record_failure(self._slot, error, duration_ms)
        await self._pool._release(self._slot)
        self._slot = None
        if len(self._attempted) >= self._pool.size:
            failures = ", ".join(self._failures)
            log_event(
                log,
                "account_pool_exhausted",
                level=logging.ERROR,
                attempted=len(self._attempted),
                last_account_id=previous_id,
                last_account_file=previous_file,
            )
            raise AccountPoolExhausted(
                f"All {self._pool.size} Notion accounts failed ({failures})"
            ) from error
        self._slot = await self._pool._acquire(self._attempted)
        self._attempted.add(self._slot.number - 1)
        metrics.registry.increment(
            metrics.FAILOVERS,
            help_text="Automatic switches to another Notion account after a failure.",
        )
        log_event(
            log,
            "account_failover",
            level=logging.WARNING,
            from_account_number=previous_number,
            from_account_id=previous_id,
            from_account_file=previous_file,
            to_account_number=self._slot.number,
            to_account_id=self._slot.account_id,
            to_account_file=self._pool._account_file(self._slot),
        )
