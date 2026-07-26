"""Operational surface: liveness, readiness, metrics, model list and admin.

The split matters for an unattended deployment. `/livez` answers "is this
process still able to serve?", which is what a supervisor should restart on.
`/readyz` answers "can it complete a Notion turn right now?", which is what a
client or load balancer should back off on. `/healthz` keeps the original
combined shape so existing checks and scripts keep working.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import metrics
from ..diagnostics import log_event
from ..notion.models import model_catalog
from ..service import AccountReloadBusy, BridgeService
from ..settings import SUPPORTED_MODELS

log = logging.getLogger("uvicorn.error.notion_bridge")
router = APIRouter()


def bridge(request: Request) -> BridgeService:
    return request.app.state.service


@router.get("/livez")
async def livez(request: Request) -> dict[str, Any]:
    """Cheap proof the event loop is still scheduling work."""
    service = bridge(request)
    return {
        "ok": True,
        "uptime_seconds": round(service.uptime_seconds, 3),
        "version": request.app.version,
    }


@router.get("/readyz")
async def readyz(request: Request):
    service = bridge(request)
    accounts = await service.account_status()
    ready = accounts["configured"] > 0 and accounts["available"] > 0
    payload = {
        "ok": ready,
        "reason": (
            None if ready
            else "no accounts configured" if accounts["configured"] == 0
            else "every account is busy, cooling down or disabled"
        ),
        "account_pool": {
            key: accounts[key]
            for key in ("configured", "available", "busy", "cooldown", "disabled")
        },
        "retry_after": accounts["global_retry_after"],
    }
    if ready:
        return payload
    headers = (
        {"Retry-After": str(accounts["global_retry_after"])}
        if accounts["global_retry_after"] else None
    )
    return JSONResponse(payload, status_code=503, headers=headers)


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    service = bridge(request)
    accounts = await service.account_status()
    state = await service.state_status()
    settings = service.settings
    return {
        "ok": accounts["configured"] > 0,
        "ready": accounts["configured"] > 0 and accounts["available"] > 0,
        "version": request.app.version,
        "uptime_seconds": round(service.uptime_seconds, 3),
        "model": settings.default_model,
        "models": list(SUPPORTED_MODELS),
        "reasoning_effort": settings.reasoning_effort,
        "account_pool": accounts,
        "turn_affinity": state["turn_affinity"],
        "conversation_segments": state["conversation_segments"],
        "coding_tools": {"configured": service.runtime_tools.configured},
        "custom_agent": bool(settings.workflow_id),
        "external_agent_loop": not bool(settings.workflow_id),
        "settings": settings.summary(),
    }


@router.get("/metrics")
async def prometheus_metrics(request: Request):
    service = bridge(request)
    if not service.settings.metrics_enabled:
        return PlainTextResponse("metrics are disabled\n", status_code=404)
    # Refresh gauges that are only known by asking the stores.
    await service.state_status()
    return PlainTextResponse(
        metrics.registry.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/v1/models")
async def models() -> dict[str, Any]:
    return model_catalog(int(time.time()))


@router.post("/admin/accounts/reload")
async def reload_accounts(request: Request):
    """Pick up new or re-authenticated Notion sessions without a restart."""
    service = bridge(request)
    if not service.settings.admin_enabled:
        return JSONResponse(
            {"error": {"message": "admin endpoints are disabled", "type": "not_found"}},
            status_code=404,
        )
    try:
        status = await service.reload_accounts()
    except AccountReloadBusy as exc:
        return JSONResponse(
            {
                "error": {"message": str(exc), "type": "conflict"},
                "busy": exc.busy,
            },
            status_code=409,
        )
    except Exception as exc:
        log_event(log, "account_reload_failed", level=logging.ERROR, error=str(exc))
        return JSONResponse(
            {"error": {"message": str(exc), "type": "api_error"}},
            status_code=500,
        )
    return {"ok": status["configured"] > 0, "account_pool": status}
