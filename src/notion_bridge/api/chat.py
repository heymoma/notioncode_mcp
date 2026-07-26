"""OpenAI Chat Completions compatibility, used by OpenCode and generic clients."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .. import metrics
from ..diagnostics import exception_fields, log_event, set_log_context
from ..planner.loop import complete_chat_turn
from ..planner.prompts import build_chat_prompt
from ..planner.toolcalls import extract_chat_tool_call
from ..service import BridgeService
from . import errors
from .payloads import chat_chunk, chat_completion

log = logging.getLogger("uvicorn.error.notion_bridge")
router = APIRouter()

STYLE: errors.Style = "openai"


def bridge(request: Request) -> BridgeService:
    return request.app.state.service


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    service = bridge(request)
    body = await request.json()
    return await handle_chat_completions(service, body)


async def handle_chat_completions(service: BridgeService, body: dict[str, Any]):
    settings = service.settings
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    requested_model = str(body.get("model") or settings.default_model).lower()
    set_log_context(model=requested_model, stream=bool(body.get("stream", False)))
    log_event(
        log,
        "request_details",
        tool_count=len(tools) if isinstance(tools, list) else 0,
    )
    try:
        model = service.resolve_model(requested_model)
    except ValueError as exc:
        return errors.invalid_request(STYLE, str(exc))
    planner_mode = bool(tools) and not settings.workflow_id
    system, prompt = build_chat_prompt(
        messages, tools, include_tool_catalog=not settings.workflow_id
    )
    if not prompt:
        return errors.invalid_request(STYLE, "messages must contain text")
    pool = service.pool
    if pool is None or pool.size == 0:
        return errors.no_accounts(STYLE)

    if not body.get("stream"):
        started_at = time.monotonic()
        try:
            async with pool.lease() as lease:
                response = await complete_chat_turn(
                    lease,
                    service.runtime_tools,
                    settings,
                    prompt=prompt,
                    system=system,
                    model=model,
                    planner_mode=planner_mode,
                )
        except Exception as exc:
            metrics.record_inference(model, "error", time.monotonic() - started_at)
            return errors.upstream_failure(STYLE, exc)
        metrics.record_inference(model, "ok", time.monotonic() - started_at)
        metrics.record_tokens(
            model, response.usage.input_tokens, response.usage.output_tokens
        )
        return chat_completion(
            response.text,
            requested_model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            extract_chat_tool_call(response.text, tools),
        )

    return StreamingResponse(
        _chat_stream(
            service,
            prompt=prompt,
            system=system,
            model=model,
            requested_model=requested_model,
            tools=tools,
            planner_mode=planner_mode,
        ),
        media_type="text/event-stream",
    )


async def _chat_stream(
    service: BridgeService,
    *,
    prompt: str,
    system: str | None,
    model: str,
    requested_model: str,
    tools: list[dict[str, Any]],
    planner_mode: bool,
):
    pool = service.pool
    assert pool is not None
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    full_text: list[str] = []

    async def run() -> None:
        started_at = time.monotonic()
        try:
            async with pool.lease() as lease:
                response = await complete_chat_turn(
                    lease,
                    service.runtime_tools,
                    service.settings,
                    prompt=prompt,
                    system=system,
                    model=model,
                    planner_mode=planner_mode,
                )
                full_text.append(response.text)
                metrics.record_inference(model, "ok", time.monotonic() - started_at)
                metrics.record_tokens(
                    model, response.usage.input_tokens, response.usage.output_tokens
                )
                if not tools:
                    await queue.put(response.text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics.record_inference(model, "error", time.monotonic() - started_at)
            log_event(
                log,
                "stream_request_failed",
                level=logging.ERROR,
                **exception_fields(exc),
            )
            await queue.put(f"\n[Notion bridge error: {exc}]\n")
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    try:
        while True:
            value = await queue.get()
            if value is None:
                break
            yield _sse(chat_chunk(value, requested_model))
        tool_call = extract_chat_tool_call("".join(full_text), tools)
        if tool_call:
            yield _sse(chat_chunk("", requested_model, None, tool_call))
        elif tools and full_text:
            yield _sse(chat_chunk("".join(full_text), requested_model))
        yield _sse(
            chat_chunk("", requested_model, "tool_calls" if tool_call else "stop")
        )
        yield b"data: [DONE]\n\n"
    finally:
        # A client that disconnects mid-turn used to leave this task running,
        # so its Notion account stayed leased until the inference timeout.
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
