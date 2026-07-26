"""Local bridge between Notion AI and OpenAI/Anthropic-compatible coding clients."""

from __future__ import annotations

from .app import VERSION, create_app
from .settings import Settings, SettingsError

__all__ = ["VERSION", "Settings", "SettingsError", "create_app"]
