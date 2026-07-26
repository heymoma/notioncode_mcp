"""Regression tests for state that has to survive an always-on process."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from notion_bridge.state.conversation_segments import ConversationSegmentStore
from notion_bridge.state.turn_affinity import TurnAffinityStore


def affinity_kwargs(**overrides):
    return {
        "account_id": "account-a",
        "notion_thread_id": "notion-thread",
        "input_count": 1,
        "input_fingerprint": "fingerprint",
        "completion_text": "done",
        "input_tokens": 10,
        "output_tokens": 2,
        "model": "opus-5",
        **overrides,
    }


class TurnAffinityHygieneTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_turn_that_never_completes_leaves_no_lock_behind(self) -> None:
        # Every failed or cancelled turn used to leak one asyncio.Lock, so a
        # process that runs for weeks grew unbounded.
        store = TurnAffinityStore()
        for index in range(50):
            async with store.lock(f"turn-{index}"):
                pass
        self.assertEqual((await store.status())["locks"], 0)

    async def test_a_stored_turn_keeps_its_lock_for_the_retry(self) -> None:
        store = TurnAffinityStore()
        async with store.lock("turn"):
            await store.put("turn", **affinity_kwargs())
        status = await store.status()
        self.assertEqual(status["active"], 1)
        self.assertEqual(status["locks"], 1)

    async def test_a_held_lock_still_serializes_two_requests_for_one_turn(self) -> None:
        store = TurnAffinityStore()
        order: list[str] = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first() -> None:
            async with store.lock("same-turn"):
                order.append("first-in")
                entered.set()
                await release.wait()
                order.append("first-out")

        async def second() -> None:
            await entered.wait()
            async with store.lock("same-turn"):
                order.append("second-in")

        tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
        await entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(order, ["first-in"])
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(order, ["first-in", "first-out", "second-in"])

    async def test_entries_are_capped_so_memory_cannot_grow_forever(self) -> None:
        store = TurnAffinityStore(maximum=5)
        for index in range(20):
            await store.put(f"turn-{index}", **affinity_kwargs())
        status = await store.status()
        self.assertEqual(status["active"], 5)
        self.assertEqual(status["maximum"], 5)

    async def test_expired_entries_are_dropped(self) -> None:
        store = TurnAffinityStore(ttl=1)
        await store.put("turn", **affinity_kwargs())
        store._items["turn"].updated_at = time.time() - 10
        self.assertIsNone(await store.get("turn"))
        self.assertEqual((await store.status())["active"], 0)


class ConversationSegmentDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def segment_kwargs(self, **overrides):
        return {
            "account_id": "account-a",
            "notion_thread_id": "notion-thread",
            "input_fingerprints": ("hash-a",),
            "segment_index": 0,
            "awaiting_compacted_history": False,
            "turns": 1,
            "input_tokens": 10,
            "output_tokens": 2,
            "model": "opus-5",
            **overrides,
        }

    async def test_an_unwritable_state_file_does_not_fail_the_turn(self) -> None:
        # Losing the binding costs one extra Notion thread; raising here would
        # cost the user their request.
        store = ConversationSegmentStore(Path("/proc/definitely/not/writable/state.json"))
        await store.put("codex-thread", **await self.segment_kwargs())
        status = await store.status()
        self.assertEqual(status["active"], 1)
        self.assertGreaterEqual(status["write_errors"], 1)
        restored = await store.get("codex-thread")
        self.assertIsNotNone(restored)

    async def test_state_is_written_atomically_with_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conversation-state.json"
            store = ConversationSegmentStore(path)
            await store.put("codex-thread", **await self.segment_kwargs())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            # No temporary file is left behind after a successful replace.
            self.assertEqual(
                sorted(entry.name for entry in Path(directory).iterdir()),
                ["conversation-state.json"],
            )

    async def test_locks_are_released_for_conversations_that_never_store(self) -> None:
        store = ConversationSegmentStore()
        for index in range(30):
            async with store.lock(f"conversation-{index}"):
                pass
        self.assertEqual((await store.status())["locks"], 0)

    async def test_the_newest_conversations_win_when_capped(self) -> None:
        store = ConversationSegmentStore(maximum=3)
        for index in range(10):
            await store.put(f"conversation-{index}", **await self.segment_kwargs())
        self.assertEqual((await store.status())["active"], 3)
        self.assertIsNotNone(await store.get("conversation-9"))


if __name__ == "__main__":
    unittest.main()
