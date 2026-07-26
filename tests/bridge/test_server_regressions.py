from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from notion_agent_cli import transcript as notion_transcript

from notion_bridge.api import responses as responses_api
from notion_bridge.api.payloads import (
    responses_incremental_body,
    responses_payload,
    responses_sse,
)
from notion_bridge.api.responses import (
    handle_openai_compaction,
    handle_openai_responses,
    stream_openai_responses,
)
from notion_bridge.notion.models import pin_explicit_model_selection, resolve_model
from notion_bridge.planner.prompts import (
    responses_incremental_prompt,
    responses_message_text,
    responses_planner_prompt,
)
from notion_bridge.planner.toolcalls import (
    extract_malformed_responses_tool,
    extract_responses_tool_call,
    responses_tool_catalog,
)
from tests.bridge.support import FakePool, build_service, completion

CODE_ROOT = Path("/code-root")


def resolve(model: str, forced: str = "") -> str:
    return resolve_model(model, default_model="opus-5", forced_model=forced)


class ResponsesTextRegressionTests(unittest.TestCase):
    def test_codex_fable_transport_id_resolves_to_notion_fable(self) -> None:
        self.assertEqual(resolve("gpt-5.5"), "fable-5")
        self.assertEqual(resolve("fable-5"), "fable-5")

    def test_opus_aliases_resolve_to_notion_opus_5(self) -> None:
        self.assertEqual(resolve("opus-5"), "opus-5")
        self.assertEqual(resolve("opus"), "opus-5")
        self.assertEqual(resolve("claude-opus-5"), "opus-5")
        self.assertEqual(resolve("best"), "opus-5")

    def test_unknown_model_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported model"):
            resolve("gemini-3-pro")

    def test_forced_opus_replaces_every_requested_model(self) -> None:
        for requested in ("gpt-5.5", "fable-5", "gpt-5.6-sol", "opus-5", "anything"):
            self.assertEqual(resolve(requested, forced="opus-5"), "opus-5")

    def test_notion_model_stays_explicit_on_continuation(self) -> None:
        pin_explicit_model_selection()
        config = notion_transcript.build_config_value(
            notion_model="agave-flan",
            is_subsequent_turn=True,
        )
        self.assertEqual(config["model"], "agave-flan")
        self.assertIs(config["modelFromUser"], True)

    def test_pinning_the_model_config_is_idempotent(self) -> None:
        pin_explicit_model_selection()
        first = notion_transcript.build_config_value
        pin_explicit_model_selection()
        self.assertIs(notion_transcript.build_config_value, first)

    def test_input_image_does_not_replace_or_mutate_text(self) -> None:
        message = {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "keep this exact request"},
                {"type": "input_image", "image_url": "data:image/png;base64,ignored-here"},
            ],
        }
        self.assertEqual(responses_message_text(message), "[user]\nkeep this exact request")

    def test_text_only_planner_prompt_remains_stable(self) -> None:
        body = {
            "instructions": "cwd: /srv/project",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "list files"}],
            }],
            "tools": [],
        }
        prompt = responses_planner_prompt(body, [], CODE_ROOT)
        self.assertIn(
            "The local operator's current working directory is /srv/project.", prompt
        )
        self.assertIn("[user]\nlist files", prompt)

    def test_planner_prompt_falls_back_to_code_root(self) -> None:
        prompt = responses_planner_prompt({"input": "hello"}, [], CODE_ROOT)
        self.assertIn(
            "The local operator's current working directory is /code-root.", prompt
        )

    def test_namespace_tools_are_flattened_for_native_codex_calls(self) -> None:
        tools = responses_tool_catalog([{
            "type": "namespace",
            "name": "multi_agent_v1",
            "tools": [{"type": "function", "name": "spawn_agent", "parameters": {}}],
        }, {"type": "web_search"}])
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "multi_agent_v1.spawn_agent")
        self.assertEqual(tools[0]["namespace"], "multi_agent_v1")

    def test_structured_output_is_forwarded_to_planner(self) -> None:
        prompt = responses_planner_prompt({
            "input": "return a status",
            "text": {"format": {
                "type": "json_schema",
                "name": "status",
                "schema": {"type": "object", "required": ["ok"]},
            }},
        }, [], CODE_ROOT)
        self.assertIn("[user]\nreturn a status", prompt)
        self.assertIn('"required": ["ok"]', prompt)

    def test_sse_contains_full_codex_text_event_sequence(self) -> None:
        response, item = responses_payload("done", "fable-5", 8, 2, [])
        chunks = b"".join(responses_sse(response, item)).decode()
        self.assertIn("event: response.output_text.delta", chunks)
        self.assertIn("event: response.completed", chunks)
        self.assertTrue(chunks.endswith("data: [DONE]\n\n"))
        events = [
            json.loads(line[6:])
            for line in chunks.splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(
            [event["sequence_number"] for event in events],
            list(range(len(events))),
        )

    def test_sse_tool_call_is_complete_for_codex_runtime(self) -> None:
        response, item = responses_payload(
            '{"tool":"update_plan","arguments":{"plan":[]}}',
            "fable-5",
            8,
            2,
            [{"type": "function", "name": "update_plan", "parameters": {}}],
        )
        chunks = b"".join(responses_sse(response, item)).decode()
        self.assertEqual(item["type"], "function_call")
        self.assertIn('"name": "update_plan"', chunks)
        self.assertIn("event: response.output_item.done", chunks)

    def test_detects_malformed_textual_tool_call(self) -> None:
        text = '{"tool":"exec_command","arguments":{"cmd":"bash -lc "cd /opt/app""}}'
        tools = [{"type": "function", "name": "exec_command", "parameters": {}}]
        self.assertEqual(extract_malformed_responses_tool(text, tools), "exec_command")

    def test_detects_tool_with_noop_invoke_body_as_malformed(self) -> None:
        text = '{"tool":"exec_command">\n{"function":"noop"}\n</invoke>'
        tools = [{"type": "function", "name": "exec_command", "parameters": {}}]
        self.assertIsNone(extract_responses_tool_call(text, tools))
        self.assertEqual(extract_malformed_responses_tool(text, tools), "exec_command")

    def test_converts_antml_parameters_to_function_call(self) -> None:
        text = (
            '{"tool":"exec_command","antml:parameter name="cmd">'
            "bash -lc 'cd /opt/app && sed -n \"1,20p\" main.py'"
            "</parameter>\n"
            '<parameter name="yield_time_ms">120000</parameter>\n</invoke>'
        )
        tools = [{"type": "function", "name": "exec_command", "parameters": {}}]
        tool_type, name, raw_arguments = extract_responses_tool_call(text, tools)
        self.assertEqual(tool_type, "function")
        self.assertEqual(name, "exec_command")
        self.assertEqual(
            json.loads(raw_arguments),
            {
                "cmd": "bash -lc 'cd /opt/app && sed -n \"1,20p\" main.py'",
                "yield_time_ms": 120000,
            },
        )
        _response, item = responses_payload(text, "opus-5", 10, 2, tools)
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["name"], "exec_command")
        self.assertEqual(json.loads(item["arguments"]), json.loads(raw_arguments))

    def test_converts_standard_anthropic_invoke_markup(self) -> None:
        text = (
            '<invoke name="write_stdin">'
            '<parameter name="session_id">42</parameter>'
            '<parameter name="chars">y\\n</parameter>'
            "</invoke>"
        )
        tools = [{"type": "function", "name": "write_stdin", "parameters": {}}]
        tool_type, name, raw_arguments = extract_responses_tool_call(text, tools)
        self.assertEqual(tool_type, "function")
        self.assertEqual(name, "write_stdin")
        self.assertEqual(json.loads(raw_arguments), {"session_id": 42, "chars": "y\\n"})

    def test_custom_tool_call_uses_the_command_payload(self) -> None:
        tools = [{"type": "custom", "name": "shell"}]
        tool_type, name, payload = extract_responses_tool_call(
            '{"tool":"shell","input":{"command":"ls -la"}}', tools
        )
        self.assertEqual((tool_type, name, payload), ("custom", "shell", "ls -la"))

    def test_does_not_flag_normal_text_or_valid_tool_call_as_malformed(self) -> None:
        tools = [{"type": "function", "name": "exec_command", "parameters": {}}]
        self.assertIsNone(
            extract_malformed_responses_tool(
                "I would use exec_command if another action were needed.", tools
            )
        )
        self.assertIsNone(
            extract_malformed_responses_tool(
                '{"tool":"exec_command","arguments":{"cmd":"pwd"}}', tools
            )
        )

    def test_tool_loop_continuation_sends_only_new_tool_result(self) -> None:
        body = {
            "input": [
                {"type": "message", "role": "user", "content": "task"},
                {
                    "type": "function_call", "name": "update_plan",
                    "call_id": "call-1", "arguments": "{}",
                },
                {
                    "type": "function_call_output", "call_id": "call-1",
                    "output": "Plan updated",
                },
            ],
            "tools": [{"type": "function", "name": "update_plan"}],
        }
        incremental = responses_incremental_body(body, 1)
        self.assertIsNotNone(incremental)
        self.assertEqual(len(incremental["input"]), 1)
        self.assertEqual(incremental["input"][0]["type"], "function_call_output")
        self.assertEqual(incremental["tools"], [])
        prompt = responses_incremental_prompt(incremental)
        self.assertIn("Plan updated", prompt)
        self.assertNotIn('"name": "update_plan"', prompt)

    def test_compaction_item_is_forwarded_into_a_fresh_segment(self) -> None:
        text = responses_message_text({
            "type": "compaction",
            "encrypted_content": "checkpoint with image facts",
        })
        self.assertIn("checkpoint with image facts", text)


class ResponsesThinkingStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_progress_heartbeat_while_notion_is_silent(self) -> None:
        service = build_service(NOTION_REASONING_HEARTBEAT_SECONDS="1")

        async def slow_handle(_service, _body, _turn_key, *, response_id=None, **_kwargs):
            await responses_api.asyncio.sleep(0.03)
            response, _item = responses_payload(
                "done", "opus-5", 10, 5, [], response_id=response_id
            )
            return response

        with (
            patch.object(responses_api, "handle_openai_responses", slow_handle),
            patch.object(
                type(service.settings),
                "reasoning_heartbeat_seconds",
                property(lambda _self: 0.01),
            ),
        ):
            text = b"".join([
                chunk
                async for chunk in stream_openai_responses(
                    service,
                    {"model": "opus-5", "stream": True},
                    "turn-heartbeat",
                    "conversation-heartbeat",
                    "turn",
                )
            ]).decode()

        self.assertIn("Still working", text)

    async def test_streams_reasoning_before_buffered_tool_call(self) -> None:
        service = build_service()

        async def fake_handle(
            _service,
            _body,
            _turn_key,
            *,
            on_thinking_delta_async=None,
            response_id=None,
            **_kwargs,
        ):
            await on_thinking_delta_async("Reading ")
            await on_thinking_delta_async("bench.txt")
            response, _item = responses_payload(
                '{"tool":"update_plan","arguments":{"plan":[]}}',
                "opus-5",
                10,
                5,
                [{"type": "function", "name": "update_plan"}],
                response_id=response_id,
            )
            return response

        with patch.object(responses_api, "handle_openai_responses", fake_handle):
            chunks = [
                chunk
                async for chunk in stream_openai_responses(
                    service,
                    {"model": "opus-5", "stream": True},
                    "turn-thinking",
                    "conversation-thinking",
                    "turn",
                )
            ]

        text = b"".join(chunks).decode()
        events = [
            json.loads(line.removeprefix("data: "))
            for line in text.splitlines()
            if line.startswith("data: {")
        ]
        event_types = [event["type"] for event in events]
        self.assertLess(
            event_types.index("response.reasoning_summary_text.delta"),
            event_types.index("response.output_item.done"),
        )
        reasoning_deltas = [
            event["delta"]
            for event in events
            if event["type"] == "response.reasoning_summary_text.delta"
        ]
        self.assertEqual(reasoning_deltas[-2:], ["Reading ", "bench.txt"])
        self.assertIn("Notion opus-5 is working", reasoning_deltas[0])
        self.assertNotIn("response.output_text.delta", event_types)
        completed = next(
            event for event in events if event["type"] == "response.completed"
        )
        self.assertEqual(
            [item["type"] for item in completed["response"]["output"]],
            ["reasoning", "function_call"],
        )
        self.assertEqual(
            [event["sequence_number"] for event in events],
            list(range(len(events))),
        )

    async def test_upstream_failure_becomes_a_response_failed_event(self) -> None:
        service = build_service()

        async def failing_handle(_service, _body, _turn_key, **_kwargs):
            raise RuntimeError("notion is unhappy")

        with patch.object(responses_api, "handle_openai_responses", failing_handle):
            text = b"".join([
                chunk
                async for chunk in stream_openai_responses(
                    service,
                    {"model": "opus-5", "stream": True},
                    "turn-failure",
                    "conversation-failure",
                    "turn",
                )
            ]).decode()

        self.assertIn("event: response.failed", text)
        self.assertIn("notion is unhappy", text)
        self.assertTrue(text.endswith("data: [DONE]\n\n"))


class ResponsesAffinityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_change_starts_a_new_notion_thread(self) -> None:
        calls: list[dict] = []

        def responder(account_id: str):
            class Client:
                async def complete(self, **kwargs):
                    calls.append(kwargs)
                    return completion(
                        f"answer from {kwargs['model']}", f"thread-{len(calls)}"
                    )

            return Client()

        pool = FakePool(responder)
        service = build_service(pool)

        first_input = [{"type": "message", "role": "user", "content": "first"}]
        await handle_openai_responses(
            service,
            {"model": "fable-5", "input": first_input},
            "turn-1",
            conversation_key="codex-thread",
        )
        second_input = [
            *first_input,
            {"type": "message", "role": "assistant", "content": "previous"},
            {"type": "message", "role": "user", "content": "use opus"},
        ]
        await handle_openai_responses(
            service,
            {"model": "opus-5", "input": second_input},
            "turn-2",
            conversation_key="codex-thread",
        )
        segment = await service.conversation_segments.get("codex-thread")

        self.assertEqual([call["model"] for call in calls], ["fable-5", "opus-5"])
        self.assertNotIn("thread_id", calls[1])
        self.assertEqual(pool.preferred, [None, None])
        self.assertIsNotNone(segment)
        self.assertEqual(segment.model, "opus-5")

    async def test_same_codex_turn_reuses_account_and_notion_thread(self) -> None:
        calls: list[dict] = []
        replies = ['{"tool":"update_plan","arguments":{"plan":[]}}', "finished"]

        def responder(_account_id: str):
            class Client:
                async def complete(self, **kwargs):
                    calls.append(kwargs)
                    return completion(replies.pop(0), "notion-thread")

            return Client()

        pool = FakePool(responder)
        service = build_service(pool)

        first = {
            "model": "fable-5",
            "input": [{"type": "message", "role": "user", "content": "task"}],
            "tools": [{"type": "function", "name": "update_plan"}],
            "client_metadata": {"turn_id": "codex-turn"},
        }
        await handle_openai_responses(service, first, "codex-turn")
        second = {
            **first,
            "input": [
                *first["input"],
                {
                    "type": "function_call", "name": "update_plan",
                    "call_id": "call-1", "arguments": "{}",
                },
                {
                    "type": "function_call_output", "call_id": "call-1",
                    "output": "Plan updated",
                },
            ],
        }
        response = await handle_openai_responses(service, second, "codex-turn")
        replay = await handle_openai_responses(service, second, "codex-turn")

        self.assertEqual(response["output"][0]["content"][0]["text"], "finished")
        self.assertEqual(replay["output"][0]["content"][0]["text"], "finished")
        self.assertEqual(len(calls), 2)
        self.assertEqual(pool.preferred, [None, "account-a"])
        self.assertIsNone(calls[0].get("thread_id"))
        self.assertEqual(calls[1]["thread_id"], "notion-thread")
        self.assertIn("Plan updated", calls[1]["prompt"])
        self.assertNotIn("Tool catalog", calls[1]["prompt"])

    async def test_conversation_continues_then_rotates_after_compaction(self) -> None:
        calls: list[tuple[str, dict]] = []

        def responder(account_id: str):
            class Client:
                async def complete(self, **kwargs):
                    calls.append((account_id, kwargs))
                    is_compaction = "handoff checkpoint" in kwargs["prompt"]
                    return completion(
                        "dense summary" if is_compaction else f"answer from {account_id}",
                        f"thread-{account_id}",
                    )

            return Client()

        pool = FakePool(responder, account_ids=["account-a", "account-b"])
        service = build_service(pool)

        first_input = [{"type": "message", "role": "user", "content": "first task"}]
        await handle_openai_responses(
            service,
            {"model": "fable-5", "input": first_input},
            "turn-1",
            conversation_key="codex-thread",
        )
        second_input = [
            *first_input,
            {"type": "message", "role": "assistant", "content": "previous answer"},
            {"type": "message", "role": "user", "content": "next request"},
        ]
        await handle_openai_responses(
            service,
            {"model": "fable-5", "input": second_input},
            "turn-2",
            conversation_key="codex-thread",
        )
        compacted = await handle_openai_compaction(
            service,
            {"model": "fable-5", "input": second_input},
            "compact-turn",
            "codex-thread",
        )
        final = await handle_openai_responses(
            service,
            {"model": "fable-5", "input": [
                compacted["output"][0],
                {"type": "message", "role": "user", "content": "after compact"},
            ]},
            "turn-3",
            conversation_key="codex-thread",
        )

        self.assertEqual(pool.preferred, [None, "account-a", "account-a", None])
        self.assertEqual(calls[1][0], "account-a")
        self.assertEqual(calls[1][1]["thread_id"], "thread-account-a")
        self.assertIn("next request", calls[1][1]["prompt"])
        self.assertNotIn("previous answer", calls[1][1]["prompt"])
        self.assertEqual(compacted["output"][0]["type"], "compaction")
        self.assertEqual(calls[-1][0], "account-b")
        self.assertNotIn("thread_id", calls[-1][1])
        self.assertIn("dense summary", calls[-1][1]["prompt"])
        self.assertEqual(final["output"][0]["content"][0]["text"], "answer from account-b")

    async def test_missing_pool_answers_503_instead_of_raising(self) -> None:
        service = build_service(None)
        response = await handle_openai_responses(
            service, {"model": "opus-5", "input": "hello"}, "turn-x"
        )
        self.assertEqual(response.status_code, 503)

    async def test_planner_refusal_is_corrected_inside_the_same_thread(self) -> None:
        prompts: list[str] = []
        replies = [
            "I don't have access to the file system.",
            '{"tool":"exec_command","arguments":{"cmd":"pwd"}}',
        ]

        def responder(_account_id: str):
            class Client:
                account_path = None

                async def complete(self, **kwargs):
                    prompts.append(kwargs["prompt"])
                    return completion(replies.pop(0), "notion-thread")

                async def _prepare_call(self, **_kwargs):  # pragma: no cover
                    raise AssertionError("images are not involved in this test")

            return Client()

        pool = FakePool(responder)
        service = build_service(pool)
        response = await handle_openai_responses(
            service,
            {
                "model": "opus-5",
                "input": [{"type": "message", "role": "user", "content": "where am I"}],
                "tools": [{"type": "function", "name": "exec_command"}],
            },
            "turn-correction",
        )

        self.assertEqual(len(prompts), 2)
        self.assertIn("not a valid planner recommendation", prompts[1])
        self.assertEqual(response["output"][0]["type"], "function_call")


if __name__ == "__main__":
    unittest.main()
