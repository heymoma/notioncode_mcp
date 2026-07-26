"""FastAPI application factory and request lifecycle."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from . import metrics
from .api import anthropic, chat, operations, responses
from .diagnostics import (
    exception_fields,
    log_event,
    reset_log_context,
    set_log_context,
)
from .sd_notify import SystemdNotifier, WatchdogTask
from .service import BridgeService
from .settings import Settings

VERSION = "2.0.0"

log = logging.getLogger("uvicorn.error.notion_bridge")

TRACKED_ENDPOINTS = frozenset({
    "/v1/responses",
    "/v1/responses/compact",
    "/v1/messages",
    "/v1/chat/completions",
})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service = BridgeService(settings)
        app.state.service = service
        notifier = SystemdNotifier() if settings.watchdog_enabled else SystemdNotifier("")
        watchdog = WatchdogTask(notifier)
        await service.start()
        notifier.ready()
        notifier.status("serving")
        watchdog.start()
        log_event(log, "bridge_started", version=VERSION, **settings.summary())
        try:
            yield
        finally:
            notifier.stopping()
            await watchdog.stop()
            await service.aclose()
            notifier.close()
            log_event(log, "bridge_stopped", version=VERSION)

    app = FastAPI(
        title="Notion AI bridge",
        version=VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def diagnostic_request_logging(request: Request, call_next):
        endpoint = request.url.path
        tracked = request.method == "POST" and endpoint in TRACKED_ENDPOINTS
        token = set_log_context(
            request_id=uuid.uuid4().hex[:12],
            method=request.method,
            endpoint=endpoint,
        )
        started_at = time.monotonic()
        status_code = 500
        if tracked:
            metrics.registry.increment(
                metrics.IN_FLIGHT,
                help_text="Requests currently being served.",
                labels={"endpoint": endpoint},
            )
        try:
            if tracked:
                log_event(log, "request_started")
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as error:
            if tracked:
                log_event(
                    log,
                    "request_unhandled_exception",
                    level=logging.ERROR,
                    **exception_fields(error),
                )
            raise
        finally:
            duration = time.monotonic() - started_at
            if tracked:
                metrics.registry.increment(
                    metrics.IN_FLIGHT,
                    amount=-1,
                    labels={"endpoint": endpoint},
                )
                metrics.record_request(endpoint, status_code, duration)
                log_event(
                    log,
                    "request_finished",
                    level=logging.INFO if status_code < 500 else logging.ERROR,
                    status_code=status_code,
                    duration_ms=round(duration * 1000),
                )
            reset_log_context(token)

    app.include_router(operations.router)
    app.include_router(responses.router)
    app.include_router(anthropic.router)
    app.include_router(chat.router)
    return app
