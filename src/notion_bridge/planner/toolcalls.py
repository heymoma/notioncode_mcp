"""Recovering tool calls from a chat model that has no native tool protocol.

Notion AI answers with prose, so the planner protocol asks for a bare JSON
object. Models still wrap it in fences, emit Anthropic-style `<invoke>` XML or
mangle the JSON, so every accepted shape is decoded here in one place.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TOOL_NAME_PATTERN = re.compile(r'["\'](?:tool|name)["\']\s*:\s*["\']([^"\']+)')
_INVOKE_NAME_PATTERN = re.compile(r'<invoke\s+name=["\']([^"\']+)')
_PARAMETER_PATTERN = re.compile(
    r'(?:<(?:antml:)?parameter|["\']antml:parameter)\s+'
    r'name=["\']([^"\']+)["\']>(.*?)</(?:antml:)?parameter>',
    re.DOTALL,
)
_WORKFLOW_CALL_PATTERN = re.compile(r"\{\s*\"function\"\s*:.*?\}\s*$", re.S)

REFUSAL_MARKERS = (
    "нет доступа к файловой системе",
    "нет доступа к вашему компьютеру",
    "нет доступа к вашему серверу",
    "нет инструментов",
    "не могу выполнить это",
    "не могу запускать shell",
    "i don't have access to the file system",
    "i do not have access to the file system",
    "i can't access your file system",
    "i cannot access your file system",
    "i don't have tools",
    "i do not have tools",
)


def json_candidates(text: str) -> list[str]:
    """The whole answer plus any fenced block, in priority order."""
    candidates = [text.strip()]
    if "```" in text:
        candidates.extend(
            part.strip().removeprefix("json").strip()
            for part in text.split("```")
            if part.strip()
        )
    return candidates


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    return {
        str(tool.get("name"))
        for tool in tools or []
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }


def looks_like_agent_refusal(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)


def responses_tool_catalog(value: Any) -> list[dict[str, Any]]:
    """Flatten Responses namespace tools into callable names Codex accepts."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for tool in value:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "namespace":
            if isinstance(tool.get("name"), str):
                result.append(tool)
            continue
        namespace = tool.get("name")
        children = tool.get("tools")
        if not isinstance(namespace, str) or not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, dict) or not isinstance(child.get("name"), str):
                continue
            flattened = dict(child)
            flattened["name"] = f"{namespace}.{child['name']}"
            flattened["namespace"] = namespace
            result.append(flattened)
    return result


def _custom_tool_input(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("command")
            or value.get("cmd")
            or value.get("patch")
            or json.dumps(value, ensure_ascii=False)
        )
    return str(value)


def extract_responses_tool_call(
    text: str, tools: list[dict[str, Any]]
) -> tuple[str, str, str] | None:
    """Return `(kind, name, payload)` for a Responses function/custom tool call."""
    by_name = {
        str(tool.get("name")): tool
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    for candidate in json_candidates(text):
        name_match = (
            _TOOL_NAME_PATTERN.search(candidate)
            or _INVOKE_NAME_PATTERN.search(candidate)
        )
        if name_match and name_match.group(1) in by_name:
            parameter_matches = _PARAMETER_PATTERN.findall(candidate)
            if parameter_matches:
                arguments: dict[str, Any] = {}
                for key, raw_value in parameter_matches:
                    raw_value = raw_value.strip()
                    try:
                        arguments[key] = json.loads(raw_value)
                    except json.JSONDecodeError:
                        arguments[key] = raw_value
                name = name_match.group(1)
                if str(by_name[name].get("type", "function")) == "custom":
                    return "custom", name, _custom_tool_input(arguments)
                return "function", name, json.dumps(arguments, ensure_ascii=False)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("tool") or value.get("name")
        tool = by_name.get(str(name))
        if tool is None:
            continue
        if str(tool.get("type", "function")) == "custom":
            return "custom", str(name), _custom_tool_input(
                value.get("input", value.get("arguments", ""))
            )
        arguments = value.get("arguments", value.get("input", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                continue
        if isinstance(arguments, dict):
            return "function", str(name), json.dumps(arguments, ensure_ascii=False)
    return None


def extract_malformed_responses_tool(
    text: str, tools: list[dict[str, Any]]
) -> str | None:
    """Name of a known tool the model tried to call with invalid JSON."""
    allowed = _tool_names(tools)
    for candidate in json_candidates(text):
        if not candidate.startswith("{"):
            continue
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            match = _TOOL_NAME_PATTERN.search(candidate)
            if match and match.group(1) in allowed:
                return match.group(1)
    return None


def extract_unavailable_responses_tool(
    text: str, tools: list[dict[str, Any]]
) -> str | None:
    """Name of a tool the model invented, which is not in the catalog."""
    allowed = _tool_names(tools)
    for candidate in json_candidates(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("tool") or value.get("name")
        if (
            isinstance(name, str)
            and name
            and name not in allowed
            and ("arguments" in value or "input" in value)
        ):
            return name
    return None


def extract_anthropic_tool_call(
    text: str, tools: list[dict[str, Any]]
) -> tuple[str, dict[str, Any]] | None:
    allowed = _tool_names(tools)
    for candidate in json_candidates(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("tool") or value.get("name")
        arguments = value.get("arguments", value.get("input", {}))
        if name in allowed and isinstance(arguments, dict):
            return str(name), arguments
    return None


def extract_chat_tool_call(
    text: str, tools: list[dict[str, Any]] | None
) -> tuple[str, dict[str, Any]] | None:
    """Chat Completions variant: tool names live under `function.name`."""
    if not tools:
        return None
    allowed = {
        str(item.get("function", {}).get("name"))
        for item in tools
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    }
    candidates = json_candidates(text)
    if "<tool_call>" in text and "</tool_call>" in text:
        start = text.index("<tool_call>") + len("<tool_call>")
        end = text.index("</tool_call>", start)
        candidates.insert(0, text[start:end].strip())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("tool") or value.get("name")
        arguments = value.get("arguments", value.get("parameters", {}))
        if name in allowed and isinstance(arguments, dict):
            return str(name), arguments
    return None


def extract_workflow_call(text: str) -> tuple[str, dict[str, Any]] | None:
    """Tool call emitted by a Notion custom agent (`NOTION_WORKFLOW_ID`)."""
    candidates = [text.strip(), *_WORKFLOW_CALL_PATTERN.findall(text)]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        function = value.get("function")
        args = value.get("args")
        if not isinstance(function, str) or not isinstance(args, dict):
            continue
        if function.endswith(".runTool"):
            name = args.get("toolName")
            tool_args = args.get("toolArguments", {})
            if isinstance(name, str) and isinstance(tool_args, dict):
                return name, tool_args
        if function.endswith(".listTools"):
            return "listTools", {}
    return None


def extract_planner_action(text: str) -> dict[str, Any] | None:
    """One-action JSON used by the OpenCode/Chat planner loop."""
    for candidate in json_candidates(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("action"), str):
            return value
    return None


def planner_action_to_tool(action: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Translate a planner action into a coding-runtime MCP tool call."""
    kind = action.get("action")
    if kind == "list_files":
        return "list_files", {"directory": str(action.get("directory", "."))}
    if kind == "read_file":
        arguments: dict[str, Any] = {"file_path": str(action.get("file_path", ""))}
        if isinstance(action.get("max_bytes"), int):
            arguments["max_bytes"] = action["max_bytes"]
        return "read_file", arguments
    if kind == "write_file":
        return "write_file", {
            "file_path": str(action.get("file_path", "")),
            "content": str(action.get("content", "")),
        }
    if kind == "edit_file":
        return "edit_file", {
            "file_path": str(action.get("file_path", "")),
            "old_text": str(action.get("old_text", "")),
            "new_text": str(action.get("new_text", "")),
            "replace_all": bool(action.get("replace_all", False)),
        }
    if kind == "run_shell":
        arguments = {
            "command": str(action.get("command", "")),
            "cwd": str(action.get("cwd", ".")),
        }
        if isinstance(action.get("timeout_ms"), int):
            arguments["timeout_ms"] = action["timeout_ms"]
        return "run_shell", arguments
    return None
