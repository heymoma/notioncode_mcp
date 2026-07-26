"""One error-mapping policy for every compatibility surface.

Clients back off correctly only when the bridge is consistent: a cooling-down
account pool has to become `503` plus `Retry-After` on the Anthropic and Chat
endpoints too, not just on Responses.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi.responses import JSONResponse

from ..accounts.pool import AccountPoolCoolingDown, AccountPoolExhausted
from ..diagnostics import exception_fields, log_event
from ..planner.runtime_tools import RuntimeToolsUnavailable

log = logging.getLogger("uvicorn.error.notion_bridge")

Style = Literal["openai", "anthropic"]

NO_ACCOUNTS_MESSAGE = "No valid Notion accounts are configured"


def _body(style: Style, message: str, error_type: str) -> dict[str, Any]:
    if style == "anthropic":
        return {"type": "error", "error": {"type": error_type, "message": message}}
    return {"error": {"message": message, "type": error_type}}


def error_payload(
    style: Style,
    message: str,
    *,
    error_type: str,
    status_code: int,
    retry_after: int | None = None,
) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        _body(style, message, error_type),
        status_code=status_code,
        headers=headers,
    )


def invalid_request(style: Style, message: str) -> JSONResponse:
    return error_payload(
        style, message, error_type="invalid_request_error", status_code=400
    )


def no_accounts(style: Style) -> JSONResponse:
    return error_payload(
        style,
        NO_ACCOUNTS_MESSAGE,
        error_type="api_error",
        status_code=503,
    )


def upstream_failure(
    style: Style,
    error: Exception,
    *,
    event: str = "api_request_failed",
) -> JSONResponse:
    """Translate any upstream failure into the right status code and log it once."""
    if isinstance(error, AccountPoolCoolingDown):
        log_event(
            log,
            event,
            level=logging.WARNING,
            status_code=503,
            retry_after=error.retry_after,
            **exception_fields(error),
        )
        return error_payload(
            style,
            str(error),
            error_type="temporarily_unavailable",
            status_code=503,
            retry_after=error.retry_after,
        )
    if isinstance(error, AccountPoolExhausted):
        log_event(
            log, event, level=logging.ERROR, status_code=503, **exception_fields(error)
        )
        return error_payload(
            style, str(error), error_type="temporarily_unavailable", status_code=503
        )
    if isinstance(error, TimeoutError):
        log_event(
            log, event, level=logging.WARNING, status_code=504, **exception_fields(error)
        )
        return error_payload(
            style, str(error), error_type="timeout_error", status_code=504
        )
    if isinstance(error, RuntimeToolsUnavailable):
        log_event(
            log, event, level=logging.ERROR, status_code=503, **exception_fields(error)
        )
        return error_payload(
            style, str(error), error_type="api_error", status_code=503
        )
    log_event(
        log, event, level=logging.ERROR, status_code=502, **exception_fields(error)
    )
    return error_payload(style, str(error), error_type="api_error", status_code=502)
