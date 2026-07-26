"""Logging setup shared by the service and by one-off commands.

The bridge emits one JSON object per line under a `uvicorn.error.*` logger, so
it works identically under systemd/journald, in Docker and in a terminal.
"""

from __future__ import annotations

import logging
import sys

LOGGER_NAMES = ("uvicorn.error.notion_bridge", "uvicorn.error.notion_pool")


def configure_logging(level: str = "INFO", *, stream=None) -> None:
    resolved = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(resolved)
        if not any(
            isinstance(existing, logging.StreamHandler) for existing in logger.handlers
        ):
            logger.addHandler(handler)
        # uvicorn installs its own handler on `uvicorn.error`; without this a
        # single event would be printed twice.
        logger.propagate = False
