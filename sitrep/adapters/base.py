"""The common interface every data source implements.

There are three shapes of source:

- PollingAdapter: fetches something on a timer (IODA, Radar, ACSC, ...).
- StreamingAdapter: keeps a live connection open (Bluesky's Jetstream).
- PushAdapter: receives data that something else sends to sitrep at
  POST /api/ingest/<id> (for example a balcony microphone posting siren counts).

Whatever the shape, an adapter reports an Assessment (a level plus a one-line
reason) through report(), and problems through report_failure(). The base
classes own the timers and the retries with exponential backoff, and they
catch every error, so a failing source can never crash the app.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, ClassVar

import httpx

from ..config import Config, Settings
from ..db import Database
from ..models import Assessment, Category
from ..state import StatusBoard


class SourceError(Exception):
    """A source failed in an expected way (bad response, unusable data).

    retry_after is how many seconds the source asked us to wait, if it said.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class AdapterContext:
    """Everything an adapter needs from the rest of the app."""

    config: Config
    db: Database
    board: StatusBoard
    http: httpx.AsyncClient
    clock: Callable[[], float] = time.time


class AdapterSettings(Settings):
    enabled: bool = True


class PollingSettings(AdapterSettings):
    interval_seconds: float
    # Show the source as stale after this many intervals without a good update.
    stale_after_intervals: float = 3


class StreamingSettings(AdapterSettings):
    stale_after_seconds: float = 180
    reconnect_first_seconds: float = 2
    reconnect_max_seconds: float = 300


def retry_delay(
    failures: int,
    first: float,
    maximum: float,
    retry_after: float | None = None,
    rand: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff: first, 2x, 4x, ... capped at maximum, with +-10% jitter."""
    delay = min(first * 2 ** max(failures - 1, 0), maximum)
    delay *= 0.9 + 0.2 * rand()
    if retry_after:
        delay = max(delay, retry_after)
    return delay


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The Retry-After header in seconds, if the server sent one in that form."""
    try:
        return max(0.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        return None


async def get_json(client: httpx.AsyncClient, url: str, source: str, params: dict[str, Any] | None = None) -> Any:
    """GET a JSON document, turning the usual failures into plain-English errors."""
    response = await client.get(url, params=params)
    if response.status_code == 429:
        raise SourceError(f"{source} asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError(f"{source} sent something that isn't JSON") from exc


def describe_error(exc: BaseException) -> str:
    """A short, plain-English description of what went wrong."""
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.ConnectError):
        return "couldn't connect"
    if isinstance(exc, SourceError):
        return str(exc)
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 160 else text[:157] + "..."


class SourceAdapter(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    category: ClassVar[Category]
    link: ClassVar[str | None] = None
    Settings: ClassVar[type[AdapterSettings]]
    # True if the source's settings take a carriers list, filled from the top-level
    # carriers in config.yaml unless the source's own block lists its own.
    uses_carriers: ClassVar[bool] = False

    def __init__(self, ctx: AdapterContext, settings: Any) -> None:
        self.ctx = ctx
        self.settings = settings
        self.log = logging.getLogger(f"sitrep.{self.id}")

    @property
    @abstractmethod
    def stale_after(self) -> float:
        """Seconds without a good update before the dashboard calls this source stale."""

    def register(self) -> None:
        self.ctx.board.register(self.id, self.name, self.category, self.link, self.stale_after)

    def report(self, assessment: Assessment) -> None:
        self.ctx.board.report(self.id, assessment)

    def report_failure(self, exc: BaseException) -> None:
        message = describe_error(exc)
        self.ctx.board.report_failure(self.id, message)
        self.log.warning("%s failed: %s", self.name, message)

    @abstractmethod
    async def run(self) -> None:
        """Run forever. Called (and restarted if it ever crashes) by the runtime."""


class PollingAdapter(SourceAdapter):
    settings: PollingSettings

    # A single poll that takes longer than this is abandoned and counted as a failure.
    poll_timeout_seconds: ClassVar[float] = 120

    @property
    def stale_after(self) -> float:
        return self.settings.interval_seconds * self.settings.stale_after_intervals + 30

    @abstractmethod
    async def poll(self) -> Assessment:
        """Fetch, parse and judge the source once. Raise on failure."""

    async def run(self) -> None:
        http = self.ctx.config.http
        failures = 0
        while True:
            try:
                assessment = await asyncio.wait_for(self.poll(), self.poll_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.report_failure(exc)
                delay = retry_delay(
                    failures, http.retry_first_seconds, http.retry_max_seconds,
                    getattr(exc, "retry_after", None),
                )
            else:
                failures = 0
                self.report(assessment)
                delay = self.settings.interval_seconds
            await asyncio.sleep(delay)


class StreamingAdapter(SourceAdapter):
    settings: StreamingSettings

    @property
    def stale_after(self) -> float:
        return self.settings.stale_after_seconds

    @abstractmethod
    async def stream(self) -> None:
        """Connect and process events until the connection ends. Raise on errors."""

    async def run(self) -> None:
        failures = 0
        while True:
            started = self.ctx.clock()
            try:
                await self.stream()
                raise SourceError("connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.ctx.clock() - started > 300:
                    failures = 0  # it was healthy for a while, so start the backoff afresh
                failures += 1
                self.report_failure(exc)
                await asyncio.sleep(
                    retry_delay(
                        failures, self.settings.reconnect_first_seconds,
                        self.settings.reconnect_max_seconds, getattr(exc, "retry_after", None),
                    )
                )


class PushAdapter(SourceAdapter):
    """A source that receives data instead of fetching it."""

    settings: AdapterSettings

    # Seconds without a push before the source is shown as stale.
    stale_after_seconds: ClassVar[float] = 900

    @property
    def stale_after(self) -> float:
        return self.stale_after_seconds

    @abstractmethod
    async def ingest(self, payload: dict[str, Any]) -> None:
        """Handle one payload posted to /api/ingest/<id>; call report() as needed."""

    async def run(self) -> None:
        await asyncio.Event().wait()  # nothing to fetch; data arrives through ingest()
