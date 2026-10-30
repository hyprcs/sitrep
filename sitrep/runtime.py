"""Wires everything together and runs the background tasks:

- one task per enabled source (restarted if it ever crashes),
- the scorer, which works out the overall level every 30 seconds,
- alerts, which message your phone when the overall level changes (if set up),
- housekeeping, which deletes data older than the retention settings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import httpx
from pydantic import ValidationError

from . import __version__
from .adapters import REGISTRY
from .adapters.base import AdapterContext, AdapterSettings, SourceAdapter, retry_delay
from .alerts import Alerts, Notifier
from .config import Config, ConfigError, format_errors
from .db import Database
from .models import Level, Overall
from .scoring import OverallTracker, score_categories, score_overall
from .state import SourceView, StatusBoard
from .telegram import TelegramNotifier, off_status

log = logging.getLogger("sitrep")

HOUSEKEEPING_EVERY_SECONDS = 6 * 3600


def _quiet_connection_resets(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Python's event loop on Windows logs a scary traceback whenever the other side
    drops a connection abruptly (a browser reloading the page, for example). It's harmless."""
    if isinstance(context.get("exception"), ConnectionResetError):
        log.debug("Connection reset: %s", context.get("message"))
        return
    loop.default_exception_handler(context)


def build_adapter_specs(cfg: Config) -> list[tuple[type[SourceAdapter], AdapterSettings]]:
    """Check every source's settings up front, so mistakes show before anything starts."""
    specs: list[tuple[type[SourceAdapter], AdapterSettings]] = []
    problems: list[str] = []
    for key, raw in cfg.sources.items():
        adapter_cls = REGISTRY.get(key)
        if adapter_cls is None:
            known = ", ".join(sorted(REGISTRY))
            problems.append(f"  - sources.{key}: unknown source (known sources: {known})")
            continue
        raw = dict(raw or {})
        if adapter_cls.uses_carriers and "carriers" not in raw:
            raw["carriers"] = [carrier.model_dump() for carrier in cfg.carriers]
        try:
            settings = adapter_cls.Settings.model_validate(raw)
        except ValidationError as exc:
            problems.append(format_errors(exc, prefix=f"sources.{key}."))
            continue
        if settings.enabled:
            specs.append((adapter_cls, settings))
    if problems:
        raise ConfigError("config.yaml has a problem:\n" + "\n".join(problems))
    return specs


class Runtime:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.clock = clock
        self.specs = build_adapter_specs(cfg)
        self.tracker = OverallTracker(cfg.scoring, clock())
        self.adapters: dict[str, SourceAdapter] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._last_levels: dict[str, Level] = {}

    def setup(self) -> None:
        """Open the database and create the sources and alerts, without starting anything."""
        self.db = Database(self.cfg.storage.db_path)
        self.board = StatusBoard(self.db, self.clock)
        self.http = httpx.AsyncClient(
            timeout=self.cfg.http.timeout_seconds,
            headers={"User-Agent": self.cfg.http.user_agent},
            follow_redirects=True,
        )
        self.alerts = self._build_alerts()
        ctx = AdapterContext(self.cfg, self.db, self.board, self.http, self.clock)
        for adapter_cls, settings in self.specs:
            adapter = adapter_cls(ctx, settings)
            adapter.register()
            self.adapters[adapter.id] = adapter

    async def start(self) -> None:
        asyncio.get_running_loop().set_exception_handler(_quiet_connection_resets)
        self.setup()
        for adapter in self.adapters.values():
            self._spawn(self._supervise(adapter), adapter.id)
        self._spawn(self._scorer_loop(), "scorer")
        self._spawn(self._housekeeping_loop(), "housekeeping")
        self._spawn(self.alerts.run(), "alerts")
        names = ", ".join(a.name for a in self.adapters.values()) or "none"
        log.info("sitrep %s started with sources: %s", __version__, names)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.http.aclose()
        self.db.close()

    def _build_alerts(self, simulation: bool = False) -> Alerts:
        cfg = self.cfg.alerts
        notifiers: list[Notifier] = []
        off: list[dict[str, Any]] = []
        if cfg.telegram.configured:
            notifiers.append(TelegramNotifier(cfg, self.http, self.db, self.clock, simulation=simulation))
        else:
            off.append(off_status(cfg.telegram))
        return Alerts(cfg, self.db, notifiers, off)

    def _spawn(self, coro: Any, name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))

    async def _supervise(self, adapter: SourceAdapter) -> None:
        """Keep an adapter running whatever happens inside it."""
        crashes = 0
        while True:
            try:
                await adapter.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                crashes += 1
                log.exception("%s crashed; restarting it", adapter.name)
                adapter.report_failure(exc)
            await asyncio.sleep(retry_delay(max(crashes, 1), 5, 300))

    # --- Scoring ---

    def evaluate(self) -> None:
        """One scoring pass: log source changes, update the overall level."""
        now = self.clock()
        views = self.board.views(now)
        self._log_source_changes(views, now)
        candidate, _ = score_overall(views, self.cfg.scoring)
        previous = self.tracker.current.level
        if self.tracker.update(candidate, now):
            current = self.tracker.current
            self.db.add_overall(now, current.level.value, current.reason)
            if previous != Overall.UNKNOWN or current.level != Overall.NORMAL:
                self.db.add_event(
                    now, "overall", "overall", current.level.value,
                    f"Overall: {current.level.value}",
                    details={"reason": current.reason, "from": previous.value},
                )
            log.info("Overall level is now %s: %s", current.level.value, current.reason)
        self.alerts.update(self.tracker.current.level, self.tracker.current.reason, now)

    def _log_source_changes(self, views: list[SourceView], now: float) -> None:
        for view in views:
            before = self._last_levels.get(view.id)
            self._last_levels[view.id] = view.level
            if before is None or before == view.level:
                continue
            if before == Level.PENDING and view.level in (Level.NORMAL, Level.LEARNING):
                continue  # a normal start-up isn't news
            self.db.add_status_change(now, view.id, view.level.value, view.reason)
            self.db.add_event(
                now, view.id, view.category.value, view.level.value,
                f"{view.name}: {view.level.value}",
                url=view.link,
                details={"reason": view.reason, "from": before.value},
            )

    async def _scorer_loop(self) -> None:
        interval = self.cfg.scoring.evaluate_every_seconds
        await asyncio.sleep(min(6.0, interval))  # let the first probe round finish
        while True:
            try:
                self.evaluate()
            except Exception:
                log.exception("Scoring failed; will try again")
            await asyncio.sleep(interval)

    async def _housekeeping_loop(self) -> None:
        while True:
            try:
                deleted = self.db.cleanup(self.clock(), self.cfg.storage)
                if any(deleted.values()):
                    log.info("Housekeeping removed old rows: %s", deleted)
            except Exception:
                log.exception("Housekeeping failed; will try again")
            await asyncio.sleep(HOUSEKEEPING_EVERY_SECONDS)

    # --- Data for the API ---

    def state_payload(self) -> dict[str, Any]:
        now = self.clock()
        views = self.board.views(now)
        categories = score_categories(views, self.cfg.scoring)
        tracker = self.tracker
        easing = None
        if tracker.easing_to is not None:
            easing = {"to": tracker.easing_to.level.value, "at": tracker.lowers_at}
        return {
            "server_time": now,
            "version": __version__,
            "overall": {
                "level": tracker.current.level.value,
                "reason": tracker.current.reason,
                "since": tracker.since,
                "easing": easing,
            },
            "categories": [
                {
                    "id": cat.value,
                    "label": cat.label,
                    "level": result.level.value if result.level else None,
                    "reason": result.reason,
                    "sources": result.total_sources,
                }
                for cat, result in categories.items()
            ],
            "sources": [view.to_dict() for view in views],
            "alerts": self.alerts.status(),
        }

    def uptime(self, hours: float, bucket_minutes: int) -> list[dict[str, Any]]:
        """Connectivity history in buckets, for the 24-hour strip on the dashboard."""
        size = bucket_minutes * 60
        end = (int(self.clock()) // size + 1) * size
        start = end - int(hours * 3600) // size * size
        totals = self.db.bucketed_counts("probes", start, size)
        return [
            {
                "start": bucket,
                "rounds": totals.get((bucket, "rounds"), 0),
                "dark": totals.get((bucket, "dark"), 0),
                "partial": totals.get((bucket, "partial"), 0),
            }
            for bucket in range(start, end, size)
        ]
