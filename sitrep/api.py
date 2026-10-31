"""The JSON API the dashboard page reads from."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .adapters.base import PushAdapter
from .runtime import Runtime

router = APIRouter()


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get("/api/state")
async def state(request: Request) -> dict[str, Any]:
    """Overall level, category levels, and every source's latest report."""
    return _runtime(request).state_payload()


@router.get("/api/map")
async def map_data(request: Request) -> dict[str, Any]:
    """Home, and everything sources have placed on the map, for the map page."""
    return _runtime(request).map_payload()


@router.get("/api/timeline")
async def timeline(request: Request, hours: float = Query(24, gt=0, le=24 * 90)) -> dict[str, Any]:
    runtime = _runtime(request)
    return {"events": runtime.db.events_since(runtime.clock() - hours * 3600)}


@router.get("/api/uptime")
async def uptime(
    request: Request,
    hours: float = Query(24, gt=0, le=24 * 7),
    bucket_minutes: int = Query(15, ge=1, le=360),
) -> dict[str, Any]:
    runtime = _runtime(request)
    probes = runtime.adapters.get("probes")
    return {
        "bucket_seconds": bucket_minutes * 60,
        "round_seconds": probes.settings.interval_seconds if probes else 30,
        "buckets": runtime.uptime(hours, bucket_minutes),
    }


@router.post("/api/ingest/{source_id}")
async def ingest(source_id: str, request: Request) -> dict[str, bool]:
    """For push sources (e.g. a balcony microphone): they POST JSON here."""
    adapter = _runtime(request).adapters.get(source_id)
    if not isinstance(adapter, PushAdapter):
        raise HTTPException(404, f"No push source called {source_id!r}")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(422, "Send a JSON object")
    await adapter.ingest(payload)
    return {"ok": True}
