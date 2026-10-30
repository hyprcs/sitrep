"""The adapter base classes: backoff, and never crashing when a source fails."""

from __future__ import annotations

import asyncio

import pytest

from sitrep.adapters.base import (
    AdapterContext,
    PollingAdapter,
    PollingSettings,
    SourceError,
    StreamingAdapter,
    StreamingSettings,
    retry_delay,
)
from sitrep.config import Config, HttpSettings
from sitrep.db import Database
from sitrep.models import Assessment, Category, Level
from sitrep.state import StatusBoard


def test_retry_delay_doubles_and_caps():
    no_jitter = lambda: 0.5  # noqa: E731  (0.5 means exactly 1.0x)
    assert [retry_delay(n, 30, 1800, rand=no_jitter) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert retry_delay(20, 30, 1800, rand=no_jitter) == 1800


def test_retry_delay_honours_retry_after_and_jitter():
    assert retry_delay(1, 30, 1800, retry_after=300, rand=lambda: 0.5) == 300
    assert 27 <= retry_delay(1, 30, 1800, rand=lambda: 0.0) <= 33
    assert 27 <= retry_delay(1, 30, 1800, rand=lambda: 1.0) <= 33


@pytest.fixture
def ctx():
    db = Database(":memory:")
    cfg = Config(http=HttpSettings(retry_first_seconds=0.01, retry_max_seconds=0.05))
    yield AdapterContext(cfg, db, StatusBoard(db), http=None)
    db.close()


class FlakyPoller(PollingAdapter):
    id = "flaky"
    name = "Flaky"
    category = Category.OFFICIAL
    Settings = PollingSettings

    def __init__(self, ctx, settings):
        super().__init__(ctx, settings)
        self.calls = 0

    async def poll(self) -> Assessment:
        self.calls += 1
        if self.calls <= 2:
            raise SourceError("upstream said no")
        return Assessment(Level.NORMAL, "fine")


async def test_polling_adapter_survives_failures_and_recovers(ctx):
    adapter = FlakyPoller(ctx, PollingSettings(interval_seconds=0.01))
    adapter.register()
    task = asyncio.create_task(adapter.run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if adapter.calls >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    view = ctx.board.views()[0]
    assert adapter.calls >= 3
    assert view.level == Level.NORMAL
    assert view.failures == 0


class BrokenPoller(FlakyPoller):
    id = "broken"

    async def poll(self) -> Assessment:
        self.calls += 1
        raise RuntimeError("upstream said no")


async def test_polling_adapter_records_failures_without_crashing(ctx):
    adapter = BrokenPoller(ctx, PollingSettings(interval_seconds=0.01))
    adapter.register()
    task = asyncio.create_task(adapter.run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if adapter.calls >= 3:
            break
    assert not task.done()  # still running after repeated failures
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    view = ctx.board.views()[0]
    assert view.failures >= 3
    assert view.last_error == "upstream said no"
    assert view.level == Level.PENDING  # never reported as normal


class DroppyStream(StreamingAdapter):
    id = "droppy"
    name = "Droppy"
    category = Category.SOCIAL
    Settings = StreamingSettings

    def __init__(self, ctx, settings):
        super().__init__(ctx, settings)
        self.connections = 0

    async def stream(self) -> None:
        self.connections += 1
        self.report(Assessment(Level.NORMAL, f"connection {self.connections}"))
        raise ConnectionResetError("dropped")


async def test_streaming_adapter_reconnects(ctx):
    settings = StreamingSettings(reconnect_first_seconds=0.01, reconnect_max_seconds=0.02)
    adapter = DroppyStream(ctx, settings)
    adapter.register()
    task = asyncio.create_task(adapter.run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if adapter.connections >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.connections >= 3
