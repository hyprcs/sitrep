"""Wires everything together and runs the background tasks:

- one task per enabled source (restarted if it ever crashes),
- the scorer, which works out the overall level every 30 seconds: once for the
  dashboard, counting everything, and once for alerts, counting only what's new
  enough to alert on,
- story clusters, worked out on each scoring pass from the items sources have seen,
- alerts, which message your phone when the overall level changes (if set up),
- housekeeping, which deletes data older than the retention settings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from pydantic import ValidationError

from . import __version__
from .adapters import REGISTRY
from .adapters.base import AdapterContext, AdapterSettings, SourceAdapter, retry_delay
from .alerts import Alerts, Notifier
from .config import Config, ConfigError, format_errors
from .db import Database
from .models import Category, Level, Overall, is_live
from .adapters.bluesky import DEFAULT_GROUPS, KeywordGroup, compile_groups, matching_groups
from .scoring import OverallTracker, alerting_views, score_categories, score_overall
from .convergence import Convergence
from .stories import Stories
from .terms import Terms
from .state import SourceView, StatusBoard, describe_gap
from .telegram import TelegramNotifier, off_status

log = logging.getLogger("sitrep")

HOUSEKEEPING_EVERY_SECONDS = 6 * 3600
HEARTBEAT_KEY = "runtime:heartbeat"  # when sitrep last scored, to spot time it wasn't running


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
        self.tracker = OverallTracker(cfg.scoring, clock())  # what the dashboard shows
        self.alert_tracker = OverallTracker(cfg.scoring, clock())  # what alerts see
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
        ctx = AdapterContext(self.cfg, self.db, self.board, self.http, self.clock)
        for adapter_cls, settings in self.specs:
            adapter = adapter_cls(ctx, settings)
            adapter.register()
            self.adapters[adapter.id] = adapter
        self.alerts = self._build_alerts()
        self.stories = self._build_stories()
        # Worked out inside sitrep rather than fetched, so it's never something sitrep can't see.
        self.board.register("stories", "Story clusters", Category.INFO, None, stale_after=24 * 3600)
        self.board.report("stories", self.stories.assessment(self.clock()))
        self.convergence = Convergence(
            self.db, self.cfg.convergence, self.clock, keyword_groups=self.stories.keyword_groups,
            posts=getattr(self.adapters.get("bluesky"), "recent_mentions", None),
        )
        if self.cfg.convergence.enabled:
            self.board.register("convergence", "Convergence", Category.INFO, None, stale_after=24 * 3600)
            self.board.report("convergence", self.convergence.assessment(self.clock()))
        self.terms = Terms(self.db, self.cfg.terms, self.clock,
                           posts=getattr(self.adapters.get("bluesky"), "recent_posts", None))
        if self.cfg.terms.enabled:
            self.board.register("terms", "Emerging terms", Category.INFO, None, stale_after=24 * 3600)
            self.terms.update(self.clock())
            self.board.report("terms", self.terms.assessment(self.clock()))

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
        return Alerts(cfg, self.db, notifiers, off, started_at=self.clock(), restart_gap=self._restart_gap(),
                      names={a.id: a.name for a in self.adapters.values()})

    def _build_stories(self) -> Stories:
        bluesky = self.adapters.get("bluesky")
        groups = getattr(bluesky, "_compiled", None) or compile_groups([KeywordGroup(**g) for g in DEFAULT_GROUPS])
        return Stories(
            self.db, self.clock,
            names={a.id: (a.name, a.category) for a in self.adapters.values()},
            keyword_groups=lambda text: matching_groups(text, groups),
            social=getattr(bluesky, "recent_posts", None),
        )

    def _restart_gap(self) -> tuple[float, float] | None:
        """When sitrep wasn't running, if it was down long enough to be worth a summary."""
        now = self.clock()
        try:
            last = float(self.db.kv_get(HEARTBEAT_KEY) or "")
        except ValueError:
            return None
        if now - last >= self.cfg.alerts.summary_after_restart_minutes * 60:
            return last, now
        return None

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
        """One scoring pass: log source changes, update the overall level, tell alerts."""
        now = self.clock()
        self.stories.update(now)
        self.board.report("stories", self.stories.assessment(now))
        if self.cfg.terms.enabled:
            self.terms.update(now)
            self.board.report("terms", self.terms.assessment(now))
        if self.cfg.convergence.enabled:
            self.convergence.update(now)
            self.board.report("convergence", self.convergence.assessment(now))
        views = self.board.views(now)
        self._log_source_changes(views, now)
        candidate, _ = score_overall(views + self._derived(now), self.cfg.scoring)
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
            if previous == Overall.OFFLINE:
                self._wake_sources()
        alerting, _ = score_overall(alerting_views(views) + self._derived(now, alertable_only=True),
                                    self.cfg.scoring)
        self.alert_tracker.update(alerting, now)
        seen, shown = self.alert_tracker.current, self.tracker.current
        self.alerts.update(seen.level, seen.reason, now, drivers=seen.drivers,
                           shown=shown.level, shown_reason=shown.reason)
        self.db.kv_set(HEARTBEAT_KEY, str(now))

    def _derived(self, now: float, alertable_only: bool = False) -> list[SourceView]:
        """Stand-in views from signals that look across sources: stories, terms and convergence."""
        views = self.stories.views(now, alertable_only)
        if self.cfg.terms.enabled:
            views += self.terms.views(now, alertable_only)
        if self.cfg.convergence.enabled:
            views += self.convergence.views(now, alertable_only)
        return views

    def _wake_sources(self) -> None:
        """The internet is back: every source looks again now, instead of waiting out its retry delay."""
        for adapter in self.adapters.values():
            adapter.wake()

    def _log_source_changes(self, views: list[SourceView], now: float) -> None:
        for view in views:
            if view.id in ("stories", "convergence"):
                continue  # these put themselves on the timeline as they grow
            before = self._last_levels.get(view.id)
            self._last_levels[view.id] = view.level
            if before is None or before == view.level:
                continue
            if before in (Level.PENDING, Level.LEARNING) and view.level in (Level.NORMAL, Level.LEARNING):
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
        categories = score_categories(views + self._derived(now), self.cfg.scoring)
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
            "gaps": [
                {"id": view.id, "name": view.name, "health": view.health, "since": view.since, "text": text}
                for view in views
                if (text := describe_gap(view, now))
            ] + [
                # A source made of several feeds keeps counting with the ones that work,
                # but each failing feed is still something sitrep can't see.
                {"id": view.id, "name": view.name, "health": "failing", "since": None, "text": f"{view.name}: {problem}"}
                for view in views if view.health == "ok"
                for problem in view.details.get("feeds_failing") or []
            ],
            "alerts": self.alerts.status(),
        }

    def map_payload(self) -> dict[str, Any]:
        """Home, the basemap, and every point sources have put in details["map"]."""
        now = self.clock()
        home = self.cfg.home.location
        tiles = Path(self.cfg.map.tiles_path)
        points = []
        for view in self.board.views(now):
            for point in view.details.get("map") or []:
                points.append({**point, "source": view.id, "source_name": view.name, "live": is_live(view.level)})
        return {
            "server_time": now,
            "home": {"name": home.name, "lat": home.lat, "lon": home.lon},
            "bbox": self.cfg.map.bbox,
            "basemap": {"url": f"tiles/{tiles.name}", "present": tiles.is_file(),
                        "made_at": tiles.stat().st_mtime if tiles.is_file() else None},
            "points": points,
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
