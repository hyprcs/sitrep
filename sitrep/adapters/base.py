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

Each report also says how new the data inside it is (content_at), so a source
whose data has frozen shows as frozen. Sources made of items (advisories, notices,
headlines) pass each one through see_item(), which remembers when sitrep first saw
it: an item that was already old by then is backlog, and can't raise an alert.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Literal

import httpx
from pydantic import field_validator

from ..config import Config, Settings
from ..db import Database, Seen
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
    # How old the newest data may get, in minutes, before the source shows as "frozen".
    # "none" for quiet feeds that can go days without anything new. Leave it out to use
    # the source's own default.
    content_age_budget_minutes: float | Literal["none", "default"] = "default"
    # Items first seen more than this many minutes after their own time can't alert.
    # Leave it out to use alerts.recency_limit_minutes, or the source's poll interval
    # plus its usual publishing delay if that's longer.
    alert_recency_minutes: float | None = None

    @field_validator("content_age_budget_minutes", mode="before")
    @classmethod
    def _none_means_none(cls, value: Any) -> Any:
        if value is None:
            return "none"
        return value.strip().lower() if isinstance(value, str) else value


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
    # What a raised report is about, when its signals don't say (see models.py).
    scope: ClassVar[str | None] = None
    # official, mainstream or social: what kind of source this is, for story clusters.
    source_type: ClassVar[str] = "official"
    # Minutes of data age before this source shows as frozen; None for quiet feeds.
    content_budget_minutes: ClassVar[float | None] = None
    # How long after an item's own time the source usually publishes it, in minutes.
    publish_delay_minutes: ClassVar[float] = 0

    def __init__(self, ctx: AdapterContext, settings: Any) -> None:
        self.ctx = ctx
        self.settings = settings
        self.log = logging.getLogger(f"sitrep.{self.id}")
        self._wake = asyncio.Event()
        self.last_success_at: float | None = None

    @property
    @abstractmethod
    def stale_after(self) -> float:
        """Seconds without a good update before the dashboard says this source's fetch is failing."""

    @property
    def poll_gap(self) -> float:
        """The usual seconds between two looks at the source (0 for a live stream)."""
        return 0.0

    @property
    def content_budget(self) -> float | None:
        """Seconds the newest data may age before the source is frozen, or None for no limit."""
        chosen = self.settings.content_age_budget_minutes
        if chosen == "none":
            return None
        if chosen == "default":
            return None if self.content_budget_minutes is None else self.content_budget_minutes * 60
        return float(chosen) * 60

    @property
    def recency_limit(self) -> float:
        """Seconds after its own time that an item can still be first seen and raise an alert."""
        if self.settings.alert_recency_minutes is not None:
            return self.settings.alert_recency_minutes * 60
        floor = self.ctx.config.alerts.recency_limit_minutes * 60
        return max(floor, self.poll_gap + self.publish_delay_minutes * 60)

    def register(self) -> None:
        self.ctx.board.register(
            self.id, self.name, self.category, self.link, self.stale_after,
            content_budget=self.content_budget, default_scope=self.scope,
            failing_after=self.ctx.config.scoring.failing_after_tries,
        )

    def see_item(
        self, item_id: str, ts: float | None, *, title: str, level: str = "info", url: str | None = None,
        details: dict[str, Any] | None = None, now: float | None = None,
    ) -> Seen:
        """Remember an item and say whether it can raise an alert.

        An item can alert when sitrep first sees it (or a new version of it) within
        recency_limit of its own time. An item with no time of its own can alert only if
        sitrep's previous look at this source worked and was recent, so a source catching
        up after a break can't present its backlog as news.
        """
        now = self.ctx.clock() if now is None else now
        if ts is not None:
            fresh = now - ts <= self.recency_limit
        else:
            gap = max(self.poll_gap, 60.0)
            fresh = self.last_success_at is not None and now - self.last_success_at <= 2 * gap + 30
        return self.ctx.db.see_item(
            self.id, item_id, ts=ts, now=now, fresh=fresh, source_type=self.source_type,
            title=title, level=level, url=url, details=details,
        )

    def wake(self) -> None:
        """Look at the source again now, without waiting out a retry delay (the internet is back)."""
        self._wake.set()

    async def _sleep(self, seconds: float) -> bool:
        """Wait, unless woken first. Returns True if woken."""
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            return False
        finally:
            self._wake.clear()
        return True

    def report(self, assessment: Assessment) -> None:
        self.ctx.board.report(self.id, assessment)
        self.last_success_at = self.ctx.clock()

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

    @property
    def poll_gap(self) -> float:
        return self.settings.interval_seconds

    @abstractmethod
    async def poll(self) -> Assessment:
        """Fetch, parse and judge the source once. Raise on failure."""

    def next_interval(self) -> float:
        """Seconds until the next poll after a good one. A source can look more often while it matters."""
        return self.settings.interval_seconds

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
                delay = self.next_interval()
            if await self._sleep(delay):
                failures = 0  # woken because the internet is back: start the backoff afresh


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
                woken = await self._sleep(
                    retry_delay(
                        failures, self.settings.reconnect_first_seconds,
                        self.settings.reconnect_max_seconds, getattr(exc, "retry_after", None),
                    )
                )
                if woken:
                    failures = 0


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
