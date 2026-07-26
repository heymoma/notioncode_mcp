"""Anthropic Messages compatibility, used by Claude Code."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from notion_agent_cli.provider import NotionAgentClient

from .. import metrics
from ..diagnostics import log_event, set_log_context
from ..planner.prompts import (
    ANTHROPIC_CORRECTION_PROMPT,
    anthropic_planner_prompt,
)
from ..planner.toolcalls import extract_anthropic_tool_call, looks_like_agent_refusal
from ..service import BridgeService
from . import errors
from .payloads import anthropic_message, anthropic_sse_stream

log = logging.getLogger("uvicorn.error.notion_bridge")
router = APIRouter()

STYLE: errors.Style = "anthropic"


def bridge(request: Request) -> BridgeService:
    return request.app.state.service


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request) -> dict[str, int]:
    """Approximate token count.

    Notion exposes no counting endpoint, so this is a deliberate estimate of
    four characters per token over the serialised request.
    """
    body = await request.json()
    serialized = json.dumps(body, ensure_ascii=False)
    return {"input_tokens": max(1, len(serialized) // 4)}


@router.post("/v1/messages")
async def anthropic_messages(request: Request):
    service = bridge(request)
    body = await request.json()
    return await handle_anthropic_messages(service, body)


async def handle_anthropic_messages(service: BridgeService, body: dict[str, Any]):
    settings = service.settings
    requested_model = str(body.get("model") or settings.default_model).lower()
    tools = body.get("tools") or []
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
    pool = service.pool
    if pool is None or pool.size == 0:
        return errors.no_accounts(STYLE)
    prompt = anthropic_planner_prompt(body, settings.code_root)

    async def initial_completion(notion: NotionAgentClient):
        return await notion.complete(
            prompt=prompt,
            model=model,
            web_search=False,
            workspace_search=False,
            ask_mode=True,
        )

    started_at = time.monotonic()
    try:
        async with pool.lease() as lease:
            response = await lease.run(initial_completion)
            if (
                tools
                and extract_anthropic_tool_call(response.text, tools) is None
                and looks_like_agent_refusal(response.text)
            ):
                thread_id = response.thread_id
                log_event(log, "planner_correction_requested", attempt=1)
                response = await lease.run(
                    lambda notion: notion.complete(
                        prompt=ANTHROPIC_CORRECTION_PROMPT,
                        model=model,
                        web_search=False,
                        workspace_search=False,
                        ask_mode=True,
                        thread_id=thread_id,
                    ),
                    retry_operation=initial_completion,
                )
    except Exception as exc:
        metrics.record_inference(model, "error", time.monotonic() - started_at)
        return errors.upstream_failure(STYLE, exc)
    metrics.record_inference(model, "ok", time.monotonic() - started_at)
    metrics.record_tokens(
        model, response.usage.input_tokens, response.usage.output_tokens
    )
    message = anthropic_message(
        response.text,
        requested_model,
        response.usage.input_tokens,
        response.usage.output_tokens,
        tools,
    )
    if not body.get("stream"):
        return message

    async def stream():
        for event in anthropic_sse_stream(message):
            yield event

    return StreamingResponse(stream(), media_type="text/event-stream")
