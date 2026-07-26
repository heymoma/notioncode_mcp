"""The planner/operator action loop used by the Chat Completions surface.

OpenCode and plain OpenAI clients have no Codex runtime behind them, so the
bridge itself drives the loop: ask Notion for one action, run it against the
coding-tools MCP, feed the result back, repeat until the model says it is done.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from notion_agent_cli.types import ChatResponse

from ..accounts.pool import AccountLease
from ..diagnostics import log_event
from ..settings import Settings
from .prompts import chat_planner_prompt
from .runtime_tools import LIST_TOOLS, RuntimeToolClient
from .toolcalls import (
    extract_planner_action,
    extract_workflow_call,
    planner_action_to_tool,
)

log = logging.getLogger("uvicorn.error.notion_bridge")

WORKFLOW_MAX_STEPS = 12


def completion_call(**arguments: Any) -> Callable[[Any], Awaitable[ChatResponse]]:
    """Bind Notion completion arguments at call-construction time.

    The loops below build a primary and a failover call per iteration. Closing
    over the loop variables directly would let a retry read a later iteration's
    prompt or thread ID.
    """

    async def call(notion: Any) -> ChatResponse:
        return await notion.complete(**arguments)

    return call


class PlannerLoopExceeded(RuntimeError):
    """The model kept asking for actions past the configured limit."""


async def run_planner_loop(
    lease: AccountLease,
    runtime_tools: RuntimeToolClient,
    settings: Settings,
    *,
    prompt: str,
    system: str | None,
    model: str,
) -> ChatResponse:
    """Drive the JSON one-action protocol until the model returns `final`."""
    first_prompt = chat_planner_prompt(prompt, system, settings.code_root)
    response = await lease.run(
        completion_call(
            prompt=first_prompt,
            model=model,
            web_search=False,
            workspace_search=False,
            ask_mode=True,
        )
    )
    completed_actions: list[str] = []
    for step in range(settings.planner_max_steps):
        action = extract_planner_action(response.text)
        if not action:
            return response
        if action.get("action") == "final":
            response.text = str(action.get("message", "Task completed."))
            return response
        mapped = planner_action_to_tool(action)
        if mapped is None:
            name = str(action.get("action"))
            tool_result = json.dumps(
                {"isError": True, "error": "Unknown action"}, ensure_ascii=False
            )
        else:
            name, arguments = mapped
            tool_result = await runtime_tools.call_or_error_json(name, arguments)
        log_event(log, "planner_action_executed", step=step + 1, tool=name)
        completed_actions.append(f"Action: {name}\nResult:\n{tool_result}")
        continuation_prompt = (
            "The local operator executed your recommendation.\n"
            f"{completed_actions[-1]}\n\n"
            "Recommend exactly one next action using the same JSON-only format. "
            "Use final only after the original task is complete and verified."
        )
        recovery_task = (
            f"{prompt}\n\n"
            "A previous Notion account failed after the local operator had already "
            "completed the actions below. Continue from this state and do not repeat "
            "them.\n\n" + "\n\n".join(completed_actions)
        )
        thread_id = response.thread_id
        response = await lease.run(
            completion_call(
                prompt=continuation_prompt,
                model=model,
                web_search=False,
                workspace_search=False,
                ask_mode=True,
                thread_id=thread_id,
            ),
            retry_operation=completion_call(
                prompt=chat_planner_prompt(recovery_task, system, settings.code_root),
                model=model,
                web_search=False,
                workspace_search=False,
                ask_mode=True,
            ),
        )
    raise PlannerLoopExceeded(
        f"The planner exceeded {settings.planner_max_steps} actions"
    )


async def run_workflow_loop(
    lease: AccountLease,
    runtime_tools: RuntimeToolClient,
    settings: Settings,
    *,
    prompt: str,
    system: str | None,
    model: str,
) -> ChatResponse:
    """Variant for a Notion custom agent, which emits its own function JSON."""
    workflow_id = settings.workflow_id
    response = await lease.run(
        completion_call(
            prompt=prompt,
            system=system,
            model=model,
            web_search=False,
            workspace_search=True,
            ask_mode=False,
            workflow_id=workflow_id,
        )
    )
    completed_tools: list[str] = []
    for step in range(WORKFLOW_MAX_STEPS):
        tool_call = extract_workflow_call(response.text)
        if not tool_call:
            return response
        name, arguments = tool_call
        tool_result = await runtime_tools.call_or_error_json(name, arguments)
        log_event(log, "workflow_tool_executed", step=step + 1, tool=name)
        completed_tools.append(f"Tool: {name}\nResult:\n{tool_result}")
        continuation_prompt = (
            "The requested runtime tool has completed.\n"
            f"{completed_tools[-1]}\n\n"
            "Continue the task. If another runtime tool is needed, emit the same "
            "function JSON; otherwise provide the final answer to the user."
        )
        recovery_prompt = (
            f"Original task:\n{prompt}\n\n"
            "Continue the task on a new account. The runtime tools below already "
            "completed; do not repeat them.\n\n" + "\n\n".join(completed_tools)
        )
        thread_id = response.thread_id
        response = await lease.run(
            completion_call(
                prompt=continuation_prompt,
                model=model,
                ask_mode=False,
                workflow_id=workflow_id,
                thread_id=thread_id,
            ),
            retry_operation=completion_call(
                prompt=recovery_prompt,
                system=system,
                model=model,
                web_search=False,
                workspace_search=True,
                ask_mode=False,
                workflow_id=workflow_id,
            ),
        )
    raise PlannerLoopExceeded(
        f"The agent exceeded {WORKFLOW_MAX_STEPS} runtime tool calls"
    )


async def complete_chat_turn(
    lease: AccountLease,
    runtime_tools: RuntimeToolClient,
    settings: Settings,
    *,
    prompt: str,
    system: str | None,
    model: str,
    planner_mode: bool,
) -> ChatResponse:
    """Single entry point for the three Chat Completions execution modes."""
    if settings.workflow_id:
        return await run_workflow_loop(
            lease, runtime_tools, settings,
            prompt=prompt, system=system, model=model,
        )
    if planner_mode:
        return await run_planner_loop(
            lease, runtime_tools, settings,
            prompt=prompt, system=system, model=model,
        )
    return await lease.run(
        completion_call(
            prompt=prompt,
            system=system,
            model=model,
            web_search=False,
            workspace_search=True,
            ask_mode=True,
        )
    )


async def list_runtime_tools(runtime_tools: RuntimeToolClient) -> str:
    return await runtime_tools.call(LIST_TOOLS, {})
