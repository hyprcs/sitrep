"""The FastAPI app: the JSON API plus the dashboard's static files."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import router
from .runtime import Runtime

STATIC_DIR = Path(__file__).parent / "static"


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="sitrep", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime
    app.include_router(router)
    # Everything the page needs is served from here: no CDNs, so it works offline.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
