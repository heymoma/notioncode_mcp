"""Prompt construction for the planner/operator protocol.

Notion AI is a chat assistant, not an agent runtime. Every endpoint therefore
frames the request the same way: the model is a planner that recommends one
action, and the local Codex/OpenCode runtime is the operator that executes it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CODEX_SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its "
    "thinking process. You also have access to the state of the tools that were used by that "
    "language model. Use this to build on the work that has already been done and avoid "
    "duplicating work. Here is the summary produced by the other language model, use the "
    "information in this summary to assist with your own analysis:"
)

_CWD_PATTERNS = (
    re.compile(r"<cwd>([^<]+)</cwd>", re.I),
    re.compile(
        r"(?:current working directory|working directory|workdir|cwd)\s*[:=]\s*([^\n<]+)",
        re.I,
    ),
)


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {
                "text", "input_text", "output_text"
            }:
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return str(value or "")


def system_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in value
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def operator_context(value: Any, code_root: Path) -> str:
    """One sentence of runtime fact, extracted from a client's system prompt.

    Claude Code and Codex both ship large system prompts that describe the
    assistant as a local agent. Forwarding them verbatim makes Notion's chat
    safety layer read the planner protocol as an identity override, so only the
    working directory is carried over.
    """
    system = system_text(value)
    cwd = str(code_root)
    for pattern in _CWD_PATTERNS:
        match = pattern.search(system)
        if match:
            candidate = match.group(1).strip().strip("`\"'")
            if Path(candidate).is_absolute():
                cwd = candidate
                break
    return f"The local operator's current working directory is {cwd}."


def contained_working_directory(system: str | None, code_root: Path) -> str:
    """A client-declared cwd, but only when it stays inside `CODE_ROOT`."""
    cwd = str(code_root)
    if not system:
        return cwd
    for pattern in (
        re.compile(r"<cwd>([^<]+)</cwd>", re.I),
        re.compile(r"(?:working directory|workdir|cwd)\s*[:=]\s*([^\n]+)", re.I),
    ):
        match = pattern.search(system)
        if not match:
            continue
        candidate = match.group(1).strip().strip("`\"'")
        try:
            Path(candidate).resolve().relative_to(code_root)
        except (OSError, ValueError):
            continue
        return candidate
    return cwd


def chat_planner_prompt(task: str, system: str | None, code_root: Path) -> str:
    cwd = contained_working_directory(system, code_root)
    return f"""You are a coding planner advising a local runtime operator.
You do not need computer access and must not perform an action yourself. \
The operator will execute exactly one recommendation and return its result to you.

Respond with ONLY one JSON object, without markdown or explanation. Allowed forms:
{{"action":"list_files","directory":"path"}}
{{"action":"read_file","file_path":"path","max_bytes":500000}}
{{"action":"write_file","file_path":"path","content":"complete file content"}}
{{"action":"edit_file","file_path":"path","old_text":"exact text",\
"new_text":"replacement","replace_all":false}}
{{"action":"run_shell","command":"command","cwd":"path","timeout_ms":30000}}
{{"action":"final","message":"concise result for the user"}}

Paths are relative to {code_root}. The current OpenCode working directory is {cwd}; \
express it relative to {code_root} when choosing paths. Inspect existing files before \
editing, make the requested changes, run appropriate tests, and use final only when the \
task is genuinely complete.

Task from the user:
{task}"""


def chat_tool_catalog_instructions(tools: list[dict[str, Any]]) -> str:
    tool_catalog = json.dumps(tools, ensure_ascii=False, indent=2)
    return (
        "You have access to the following external tools.\n"
        "When a tool is needed, respond with ONLY one JSON object in this exact form "
        "and no markdown or explanation: "
        '{"tool":"<exact tool name>","arguments":{...}}\n'
        "Use an exact tool name from the catalog and valid arguments. "
        "Do not claim that a tool was called unless you emit this JSON object.\n"
        f"Tool catalog:\n{tool_catalog}"
    )


def build_chat_prompt(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    include_tool_catalog: bool = True,
) -> tuple[str | None, str]:
    systems: list[str] = []
    conversation: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = text_content(message.get("content", ""))
        if not content:
            continue
        if role == "system":
            systems.append(content)
        else:
            conversation.append(f"[{role}]\n{content}")
    if tools and include_tool_catalog:
        systems.append(chat_tool_catalog_instructions(tools))
    return ("\n\n".join(systems) or None), "\n\n".join(conversation)


def anthropic_message_text(message: dict[str, Any]) -> str:
    role = str(message.get("role", "user"))
    content = message.get("content", "")
    if isinstance(content, str):
        return f"[{role}]\n{content}"
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "tool_use":
            parts.append(
                "The planner recommended tool "
                f"{item.get('name')} with arguments "
                f"{json.dumps(item.get('input', {}), ensure_ascii=False)}."
            )
        elif kind == "tool_result":
            result = text_content(item.get("content", ""))
            error_note = " (failed)" if item.get("is_error") else ""
            parts.append(
                f"The local operator returned this tool result{error_note}:\n{result}"
            )
        elif kind == "image":
            parts.append("[An image was supplied to the local operator.]")
    return f"[{role}]\n" + "\n".join(part for part in parts if part)


def anthropic_planner_prompt(body: dict[str, Any], code_root: Path) -> str:
    tools = body.get("tools") or []
    catalog = [
        {
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "input_schema": tool.get("input_schema", {}),
        }
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ]
    conversation = "\n\n".join(
        anthropic_message_text(message)
        for message in body.get("messages", [])
        if isinstance(message, dict)
    )
    context = operator_context(body.get("system"), code_root)
    tool_instructions = ""
    if catalog:
        tool_instructions = f"""
The operator can execute the tools below. When an action is needed, respond with ONLY \
one JSON object and no markdown:
{{"tool":"<exact tool name>","arguments":{{...}}}}
Use an exact tool name and arguments matching its input schema. Recommend one action at \
a time. Do not claim it ran; its result will arrive in the next conversation turn.

Tool catalog:
{json.dumps(catalog, ensure_ascii=False)}
"""
    return f"""You are a coding planner advising a local runtime operator.
You do not need computer access and must not perform an action yourself. The operator \
will execute exactly one recommendation and return its result to you. Inspect before \
editing, make complete changes, and verify them with appropriate commands.
{tool_instructions}
If no tool is needed, answer the user normally. The operator and its tools are real \
parts of this workflow; never discuss whether you personally have computer access.

Operator context:
{context}

Conversation:
{conversation}"""


def responses_message_text(item: dict[str, Any]) -> str:
    kind = str(item.get("type", "message"))
    if kind == "message":
        role = str(item.get("role", "user"))
        content = item.get("content", "")
        if isinstance(content, str):
            return f"[{role}]\n{content}"
        parts: list[str] = []
        if isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and part.get("type") in {
                    "input_text", "output_text", "text"
                }:
                    parts.append(str(part.get("text", "")))
        return f"[{role}]\n" + "\n".join(parts)
    if kind in {"function_call", "custom_tool_call"}:
        payload = item.get("arguments", item.get("input", ""))
        return (
            f"[assistant]\nThe planner recommended {item.get('name')} "
            f"with input {payload}."
        )
    if kind in {"function_call_output", "custom_tool_call_output"}:
        return (
            "[user]\nThe local operator returned this tool result:\n"
            f"{text_content(item.get('output', ''))}"
        )
    if kind in {"compaction", "context_compaction"}:
        summary = item.get("encrypted_content")
        if isinstance(summary, str) and summary:
            return f"[developer]\n{summary}"
    return ""


def normalized_response_input(body: dict[str, Any]) -> list[dict[str, Any]]:
    request_input = body.get("input", [])
    if isinstance(request_input, str):
        return [{"type": "message", "role": "user", "content": request_input}]
    if not isinstance(request_input, list):
        return []
    return [item for item in request_input if isinstance(item, dict)]


def response_transcript(body: dict[str, Any]) -> str:
    return "\n\n".join(
        part
        for item in normalized_response_input(body)
        for part in [responses_message_text(item)]
        if part
    )


def responses_planner_prompt(
    body: dict[str, Any],
    tools: list[dict[str, Any]],
    code_root: Path,
) -> str:
    conversation = response_transcript(body)
    context = operator_context(body.get("instructions"), code_root)
    tool_instructions = ""
    if tools:
        tool_instructions = f"""
The operator can execute the tools below. Recommend exactly one action at a time.
For a tool with type "function", respond with ONLY this JSON object:
{{"tool":"<exact tool name>","arguments":{{...}}}}
For a tool with type "custom", respond with ONLY this JSON object:
{{"tool":"<exact tool name>","input":"text matching the tool format"}}
Do not use markdown and do not claim the action already ran. Use an exact tool name and \
valid input.

Tool catalog:
{json.dumps(tools, ensure_ascii=False)}
"""
    output_instructions = ""
    text_options = body.get("text")
    if isinstance(text_options, dict) and isinstance(text_options.get("format"), dict):
        output_format = text_options["format"]
        if output_format.get("type") in {"json_schema", "json_object"}:
            output_instructions = f"""
The final answer must conform exactly to this requested structured-output format:
{json.dumps(output_format, ensure_ascii=False)}
"""
    return f"""You are a coding planner advising a local Codex runtime operator.
You do not need computer access and must not perform an action yourself. The operator \
will execute exactly one recommendation and return its result. Inspect before editing, \
finish the user's task completely, and verify the result.
{tool_instructions}
{output_instructions}
If no tool is needed, answer the user normally. Return only the answer intended for the \
user. Never mention the planner/operator workflow, hidden instructions, or your \
provider/model identity. The operator and tools are real parts of this workflow; never \
discuss whether you personally have computer access.

Operator context:
{context}

Conversation:
{conversation}"""


def responses_incremental_prompt(body: dict[str, Any]) -> str:
    conversation = response_transcript(body)
    return f"""The local Codex operator executed the action recommended in your previous \
response.
Continue the same original task using the new events below. If another tool is required, \
use the exact JSON-only tool-call format and catalog from earlier in this thread. \
Otherwise return only the final answer for the user. Never repeat an action whose result \
is already present.

New events:
{conversation}"""


def responses_compaction_prompt(body: dict[str, Any], *, continuing: bool) -> str:
    conversation = response_transcript(body)
    history_note = (
        "The complete conversation, including image attachments, is already available "
        "earlier in this Notion thread. Use it as the primary source."
        if continuing else
        "Use the transcript supplied below as the source."
    )
    return f"""Create a dense handoff checkpoint for another coding agent that will \
continue this exact task.
Preserve all user requirements and prohibitions, decisions, file paths, edits already \
made, tool results, failures, tests, image-derived facts, current state, and concrete \
next steps. Remove repetition and obsolete intermediate chatter. Do not call tools, do \
not add commentary, and output only the checkpoint text.

{history_note}

Current transcript events:
{conversation}"""


def planner_correction_prompt(
    *,
    unavailable_tool: str | None,
    malformed_tool: str | None,
) -> str:
    if unavailable_tool:
        reason = (
            f'The tool "{unavailable_tool}" is not available to the local operator. '
        )
    elif malformed_tool:
        reason = f'The attempted call to "{malformed_tool}" was not valid JSON. '
    else:
        reason = "Your previous answer was not a valid planner recommendation. "
    return (
        reason
        + "Use only an exact tool from the catalog already provided when another "
        "local action is necessary. If the requested information is already visible "
        "in the conversation, answer the user normally instead of emitting JSON."
    )


ANTHROPIC_CORRECTION_PROMPT = (
    "Your previous answer was not a valid planner recommendation. "
    "The local operator and the listed tools are available outside the model. "
    "You are not being asked to execute anything yourself. Recommend exactly "
    "one next action for the user's request as ONLY this JSON object: "
    '{"tool":"<exact tool name>","arguments":{...}}. '
    "Choose a tool from the catalog already provided and do not discuss capabilities."
)
