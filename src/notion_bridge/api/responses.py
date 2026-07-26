"""OpenAI Responses compatibility: the surface Codex actually speaks."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from notion_agent_cli.provider import NotionAgentClient

from .. import metrics
from ..diagnostics import correlation_id, log_event, set_log_context
from ..notion.images import (
    ImageInputError,
    complete_with_images,
    estimated_image_tokens,
    extract_response_images,
)
from ..planner.prompts import (
    planner_correction_prompt,
    responses_compaction_prompt,
    responses_incremental_prompt,
    responses_planner_prompt,
)
from ..planner.toolcalls import (
    extract_malformed_responses_tool,
    extract_responses_tool_call,
    extract_unavailable_responses_tool,
    looks_like_agent_refusal,
    responses_tool_catalog,
)
from ..service import BridgeService
from ..state.conversation_segments import (
    input_prefix_length,
    response_input_fingerprints,
)
from ..state.turn_affinity import (
    codex_conversation_key,
    codex_request_kind,
    codex_turn_key,
    response_input_count,
    response_input_fingerprint,
)
from . import errors
from .payloads import (
    compact_payload,
    encode_responses_event,
    reasoning_item,
    request_body_with_codex_metadata,
    responses_incremental_body,
    responses_item_events,
    responses_payload,
    responses_sse,
)

log = logging.getLogger("uvicorn.error.notion_bridge")
router = APIRouter()

STYLE: errors.Style = "openai"


def bridge(request: Request) -> BridgeService:
    return request.app.state.service


@router.post("/v1/responses")
async def openai_responses(request: Request):
    service = bridge(request)
    body = request_body_with_codex_metadata(await request.json(), request.headers)
    turn_key = codex_turn_key(body)
    conversation_key = codex_conversation_key(body)
    request_kind = codex_request_kind(body)
    requested_model = str(body.get("model") or service.settings.default_model).lower()
    set_log_context(
        model=requested_model,
        stream=bool(body.get("stream", False)),
        turn_id=correlation_id(turn_key),
        conversation_id=correlation_id(conversation_key),
        request_kind=request_kind,
    )
    raw_tools = body.get("tools")
    log_event(
        log,
        "request_details",
        tool_count=len(raw_tools) if isinstance(raw_tools, list) else 0,
    )
    if body.get("stream", False):
        return StreamingResponse(
            stream_openai_responses(
                service, body, turn_key, conversation_key, request_kind
            ),
            media_type="text/event-stream",
        )
    async with (
        service.conversation_segments.lock(conversation_key),
        service.turn_affinities.lock(turn_key),
    ):
        return await handle_openai_responses(
            service,
            body,
            turn_key,
            conversation_key=conversation_key,
            request_kind=request_kind,
        )


async def stream_openai_responses(
    service: BridgeService,
    body: dict[str, Any],
    turn_key: str | None,
    conversation_key: str | None,
    request_kind: str,
) -> AsyncIterator[bytes]:
    """Emit Responses SSE while Notion is still thinking.

    Codex shows nothing until the first event arrives, and Notion inference can
    take minutes. Streaming reasoning deltas plus a heartbeat is what keeps a
    long turn distinguishable from a hung one.
    """
    settings = service.settings
    response_id = f"resp_{uuid.uuid4().hex}"
    reasoning_id = f"rs_{uuid.uuid4().hex}"
    model = str(body.get("model") or settings.default_model).lower()
    created = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "error": None,
        "incomplete_details": None,
        "model": model,
        "output": [],
    }
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def on_thinking_delta(delta: str) -> None:
        if delta:
            await queue.put(delta)

    async def run_request():
        try:
            async with (
                service.conversation_segments.lock(conversation_key),
                service.turn_affinities.lock(turn_key),
            ):
                return await handle_openai_responses(
                    service,
                    {**body, "stream": False},
                    turn_key,
                    conversation_key=conversation_key,
                    request_kind=request_kind,
                    on_thinking_delta_async=on_thinking_delta,
                    response_id=response_id,
                )
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(run_request())
    sequence_number = 0
    started_at = time.monotonic()
    thinking = f"Notion {model} is working…"
    try:
        yield encode_responses_event(
            {"type": "response.created", "response": created}, sequence_number
        )
        sequence_number += 1
        yield encode_responses_event({
            "type": "response.output_item.added",
            "output_index": 0,
            "item": reasoning_item(reasoning_id),
        }, sequence_number)
        sequence_number += 1
        yield encode_responses_event({
            "type": "response.reasoning_summary_part.added",
            "item_id": reasoning_id,
            "output_index": 0,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": ""},
        }, sequence_number)
        sequence_number += 1
        yield encode_responses_event({
            "type": "response.reasoning_summary_text.delta",
            "item_id": reasoning_id,
            "output_index": 0,
            "summary_index": 0,
            "delta": thinking,
        }, sequence_number)
        sequence_number += 1
        while True:
            try:
                delta = await asyncio.wait_for(
                    queue.get(),
                    timeout=settings.reasoning_heartbeat_seconds,
                )
            except asyncio.TimeoutError:
                elapsed = round(time.monotonic() - started_at)
                delta = f"\nStill working… {elapsed}s"
            if delta is None:
                break
            thinking += delta
            yield encode_responses_event({
                "type": "response.reasoning_summary_text.delta",
                "item_id": reasoning_id,
                "output_index": 0,
                "summary_index": 0,
                "delta": delta,
            }, sequence_number)
            sequence_number += 1

        result = await task
        if isinstance(result, JSONResponse):
            payload = json.loads(result.body)
            error = payload.get("error") or {"message": "Notion request failed"}
            failed = {**created, "status": "failed", "error": error}
            yield encode_responses_event(
                {"type": "response.failed", "response": failed}, sequence_number
            )
            yield b"data: [DONE]\n\n"
            return

        summary = reasoning_item(reasoning_id, thinking)
        yield encode_responses_event({
            "type": "response.reasoning_summary_text.done",
            "item_id": reasoning_id,
            "output_index": 0,
            "summary_index": 0,
            "text": thinking,
        }, sequence_number)
        sequence_number += 1
        yield encode_responses_event({
            "type": "response.reasoning_summary_part.done",
            "item_id": reasoning_id,
            "output_index": 0,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": thinking},
        }, sequence_number)
        sequence_number += 1
        yield encode_responses_event({
            "type": "response.output_item.done",
            "output_index": 0,
            "item": summary,
        }, sequence_number)
        sequence_number += 1

        final_item = result["output"][0]
        for event in responses_item_events(final_item, 1):
            yield encode_responses_event(event, sequence_number)
            sequence_number += 1
        yield encode_responses_event(
            {"type": "response.completed",
             "response": {**result, "output": [summary, final_item]}},
            sequence_number,
        )
        yield b"data: [DONE]\n\n"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failed = {
            **created,
            "status": "failed",
            "error": {"message": str(exc), "type": type(exc).__name__},
        }
        yield encode_responses_event(
            {"type": "response.failed", "response": failed}, sequence_number
        )
        yield b"data: [DONE]\n\n"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def handle_openai_responses(
    service: BridgeService,
    body: dict[str, Any],
    turn_key: str | None,
    *,
    conversation_key: str | None = None,
    request_kind: str | None = None,
    on_thinking_delta_async: Callable[[str], Awaitable[None]] | None = None,
    response_id: str | None = None,
):
    settings = service.settings
    conversation_key = conversation_key or codex_conversation_key(body)
    request_kind = request_kind or codex_request_kind(body)
    is_compaction = request_kind == "compaction"
    requested_model = str(body.get("model") or settings.default_model).lower()
    try:
        model = service.resolve_model(requested_model)
    except ValueError as exc:
        return errors.invalid_request(STYLE, str(exc))
    log_event(
        log,
        "model_resolved",
        requested_model=requested_model,
        resolved_model=model,
        forced=bool(settings.forced_model),
    )
    pool = service.pool
    if pool is None or pool.size == 0:
        return errors.no_accounts(STYLE)
    raw_tools = body.get("tools") or []
    tools = [] if is_compaction else responses_tool_catalog(raw_tools)
    web_search_requested = any(
        isinstance(tool, dict)
        and tool.get("type") == "web_search"
        and tool.get("external_web_access", True) is not False
        for tool in raw_tools
    ) if isinstance(raw_tools, list) else False

    affinity = await service.turn_affinities.get(turn_key)
    segment = await service.conversation_segments.get(conversation_key)
    affinity_matches_model = affinity is not None and affinity.model == model
    log_event(
        log,
        "turn_affinity_checked",
        affinity=(
            "reused" if affinity_matches_model else
            "model_changed" if affinity is not None else
            "new"
        ),
        preferred_account_id=affinity.account_id if affinity_matches_model else None,
        notion_thread_id=(
            correlation_id(affinity.notion_thread_id) if affinity_matches_model else None
        ),
    )
    current_fingerprints = response_input_fingerprints(body)
    segment_prefix = (
        input_prefix_length(segment.input_fingerprints, current_fingerprints)
        if segment is not None else None
    )
    rollover_reason: str | None = None
    if segment is not None and segment.awaiting_compacted_history and not is_compaction:
        rollover_reason = "post_compaction"
    elif segment is not None and segment.model != model:
        rollover_reason = "model_changed"
    elif segment is not None and segment_prefix is None:
        rollover_reason = "history_rewritten"
    log_event(
        log,
        "conversation_segment_checked",
        state="new" if segment is None else "rollover" if rollover_reason else "continued",
        segment_index=segment.segment_index if segment is not None else 0,
        rollover_reason=rollover_reason,
        preferred_account_id=(
            segment.account_id if segment is not None and rollover_reason is None else None
        ),
        notion_thread_id=(
            correlation_id(segment.notion_thread_id)
            if segment is not None and rollover_reason is None else None
        ),
    )
    input_fingerprint = response_input_fingerprint(body)
    if (
        affinity_matches_model
        and affinity is not None
        and affinity.input_fingerprint == input_fingerprint
        and affinity.completion_text
    ):
        log_event(log, "response_cache_hit", account_id=affinity.account_id)
        metrics.registry.increment(
            "notion_bridge_response_cache_hits_total",
            help_text="Repeated identical Codex turns served without new inference.",
        )
        cached_response, cached_item = responses_payload(
            affinity.completion_text,
            requested_model,
            affinity.input_tokens,
            affinity.output_tokens,
            tools,
            response_id=response_id,
        )
        if not body.get("stream", False):
            return cached_response

        async def cached_stream():
            for event in responses_sse(cached_response, cached_item):
                yield event

        return StreamingResponse(cached_stream(), media_type="text/event-stream")

    anchor = None
    previous_input_count: int | None = None
    if rollover_reason is None:
        if affinity_matches_model and affinity is not None:
            anchor = affinity
            previous_input_count = affinity.input_count
        elif segment is not None and segment_prefix is not None:
            anchor = segment
            previous_input_count = segment_prefix
    incremental_body = (
        responses_incremental_body(body, previous_input_count)
        if previous_input_count is not None else None
    )
    full_prompt = (
        responses_compaction_prompt(body, continuing=False)
        if is_compaction
        else responses_planner_prompt(body, tools, settings.code_root)
    )
    incremental_prompt = None
    if anchor is not None:
        if is_compaction:
            incremental_prompt = responses_compaction_prompt(
                incremental_body or {"input": []}, continuing=True
            )
        elif incremental_body is not None:
            incremental_prompt = responses_incremental_prompt(incremental_body)
    full_images = None
    try:
        if incremental_prompt is None:
            full_images = extract_response_images(body)
            incremental_images = []
        else:
            incremental_images = (
                extract_response_images(incremental_body)
                if incremental_body is not None else []
            )
    except ImageInputError as exc:
        return errors.invalid_request(STYLE, str(exc))
    active_images = (
        incremental_images if incremental_prompt is not None else (full_images or [])
    )
    active_prompt = incremental_prompt or full_prompt
    log_event(
        log,
        "responses_context",
        mode="continuation" if incremental_prompt is not None else "full",
        input_items=response_input_count(body),
        delta_items=(response_input_count(incremental_body) if incremental_body else 0),
        image_count=len(active_images),
        image_bytes=sum(len(image.data) for image in active_images),
        estimated_prompt_tokens=(
            max(1, len(active_prompt) // 4) + estimated_image_tokens(active_images)
        ),
    )

    async def initial_completion(notion: NotionAgentClient):
        recovery_images = (
            full_images if full_images is not None else extract_response_images(body)
        )
        if recovery_images or on_thinking_delta_async is not None:
            return await complete_with_images(
                notion,
                prompt=full_prompt,
                images=recovery_images,
                model=model,
                web_search=web_search_requested,
                workspace_search=False,
                ask_mode=True,
                on_thinking_delta_async=on_thinking_delta_async,
            )
        return await notion.complete(
            prompt=full_prompt,
            model=model,
            web_search=web_search_requested,
            workspace_search=False,
            ask_mode=True,
        )

    async def continuation_completion(notion: NotionAgentClient):
        if anchor is None or incremental_prompt is None:
            return await initial_completion(notion)
        if incremental_images or on_thinking_delta_async is not None:
            return await complete_with_images(
                notion,
                prompt=incremental_prompt,
                images=incremental_images,
                model=model,
                web_search=web_search_requested,
                workspace_search=False,
                ask_mode=True,
                thread_id=anchor.notion_thread_id,
                on_thinking_delta_async=on_thinking_delta_async,
            )
        return await notion.complete(
            prompt=incremental_prompt,
            model=model,
            web_search=web_search_requested,
            workspace_search=False,
            ask_mode=True,
            thread_id=anchor.notion_thread_id,
        )

    inference_started = time.monotonic()
    try:
        async with pool.lease(
            preferred_account_id=(anchor.account_id if anchor is not None else None),
        ) as lease:
            can_continue = (
                anchor is not None
                and incremental_prompt is not None
                and lease.account_id == anchor.account_id
            )
            completion = await lease.run(
                continuation_completion if can_continue else initial_completion,
                retry_operation=initial_completion,
            )
            log_event(
                log,
                "notion_model_selected",
                resolved_model=model,
                notion_model=getattr(completion, "model", None),
                reported_notion_model=(
                    completion.raw.get("reported_notion_model")
                    if isinstance(getattr(completion, "raw", None), dict)
                    else None
                ),
            )
            completion = await _correct_planner_output(
                service,
                lease,
                completion,
                tools=tools,
                model=model,
                is_compaction=is_compaction,
                web_search_requested=web_search_requested,
                on_thinking_delta_async=on_thinking_delta_async,
                fallback=initial_completion,
            )
            await service.turn_affinities.put(
                turn_key,
                account_id=lease.account_id,
                notion_thread_id=completion.thread_id,
                input_count=response_input_count(body),
                input_fingerprint=input_fingerprint,
                completion_text=completion.text,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                model=model,
            )
            next_segment_index = (
                0 if segment is None else
                segment.segment_index + 1 if rollover_reason else
                segment.segment_index
            )
            await service.conversation_segments.put(
                conversation_key,
                account_id=lease.account_id,
                notion_thread_id=completion.thread_id,
                input_fingerprints=current_fingerprints,
                segment_index=next_segment_index,
                awaiting_compacted_history=is_compaction,
                turns=(segment.turns + 1 if segment is not None else 1),
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                model=model,
            )
            log_event(
                log,
                "turn_affinity_saved",
                account_id=lease.account_id,
                notion_thread_id=correlation_id(completion.thread_id),
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
            )
            log_event(
                log,
                "conversation_segment_saved",
                account_id=lease.account_id,
                notion_thread_id=correlation_id(completion.thread_id),
                segment_index=next_segment_index,
                compaction_completed=is_compaction,
            )
    except Exception as exc:
        metrics.record_inference(model, "error", time.monotonic() - inference_started)
        return errors.upstream_failure(STYLE, exc)
    metrics.record_inference(model, "ok", time.monotonic() - inference_started)
    metrics.record_tokens(
        model, completion.usage.input_tokens, completion.usage.output_tokens
    )
    response, item = responses_payload(
        completion.text,
        requested_model,
        completion.usage.input_tokens,
        completion.usage.output_tokens,
        tools,
        response_id=response_id,
    )
    if not body.get("stream", False):
        return response

    async def stream():
        for event in responses_sse(response, item):
            yield event

    return StreamingResponse(stream(), media_type="text/event-stream")


def _correction_call(
    *,
    prompt: str,
    model: str,
    web_search: bool,
    thread_id: str,
    on_thinking_delta_async: Callable[[str], Awaitable[None]] | None,
) -> Callable[[NotionAgentClient], Awaitable[Any]]:
    """Bind the correction arguments now, so a retry cannot capture a later loop
    iteration's values."""

    async def call(notion: NotionAgentClient):
        return await complete_with_images(
            notion,
            prompt=prompt,
            images=[],
            model=model,
            web_search=web_search,
            workspace_search=False,
            ask_mode=True,
            thread_id=thread_id,
            on_thinking_delta_async=on_thinking_delta_async,
        )

    return call


async def _correct_planner_output(
    service: BridgeService,
    lease,
    completion,
    *,
    tools: list[dict[str, Any]],
    model: str,
    is_compaction: bool,
    web_search_requested: bool,
    on_thinking_delta_async: Callable[[str], Awaitable[None]] | None,
    fallback: Callable[[NotionAgentClient], Awaitable[Any]],
):
    """Nudge the model back onto the planner protocol, a bounded number of times.

    Notion sometimes answers "I have no file system access" or invents a tool.
    Both are recoverable inside the same Notion thread, which is far cheaper
    than surfacing a broken turn to Codex.
    """
    if is_compaction or not tools:
        return completion
    attempts = service.settings.planner_correction_attempts
    for attempt in range(attempts):
        malformed_tool = extract_malformed_responses_tool(completion.text, tools)
        unavailable_tool = extract_unavailable_responses_tool(completion.text, tools)
        needs_correction = (
            looks_like_agent_refusal(completion.text)
            or malformed_tool is not None
            or unavailable_tool is not None
        )
        if extract_responses_tool_call(completion.text, tools) is not None:
            return completion
        if not needs_correction:
            return completion
        if attempt == attempts - 1:
            log_event(
                log,
                "planner_correction_exhausted",
                malformed_tool=malformed_tool,
                unavailable_tool=unavailable_tool,
            )
            return completion
        thread_id = completion.thread_id
        correction = planner_correction_prompt(
            unavailable_tool=unavailable_tool, malformed_tool=malformed_tool
        )
        log_event(
            log,
            "planner_correction_requested",
            attempt=attempt + 1,
            malformed_tool=malformed_tool,
            unavailable_tool=unavailable_tool,
        )
        metrics.registry.increment(
            "notion_bridge_planner_corrections_total",
            help_text="Retries issued when Notion broke the planner protocol.",
        )
        completion = await lease.run(
            _correction_call(
                prompt=correction,
                model=model,
                web_search=web_search_requested,
                thread_id=thread_id,
                on_thinking_delta_async=on_thinking_delta_async,
            ),
            retry_operation=fallback,
        )
    return completion


@router.post("/v1/responses/compact")
async def openai_responses_compact(request: Request):
    service = bridge(request)
    body = request_body_with_codex_metadata(await request.json(), request.headers)
    turn_key = codex_turn_key(body)
    conversation_key = codex_conversation_key(body)
    set_log_context(
        model=str(body.get("model") or service.settings.default_model).lower(),
        stream=False,
        turn_id=correlation_id(turn_key),
        conversation_id=correlation_id(conversation_key),
        request_kind="compaction",
    )
    async with (
        service.conversation_segments.lock(conversation_key),
        service.turn_affinities.lock(turn_key),
    ):
        return await handle_openai_compaction(
            service, body, turn_key, conversation_key
        )


async def handle_openai_compaction(
    service: BridgeService,
    body: dict[str, Any],
    turn_key: str | None,
    conversation_key: str | None,
):
    settings = service.settings
    requested_model = str(body.get("model") or settings.default_model).lower()
    try:
        model = service.resolve_model(requested_model)
    except ValueError as exc:
        return errors.invalid_request(STYLE, str(exc))
    pool = service.pool
    if pool is None or pool.size == 0:
        return errors.no_accounts(STYLE)
    input_fingerprint = response_input_fingerprint(body)
    affinity = await service.turn_affinities.get(turn_key)
    if (
        affinity is not None
        and affinity.model == model
        and affinity.input_fingerprint == input_fingerprint
    ):
        log_event(log, "compaction_cache_hit", account_id=affinity.account_id)
        return compact_payload(affinity.completion_text, turn_key)

    segment = await service.conversation_segments.get(conversation_key)
    current_fingerprints = response_input_fingerprints(body)
    prefix = (
        input_prefix_length(segment.input_fingerprints, current_fingerprints)
        if segment is not None else None
    )
    continuing = segment is not None and segment.model == model and prefix is not None
    incremental_body = (
        responses_incremental_body(body, prefix)
        if continuing and prefix is not None else None
    )
    compact_source = (incremental_body or {"input": []}) if continuing else body
    prompt = responses_compaction_prompt(compact_source, continuing=continuing)
    next_segment_index = (
        0 if segment is None else segment.segment_index + (0 if continuing else 1)
    )
    log_event(
        log,
        "compaction_started",
        mode="continuation" if continuing else "full",
        preferred_account_id=segment.account_id if continuing else None,
        segment_index=next_segment_index,
        input_items=response_input_count(body),
        estimated_prompt_tokens=max(1, len(prompt) // 4),
    )

    async def initial_completion(notion: NotionAgentClient):
        return await notion.complete(
            prompt=responses_compaction_prompt(body, continuing=False),
            model=model,
            web_search=False,
            workspace_search=False,
            ask_mode=True,
        )

    async def continuation_completion(notion: NotionAgentClient):
        if segment is None:
            return await initial_completion(notion)
        return await notion.complete(
            prompt=prompt,
            model=model,
            web_search=False,
            workspace_search=False,
            ask_mode=True,
            thread_id=segment.notion_thread_id,
        )

    try:
        async with pool.lease(
            preferred_account_id=segment.account_id if continuing else None,
        ) as lease:
            can_continue = (
                continuing and segment is not None
                and lease.account_id == segment.account_id
            )
            completion = await lease.run(
                continuation_completion if can_continue else initial_completion,
                retry_operation=initial_completion,
            )
            await service.turn_affinities.put(
                turn_key,
                account_id=lease.account_id,
                notion_thread_id=completion.thread_id,
                input_count=response_input_count(body),
                input_fingerprint=input_fingerprint,
                completion_text=completion.text,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                model=model,
            )
            await service.conversation_segments.put(
                conversation_key,
                account_id=lease.account_id,
                notion_thread_id=completion.thread_id,
                input_fingerprints=current_fingerprints,
                segment_index=next_segment_index,
                awaiting_compacted_history=True,
                turns=(segment.turns + 1 if segment is not None else 1),
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                model=model,
            )
            log_event(
                log,
                "compaction_completed",
                account_id=lease.account_id,
                notion_thread_id=correlation_id(completion.thread_id),
                segment_index=next_segment_index,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
            )
    except Exception as exc:
        return errors.upstream_failure(STYLE, exc, event="compaction_failed")
    return compact_payload(completion.text, turn_key)
