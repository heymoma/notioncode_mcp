"""Wire-format builders for the OpenAI and Anthropic compatible endpoints."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

from ..planner.prompts import CODEX_SUMMARY_PREFIX
from ..planner.toolcalls import extract_anthropic_tool_call, extract_responses_tool_call

# -- request normalisation -------------------------------------------------


def request_body_with_codex_metadata(
    body: dict[str, Any],
    headers: Any,
) -> dict[str, Any]:
    """Fold Codex's turn/session headers into `client_metadata`."""
    encoded = headers.get("x-codex-turn-metadata")
    session_id = headers.get("session-id")
    thread_id = headers.get("thread-id")
    if not encoded and not session_id and not thread_id:
        return body
    result = dict(body)
    current_metadata = body.get("client_metadata")
    metadata = dict(current_metadata) if isinstance(current_metadata, dict) else {}
    if encoded:
        metadata.setdefault("x-codex-turn-metadata", encoded)
    if session_id:
        metadata.setdefault("session_id", session_id)
    if thread_id:
        metadata.setdefault("thread_id", thread_id)
    result["client_metadata"] = metadata
    return result


def responses_incremental_body(
    body: dict[str, Any],
    previous_input_count: int,
) -> dict[str, Any] | None:
    """The suffix of a Responses request that Codex has not sent to Notion yet."""
    request_input = body.get("input")
    if not isinstance(request_input, list) or previous_input_count > len(request_input):
        return None
    delta = request_input[previous_input_count:]
    # The assistant-side call is already present in the Notion thread as the
    # planner's previous JSON response. Only send its result and genuinely new
    # messages when Codex resumes the same turn.
    delta = [
        item for item in delta
        if not isinstance(item, dict)
        or (
            item.get("type") not in {"function_call", "custom_tool_call"}
            and not (
                item.get("type", "message") == "message"
                and item.get("role") == "assistant"
            )
        )
    ]
    if not delta:
        return None
    return {**body, "input": delta, "tools": []}


# -- OpenAI Responses ------------------------------------------------------


def compact_payload(text: str, turn_key: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "compaction",
        "encrypted_content": f"{CODEX_SUMMARY_PREFIX}\n{text}",
    }
    if turn_key:
        item["internal_chat_message_metadata_passthrough"] = {"turn_id": turn_key}
    return {"output": [item]}


def responses_payload(
    text: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    tools: list[dict[str, Any]],
    response_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    response_id = response_id or f"resp_{uuid.uuid4().hex}"
    call = extract_responses_tool_call(text, tools)
    if call:
        tool_type, name, arguments = call
        call_id = f"call_{uuid.uuid4().hex}"
        if tool_type == "custom":
            item = {
                "type": "custom_tool_call",
                "id": f"ctc_{uuid.uuid4().hex}",
                "call_id": call_id,
                "name": name,
                "input": arguments,
            }
        else:
            item = {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
        end_turn = False
    else:
        item = {
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex}",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        end_turn = True
    usage = {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }
    response = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": model,
        "output": [item],
        "usage": usage,
        "end_turn": end_turn,
    }
    return response, item


def responses_item_events(
    item: dict[str, Any], output_index: int
) -> list[dict[str, Any]]:
    item_started = dict(item)
    events: list[dict[str, Any]] = []
    if item["type"] == "message":
        part = item["content"][0]
        item_started["content"] = []
        empty_part = {**part, "text": ""}
        events.extend([
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item_started,
            },
            {
                "type": "response.content_part.added", "item_id": item["id"],
                "output_index": output_index, "content_index": 0, "part": empty_part,
            },
            {
                "type": "response.output_text.delta", "item_id": item["id"],
                "output_index": output_index, "content_index": 0, "delta": part["text"],
            },
            {
                "type": "response.output_text.done", "item_id": item["id"],
                "output_index": output_index, "content_index": 0, "text": part["text"],
            },
            {
                "type": "response.content_part.done", "item_id": item["id"],
                "output_index": output_index, "content_index": 0, "part": part,
            },
        ])
    else:
        if item["type"] == "function_call":
            item_started["arguments"] = ""
        elif item["type"] == "custom_tool_call":
            item_started["input"] = ""
        events.append({
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": item_started,
        })
    events.append({
        "type": "response.output_item.done",
        "output_index": output_index,
        "item": item,
    })
    return events


def encode_responses_event(event: dict[str, Any], sequence_number: int) -> bytes:
    event["sequence_number"] = sequence_number
    return (
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    ).encode()


def responses_sse(
    response: dict[str, Any], item: dict[str, Any]
) -> Iterator[bytes]:
    created = {**response, "status": "in_progress", "output": []}
    events: list[dict[str, Any]] = [{"type": "response.created", "response": created}]
    events.extend(responses_item_events(item, 0))
    events.append({"type": "response.completed", "response": response})
    for sequence_number, event in enumerate(events):
        yield encode_responses_event(event, sequence_number)
    yield b"data: [DONE]\n\n"


def reasoning_item(reasoning_id: str, text: str | None = None) -> dict[str, Any]:
    return {
        "type": "reasoning",
        "id": reasoning_id,
        "summary": [{"type": "summary_text", "text": text}] if text is not None else [],
        "content": [],
        "encrypted_content": None,
    }


# -- Anthropic Messages ----------------------------------------------------


def anthropic_message(
    text: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    tool_call = extract_anthropic_tool_call(text, tools)
    if tool_call:
        name, arguments = tool_call
        content = [{
            "type": "tool_use",
            "id": f"toolu_{uuid.uuid4().hex}",
            "name": name,
            "input": arguments,
        }]
        stop_reason = "tool_use"
    else:
        content = [{"type": "text", "text": text}]
        stop_reason = "end_turn"
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def anthropic_sse_events(
    message: dict[str, Any]
) -> Iterator[tuple[str, dict[str, Any]]]:
    start = {**message, "content": [], "stop_reason": None, "stop_sequence": None}
    yield "message_start", {"type": "message_start", "message": start}
    block = message["content"][0]
    if block["type"] == "text":
        yield "content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
        yield "content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": block["text"]},
        }
    else:
        yield "content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {
                "type": "tool_use", "id": block["id"],
                "name": block["name"], "input": {},
            },
        }
        yield "content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {
                "type": "input_json_delta",
                "partial_json": json.dumps(block["input"], ensure_ascii=False),
            },
        }
    yield "content_block_stop", {"type": "content_block_stop", "index": 0}
    yield "message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": message["usage"]["output_tokens"]},
    }
    yield "message_stop", {"type": "message_stop"}


def anthropic_sse_stream(message: dict[str, Any]) -> Iterator[bytes]:
    for event_name, payload in anthropic_sse_events(message):
        yield (
            f"event: {event_name}\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        ).encode()


# -- OpenAI Chat Completions ----------------------------------------------


def chat_chunk(
    text: str,
    model: str,
    finish_reason: str | None = None,
    tool_call: tuple[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    delta: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_call:
        name, arguments = tool_call
        delta = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "index": 0,
                "id": f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }],
        }
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def chat_completion(
    text: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    tool_call: tuple[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    if tool_call:
        name, arguments = tool_call
        message: dict[str, Any] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }],
        }
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": text}
        finish_reason = "stop"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
