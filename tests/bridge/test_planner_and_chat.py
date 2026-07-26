"""Planner loop, coding-tools client and Chat Completions behaviour."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from notion_bridge.api.chat import handle_chat_completions
from notion_bridge.planner.loop import (
    PlannerLoopExceeded,
    complete_chat_turn,
    run_planner_loop,
)
from notion_bridge.planner.prompts import build_chat_prompt
from notion_bridge.planner.runtime_tools import (
    RuntimeToolClient,
    RuntimeToolsUnavailable,
    parse_mcp_payload,
)
from notion_bridge.planner.toolcalls import (
    extract_chat_tool_call,
    extract_planner_action,
    extract_workflow_call,
    planner_action_to_tool,
)
from tests.bridge.support import FakeLease, FakePool, build_service, completion


class RecordingToolClient(RuntimeToolClient):
    def __init__(self, results: list[str]) -> None:
        super().__init__("http://127.0.0.1:1/mcp/secret")
        self.results = results
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        return self.results.pop(0) if self.results else "{}"


class PlannerActionTests(unittest.TestCase):
    def test_a_fenced_action_is_decoded(self) -> None:
        action = extract_planner_action('```json\n{"action":"list_files"}\n```')
        self.assertEqual(action, {"action": "list_files"})

    def test_prose_is_not_an_action(self) -> None:
        self.assertIsNone(extract_planner_action("I would list the files next."))

    def test_every_action_maps_onto_a_runtime_tool(self) -> None:
        self.assertEqual(
            planner_action_to_tool({"action": "list_files", "directory": "src"}),
            ("list_files", {"directory": "src"}),
        )
        self.assertEqual(
            planner_action_to_tool(
                {"action": "read_file", "file_path": "a.py", "max_bytes": 10}
            ),
            ("read_file", {"file_path": "a.py", "max_bytes": 10}),
        )
        self.assertEqual(
            planner_action_to_tool(
                {"action": "edit_file", "file_path": "a.py",
                 "old_text": "x", "new_text": "y", "replace_all": True}
            )[1]["replace_all"],
            True,
        )
        self.assertEqual(
            planner_action_to_tool({"action": "run_shell", "command": "pytest"})[0],
            "run_shell",
        )
        self.assertIsNone(planner_action_to_tool({"action": "delete_everything"}))

    def test_a_workflow_tool_call_is_decoded(self) -> None:
        text = json.dumps({
            "function": "runtime.runTool",
            "args": {"toolName": "read_file", "toolArguments": {"file_path": "a.py"}},
        })
        self.assertEqual(
            extract_workflow_call(text), ("read_file", {"file_path": "a.py"})
        )
        self.assertEqual(
            extract_workflow_call(json.dumps({"function": "x.listTools", "args": {}})),
            ("listTools", {}),
        )

    def test_chat_tool_calls_are_read_from_the_function_schema(self) -> None:
        tools = [{"type": "function", "function": {"name": "search"}}]
        self.assertEqual(
            extract_chat_tool_call('{"tool":"search","arguments":{"q":"x"}}', tools),
            ("search", {"q": "x"}),
        )
        self.assertEqual(
            extract_chat_tool_call(
                '<tool_call>{"name":"search","parameters":{"q":"y"}}</tool_call>', tools
            ),
            ("search", {"q": "y"}),
        )
        self.assertIsNone(extract_chat_tool_call('{"tool":"unknown"}', tools))
        self.assertIsNone(extract_chat_tool_call('{"tool":"search"}', None))


class ChatPromptTests(unittest.TestCase):
    def test_system_messages_are_separated_from_the_conversation(self) -> None:
        system, prompt = build_chat_prompt([
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])
        self.assertEqual(system, "be brief")
        self.assertEqual(prompt, "[user]\nhello\n\n[assistant]\nhi")

    def test_the_tool_catalog_is_appended_to_the_system_prompt(self) -> None:
        system, _prompt = build_chat_prompt(
            [{"role": "user", "content": "hello"}],
            [{"type": "function", "function": {"name": "search"}}],
        )
        self.assertIn("Tool catalog", system)

    def test_a_custom_agent_deployment_omits_the_catalog(self) -> None:
        system, _prompt = build_chat_prompt(
            [{"role": "user", "content": "hello"}],
            [{"type": "function", "function": {"name": "search"}}],
            include_tool_catalog=False,
        )
        self.assertIsNone(system)


class RuntimeToolClientTests(unittest.IsolatedAsyncioTestCase):
    def test_a_json_rpc_reply_is_parsed_from_plain_json_or_sse(self) -> None:
        self.assertEqual(parse_mcp_payload('{"result":{"ok":true}}'), {"result": {"ok": True}})
        self.assertEqual(
            parse_mcp_payload('event: message\ndata: {"result":{"ok":true}}\n'),
            {"result": {"ok": True}},
        )

    async def test_an_unconfigured_endpoint_reports_itself_clearly(self) -> None:
        client = RuntimeToolClient(None)
        self.assertFalse(client.configured)
        with self.assertRaises(RuntimeToolsUnavailable):
            await client.call("list_files", {})

    async def test_a_failure_becomes_a_tool_result_the_model_can_read(self) -> None:
        client = RuntimeToolClient(None)
        payload = json.loads(await client.call_or_error_json("list_files", {}))
        self.assertTrue(payload["isError"])
        self.assertIn("not configured", payload["error"])

    async def test_closing_an_unused_client_is_safe(self) -> None:
        await RuntimeToolClient(None).aclose()


class PlannerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_loop_runs_actions_until_final(self) -> None:
        prompts: list[str] = []
        replies = [
            '{"action":"list_files","directory":"."}',
            '{"action":"read_file","file_path":"a.py"}',
            '{"action":"final","message":"all done"}',
        ]

        class Client:
            async def complete(self, **kwargs):
                prompts.append(kwargs["prompt"])
                return completion(replies.pop(0), "notion-thread")

        service = build_service()
        tools = RecordingToolClient(['{"content":[]}', '{"content":[]}'])
        lease = FakeLease("account-a", lambda _id: Client())
        response = await run_planner_loop(
            lease, tools, service.settings,
            prompt="fix the bug", system=None, model="opus-5",
        )

        self.assertEqual(response.text, "all done")
        self.assertEqual([name for name, _ in tools.calls], ["list_files", "read_file"])
        self.assertIn("fix the bug", prompts[0])
        self.assertIn("The local operator executed your recommendation", prompts[1])

    async def test_prose_ends_the_loop_without_an_action(self) -> None:
        class Client:
            async def complete(self, **kwargs):
                return completion("Here is the answer.", "notion-thread")

        service = build_service()
        response = await run_planner_loop(
            FakeLease("account-a", lambda _id: Client()),
            RecordingToolClient([]),
            service.settings,
            prompt="explain", system=None, model="opus-5",
        )
        self.assertEqual(response.text, "Here is the answer.")

    async def test_a_runaway_loop_is_bounded(self) -> None:
        class Client:
            async def complete(self, **kwargs):
                return completion('{"action":"list_files"}', "notion-thread")

        service = build_service(NOTION_PLANNER_MAX_STEPS="3")
        with self.assertRaises(PlannerLoopExceeded):
            await run_planner_loop(
                FakeLease("account-a", lambda _id: Client()),
                RecordingToolClient(['{"content":[]}'] * 5),
                service.settings,
                prompt="loop forever", system=None, model="opus-5",
            )

    async def test_a_tool_failure_is_reported_back_to_the_planner(self) -> None:
        prompts: list[str] = []
        replies = ['{"action":"run_shell","command":"pytest"}', '{"action":"final","message":"ok"}']

        class Client:
            async def complete(self, **kwargs):
                prompts.append(kwargs["prompt"])
                return completion(replies.pop(0), "notion-thread")

        service = build_service()
        # An unconfigured runtime is the simplest way to force a tool failure.
        response = await run_planner_loop(
            FakeLease("account-a", lambda _id: Client()),
            RuntimeToolClient(None),
            service.settings,
            prompt="run the tests", system=None, model="opus-5",
        )
        self.assertEqual(response.text, "ok")
        self.assertIn("isError", prompts[1])

    async def test_without_tools_the_turn_is_a_single_completion(self) -> None:
        calls: list[dict] = []

        class Client:
            async def complete(self, **kwargs):
                calls.append(kwargs)
                return completion("plain answer", "notion-thread")

        service = build_service()
        response = await complete_chat_turn(
            FakeLease("account-a", lambda _id: Client()),
            RecordingToolClient([]),
            service.settings,
            prompt="hello", system=None, model="opus-5", planner_mode=False,
        )
        self.assertEqual(response.text, "plain answer")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["workspace_search"])


class ChatEndpointTests(unittest.IsolatedAsyncioTestCase):
    def responder(self, text: str):
        def build(_account_id: str):
            class Client:
                async def complete(self, **_kwargs):
                    return completion(text, "notion-thread")

            return Client()

        return build

    async def test_a_plain_turn_returns_a_chat_completion(self) -> None:
        service = build_service(FakePool(self.responder("hello there")))
        result = await handle_chat_completions(service, {
            "model": "opus-5",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(result["choices"][0]["message"]["content"], "hello there")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(result["usage"]["total_tokens"], 12)

    async def test_a_tool_call_is_surfaced_as_tool_calls(self) -> None:
        service = build_service(
            FakePool(self.responder('{"tool":"search","arguments":{"q":"x"}}'))
        )
        result = await handle_chat_completions(service, {
            "model": "opus-5",
            "messages": [{"role": "user", "content": "find x"}],
            "tools": [{"type": "function", "function": {"name": "search"}}],
        })
        choice = result["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "search")

    async def test_an_empty_conversation_is_rejected(self) -> None:
        service = build_service(FakePool(self.responder("unused")))
        response = await handle_chat_completions(service, {"messages": []})
        self.assertEqual(response.status_code, 400)

    async def test_an_unsupported_model_is_rejected_before_leasing(self) -> None:
        service = build_service(FakePool(self.responder("unused")))
        response = await handle_chat_completions(service, {
            "model": "gemini-3-pro",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(response.status_code, 400)

    async def test_a_cooling_down_pool_becomes_503_with_retry_after(self) -> None:
        from notion_bridge.accounts.pool import AccountPoolCoolingDown

        class CoolingPool(FakePool):
            def lease(self, preferred_account_id=None):
                raise AccountPoolCoolingDown(42)

        service = build_service(CoolingPool(self.responder("unused")))
        response = await handle_chat_completions(service, {
            "model": "opus-5",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "42")

    async def test_a_disconnecting_client_does_not_leak_the_lease(self) -> None:
        # Cancelling the generator used to leave the inference task running, so
        # the Notion account stayed leased until the inference timeout.
        released = asyncio.Event()
        cancelled = asyncio.Event()

        class Client:
            async def complete(self, **_kwargs):
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return completion("late", "notion-thread")

        class TrackingLease(FakeLease):
            async def __aexit__(self, *_args):
                released.set()
                return None

        class TrackingPool(FakePool):
            def lease(self, preferred_account_id=None):
                return TrackingLease(preferred_account_id or "account-a", lambda _id: Client())

        service = build_service(TrackingPool(lambda _id: Client()))
        result = await handle_chat_completions(service, {
            "model": "opus-5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
        stream = result.body_iterator
        consume = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.05)
        consume.cancel()
        await asyncio.gather(consume, return_exceptions=True)
        await stream.aclose()
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        await asyncio.wait_for(released.wait(), timeout=2)

    async def test_a_streamed_turn_ends_with_done(self) -> None:
        service = build_service(FakePool(self.responder("streamed answer")))
        result = await handle_chat_completions(service, {
            "model": "opus-5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
        chunks = [chunk async for chunk in result.body_iterator]
        text = b"".join(
            chunk if isinstance(chunk, bytes) else chunk.encode() for chunk in chunks
        ).decode()
        self.assertIn("streamed answer", text)
        self.assertTrue(text.endswith("data: [DONE]\n\n"))


class SimpleNamespaceUsageTests(unittest.TestCase):
    def test_the_completion_helper_matches_the_provider_shape(self) -> None:
        value = completion("text", "thread")
        self.assertIsInstance(value, SimpleNamespace)
        self.assertEqual(value.usage.input_tokens, 10)


if __name__ == "__main__":
    unittest.main()
