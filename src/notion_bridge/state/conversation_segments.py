"""Durable mapping from a Codex conversation to its Notion thread segment.

The file holds no conversation content: keys are hashed and only identifiers,
counters and fingerprints are written. It is an optimisation, so a corrupt or
unwritable file degrades to "start a fresh Notion thread" instead of failing
the request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CONVERSATION_TTL = 30 * 24 * 60 * 60
MAX_CONVERSATIONS = 500
# Version 2 invalidates bindings created while Notion recorded the thread as Auto.
STATE_VERSION = 2

log = logging.getLogger("uvicorn.error.notion_bridge")


def conversation_storage_key(key: str) -> str:
    """Return a non-reversible identifier suitable for logs and disk."""
    return hashlib.sha256(key.encode()).hexdigest()


def response_input_fingerprints(body: dict[str, Any]) -> tuple[str, ...]:
    value = body.get("input")
    items = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    return tuple(
        hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for item in items
    )


def input_prefix_length(
    previous: tuple[str, ...], current: tuple[str, ...]
) -> int | None:
    if len(previous) > len(current) or current[:len(previous)] != previous:
        return None
    return len(previous)


@dataclass(slots=True)
class ConversationSegment:
    account_id: str
    notion_thread_id: str
    input_fingerprints: tuple[str, ...]
    segment_index: int
    awaiting_compacted_history: bool
    turns: int
    input_tokens: int
    output_tokens: int
    updated_at: float
    model: str | None = None


class ConversationSegmentStore:
    """Persistent, content-free mapping from a Codex thread to a Notion segment."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl: int = CONVERSATION_TTL,
        maximum: int = MAX_CONVERSATIONS,
    ) -> None:
        self.path = path
        self.ttl = ttl
        self.maximum = maximum
        self._items: dict[str, ConversationSegment] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._loaded = False
        self._write_errors = 0

    async def get(self, key: str | None) -> ConversationSegment | None:
        if key is None:
            return None
        async with self._guard:
            self._load_locked()
            changed = self._cleanup_locked()
            snapshot = self._payload_locked() if changed else None
            item = self._items.get(conversation_storage_key(key))
        if snapshot is not None:
            await self._write(snapshot)
        return item

    async def put(
        self,
        key: str | None,
        *,
        account_id: str,
        notion_thread_id: str,
        input_fingerprints: tuple[str, ...],
        segment_index: int,
        awaiting_compacted_history: bool,
        turns: int,
        input_tokens: int,
        output_tokens: int,
        model: str,
    ) -> None:
        if key is None:
            return
        async with self._guard:
            self._load_locked()
            self._items[conversation_storage_key(key)] = ConversationSegment(
                account_id=account_id,
                notion_thread_id=notion_thread_id,
                input_fingerprints=input_fingerprints,
                segment_index=segment_index,
                awaiting_compacted_history=awaiting_compacted_history,
                turns=turns,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                updated_at=time.time(),
                model=model,
            )
            self._cleanup_locked()
            snapshot = self._payload_locked()
        await self._write(snapshot)

    @asynccontextmanager
    async def lock(self, key: str | None) -> AsyncIterator[None]:
        if key is None:
            yield
            return
        storage_key = conversation_storage_key(key)
        async with self._guard:
            lock = self._locks.setdefault(storage_key, asyncio.Lock())
        try:
            async with lock:
                yield
        finally:
            async with self._guard:
                self._discard_idle_locks_locked()

    async def status(self) -> dict[str, Any]:
        async with self._guard:
            self._load_locked()
            changed = self._cleanup_locked()
            snapshot = self._payload_locked() if changed else None
            result = {
                "active": len(self._items),
                "locks": len(self._locks),
                "ttl_seconds": self.ttl,
                "maximum": self.maximum,
                "persistent": self.path is not None,
                "write_errors": self._write_errors,
            }
        if snapshot is not None:
            await self._write(snapshot)
        return result

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf8"))
            if payload.get("version") != STATE_VERSION:
                return
            for key, raw in payload.get("conversations", {}).items():
                if not isinstance(key, str) or not isinstance(raw, dict):
                    continue
                self._items[key] = ConversationSegment(
                    account_id=str(raw["account_id"]),
                    notion_thread_id=str(raw["notion_thread_id"]),
                    input_fingerprints=tuple(raw.get("input_fingerprints", [])),
                    segment_index=int(raw.get("segment_index", 0)),
                    awaiting_compacted_history=bool(
                        raw.get("awaiting_compacted_history", False)
                    ),
                    turns=int(raw.get("turns", 0)),
                    input_tokens=int(raw.get("input_tokens", 0)),
                    output_tokens=int(raw.get("output_tokens", 0)),
                    updated_at=float(raw.get("updated_at", 0)),
                    model=(str(raw["model"]) if raw.get("model") else None),
                )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # State is an optimization. A corrupt/stale file must never prevent
            # the bridge from creating a clean Notion thread.
            self._items = {}

    def _cleanup_locked(self) -> bool:
        cutoff = time.time() - self.ttl
        before = len(self._items)
        self._items = {
            key: item for key, item in self._items.items() if item.updated_at >= cutoff
        }
        if len(self._items) > self.maximum:
            newest = sorted(
                self._items.items(), key=lambda pair: pair[1].updated_at, reverse=True
            )[:self.maximum]
            self._items = dict(newest)
        self._discard_idle_locks_locked()
        return len(self._items) != before

    def _discard_idle_locks_locked(self) -> None:
        for key in [
            key for key, lock in self._locks.items()
            if key not in self._items and not lock.locked()
        ]:
            self._locks.pop(key, None)

    def _payload_locked(self) -> str:
        return json.dumps(
            {
                "version": STATE_VERSION,
                "conversations": {
                    key: {**asdict(item), "input_fingerprints": list(item.input_fingerprints)}
                    for key, item in self._items.items()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    async def _write(self, payload: str) -> None:
        if self.path is None:
            return
        await asyncio.to_thread(self._write_blocking, payload)

    def _write_blocking(self, payload: str) -> None:
        assert self.path is not None
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf8")
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self.path)
        except OSError as error:
            # Losing the segment binding only costs one extra Notion thread;
            # failing the caller's turn would cost the user their request.
            self._write_errors += 1
            log.warning("Could not persist conversation segment state: %s", error)
            temporary.unlink(missing_ok=True)
