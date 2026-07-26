"""Model identity: bridge IDs, Codex transport IDs and Notion's own aliases."""

from __future__ import annotations

from typing import Any

from notion_agent_cli import transcript as notion_transcript

from ..settings import (
    CODEX_FABLE_MODEL_ID,
    MODEL_DISPLAY_NAMES,
    SUPPORTED_MODELS,
)

_FABLE_ALIASES = {"sonnet", "haiku", "fable", "default"}
_OPUS_ALIASES = {"opus", "best"}
_pinned = False


def pin_explicit_model_selection() -> None:
    """Tell Notion every turn carries a user-selected model.

    Without `modelFromUser`, a continuation turn silently falls back to Notion's
    Auto model, which is how a thread that started on Opus quietly stops being
    Opus halfway through a long session.
    """
    global _pinned
    if _pinned:
        return
    original = notion_transcript.build_config_value

    def build_explicit_model_config(*args: Any, **kwargs: Any) -> dict[str, Any]:
        config = original(*args, **kwargs)
        config["modelFromUser"] = True
        return config

    notion_transcript.build_config_value = build_explicit_model_config
    _pinned = True


def resolve_model(
    model: str | None,
    *,
    default_model: str,
    forced_model: str = "",
) -> str:
    """Map any client-supplied model ID onto a supported Notion model."""
    requested = (model or default_model).lower()
    if forced_model:
        if forced_model not in SUPPORTED_MODELS:
            raise ValueError(f"unsupported forced model: {forced_model}")
        return forced_model
    if requested == CODEX_FABLE_MODEL_ID:
        return "fable-5"
    if requested in SUPPORTED_MODELS:
        return requested
    if requested in _OPUS_ALIASES or "opus" in requested:
        return "opus-5"
    if requested in _FABLE_ALIASES or any(
        alias in requested for alias in ("sonnet", "haiku", "fable")
    ):
        return "fable-5"
    raise ValueError(f"unsupported model: {model}")


def model_catalog(created: int) -> dict[str, Any]:
    """The `/v1/models` payload, in the shape OpenAI and Anthropic clients expect."""
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "type": "model",
                "display_name": MODEL_DISPLAY_NAMES[model_id],
                "created": created,
                "created_at": "2026-01-01T00:00:00Z",
                "owned_by": "notion",
            }
            for model_id in SUPPORTED_MODELS
        ],
        "has_more": False,
        "first_id": SUPPORTED_MODELS[0],
        "last_id": SUPPORTED_MODELS[-1],
    }
