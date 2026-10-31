"""The FastAPI app: the JSON API plus the dashboard's static files."""

from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .api import router
from .runtime import Runtime

STATIC_DIR = Path(__file__).parent / "static"

# File types the map page needs. Python guesses types from the operating system, and Windows
# calls .mjs files plain text, which browsers refuse to run as JavaScript.
for kind, extension in (("text/javascript", ".mjs"), ("application/x-protobuf", ".pbf"),
                        ("application/vnd.pmtiles", ".pmtiles")):
    mimetypes.add_type(kind, extension)


class PageFiles(StaticFiles):
    """The dashboard's own files. Browsers must check back before reusing a saved copy (a quick
    "not modified" when nothing changed), so an updated page shows on the next reload rather than
    hours later."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


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
    # The basemap made by `python -m sitrep map`. The map page reads it in pieces (HTTP range
    # requests), which StaticFiles supports. Until the file exists, it simply isn't found.
    tiles = Path(runtime.cfg.map.tiles_path).parent
    tiles.mkdir(parents=True, exist_ok=True)
    app.mount("/tiles", StaticFiles(directory=tiles), name="tiles")
    # Everything the page needs is served from here: no CDNs, so it works offline.
    app.mount("/", PageFiles(directory=STATIC_DIR, html=True), name="static")
    return app
