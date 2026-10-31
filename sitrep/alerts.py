"""Alerts: tell you on your phone when the overall level changes.

The rules (numbers come from config.yaml, under alerts):

- Worse news goes out at once: a level higher than the last one you were told about.
  Critical makes a sound; elevated arrives silently (unless elevated_makes_sound is on).
- Better news waits. The overall level already needs 15 calm minutes before it comes
  down; the message ("all clear", or "eased to elevated") is only sent once the level
  has stayed lower for all_clear_after_minutes more. If it flares up again before then,
  nothing is sent at all, because what you were last told is still true. That's the
  cooldown: one situation gives one alert and one all-clear, however much it flickers.
  Better news is always silent.
- Alerts only count signals that are new enough (see scoring.alerting_views): items that
  were already old when sitrep first saw them are backlog. They show on the dashboard,
  but can't set off a message.
- No alerts for startup_grace_minutes after sitrep starts or the internet comes back,
  while the sources catch up.
- Every event has a key. An alert caused only by events that already alerted in the last
  repeat_after_minutes isn't sent. While you've been told it's critical, a new critical
  event gets its own message, so a second emergency isn't hidden behind the first.
- Nothing can be sent while the internet is down. Once it's back (and the grace period
  is over), one "while you were offline" message says how long it was down, what came in
  meanwhile, and how things stand now, instead of a burst of individual alerts. The same
  happens after sitrep itself was down for summary_after_restart_minutes or more.
- What you were last told is saved, so a restart doesn't repeat an alert.

A Notifier delivers alerts somewhere (Telegram now; a MeshCore mesh could be another).
Each keeps only the newest undelivered alert and retries with backoff until it gets through.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .adapters.base import retry_delay
from .config import AlertsSettings
from .db import Database
from .models import Level, Overall, Signal

log = logging.getLogger("sitrep.alerts")

RANK = {Overall.NORMAL: 0, Overall.ELEVATED: 1, Overall.CRITICAL: 2}
TOLD_KEY = "alerts:told"
# A new critical event has to be seen on this many scoring passes in a row before it alerts.
NEW_EVENT_CHECKS = 2


@dataclass(frozen=True)
class Alert:
    kind: str  # "raised", "lowered", "another" (a new critical event) or "summary"
    level: Overall  # the level being announced
    reason: str
    at: float
    told: Overall | None = None  # what you'd been told before this
    told_at: float | None = None
    lowered_at: float | None = None  # "lowered": when the level first came down
    offline: tuple[float, float] | None = None  # "summary": when sitrep couldn't send
    restarted: bool = False  # "summary": sitrep itself was down, rather than the internet
    shown: Overall | None = None  # "summary": the dashboard's level, which counts the backlog too
    shown_reason: str = ""
    items: dict[str, Any] = field(default_factory=dict)  # "summary": what came in meanwhile
    loud: bool = True  # False sends it silently
    keys: tuple[str, ...] = ()  # the events it's about


def _marked(level: Overall, key: str) -> str:
    return f"{level.value}:{key}"


class AlertPolicy:
    """Decides which changes in the overall level are worth a message."""

    def __init__(
        self, cfg: AlertsSettings, told: Overall | None = None, told_at: float | None = None, *,
        started_at: float | None = None, alerted: dict[str, float] | None = None,
        restart_gap: tuple[float, float] | None = None,
    ) -> None:
        self.cfg = cfg
        self.told = told
        self.told_at = told_at
        self.lowering_since: float | None = None
        self.offline_since: float | None = None
        grace = cfg.startup_grace_minutes * 60
        self.quiet_until = started_at + grace if started_at is not None else None
        # "<level>:<event key>" -> when that event last alerted at that level. An event
        # that alerted as elevated can still alert again when it becomes critical.
        self.alerted: dict[str, float] = dict(alerted or {})
        self.announced: set[str] = set()  # critical events you've been told about this time
        self._candidates: dict[str, int] = {}
        self.summary_window: tuple[float, float] | None = restart_gap
        self.summary_restarted = restart_gap is not None

    # --- The main entry point ---

    def update(
        self, level: Overall, reason: str, now: float, *, drivers: tuple[Signal, ...] = (),
        shown: Overall | None = None, shown_reason: str = "",
    ) -> Alert | None:
        """Feed the overall level alerts see (every scoring pass). Returns an alert to send, or None.

        drivers are the signals behind the level; shown is the dashboard's own level,
        which also counts backlog.
        """
        if level == Overall.UNKNOWN:
            return None
        if level == Overall.OFFLINE:
            if self.offline_since is None:
                self.offline_since = now
            return None

        if self.offline_since is not None:
            down_from, self.offline_since = self.offline_since, None
            self.quiet_until = now + self.cfg.startup_grace_minutes * 60
            if self.cfg.report_internet_outages and now - down_from >= self.cfg.internet_outage_min_minutes * 60:
                start = self.summary_window[0] if self.summary_window else down_from
                self.summary_window, self.summary_restarted = (start, now), False
        if self.quiet_until is not None and now < self.quiet_until:
            return None  # sources are still catching up

        if self.summary_window is not None:
            return self._summary(level, reason, now, drivers, shown or level, shown_reason)
        if self.told is None:
            if level == Overall.NORMAL:
                self._tell(level, now)  # starting up calm isn't news
                return None
            return self._raise(level, reason, now, drivers)
        if RANK[level] > RANK[self.told]:
            return self._raise(level, reason, now, drivers)
        if level == self.told:
            self.lowering_since = None
            if level == Overall.CRITICAL:
                return self._another(now, drivers)
            return None
        self._candidates.clear()
        if self.lowering_since is None:
            self.lowering_since = now
        if now - self.lowering_since < self.cfg.all_clear_after_minutes * 60:
            return None
        alert = Alert("lowered", level, reason, now, self.told, self.told_at,
                      lowered_at=self.lowering_since, loud=False)
        self._tell(level, now)
        return alert

    # --- The kinds of message ---

    def _raise(self, level: Overall, reason: str, now: float, drivers: tuple[Signal, ...]) -> Alert | None:
        keys = {_marked(level, s.key) for s in drivers}
        if keys and all(self._recent(k, now) for k in keys):
            self._tell(level, now)  # only events you've just been told about at this level: say nothing
            self._note_critical(level, drivers)
            return None
        alert = Alert("raised", level, reason, now, self.told, self.told_at, loud=self._sound(level),
                      keys=tuple(sorted(keys)))
        self._mark(keys, now)
        self._tell(level, now)
        self._note_critical(level, drivers)
        return alert

    def _another(self, now: float, drivers: tuple[Signal, ...]) -> Alert | None:
        """A critical event you haven't been told about, while you've already been told it's critical."""
        new = [s for s in drivers if s.level == Level.CRITICAL and s.key not in self.announced
               and not self._recent(_marked(Overall.CRITICAL, s.key), now)]
        seen = {s.key for s in new}
        self._candidates = {k: self._candidates.get(k, 0) + 1 for k in seen}
        ready = [s for s in new if self._candidates[s.key] >= NEW_EVENT_CHECKS]
        if not ready:
            return None
        keys = {s.key for s in ready}
        for key in keys:
            self._candidates.pop(key, None)
        self.announced |= keys
        marks = {_marked(Overall.CRITICAL, k) for k in keys}
        self._mark(marks, now)
        reason = "; ".join(dict.fromkeys(s.title for s in ready if s.title)) or "Another critical event"
        return Alert("another", Overall.CRITICAL, reason, now, self.told, self.told_at, loud=True,
                     keys=tuple(sorted(marks)))

    def _summary(
        self, level: Overall, reason: str, now: float, drivers: tuple[Signal, ...], shown: Overall,
        shown_reason: str,
    ) -> Alert:
        start, end = self.summary_window or (now, now)
        louder = self.told is None or RANK[level] > RANK[self.told]
        keys = {_marked(level, s.key) for s in drivers} if level in (Overall.ELEVATED, Overall.CRITICAL) else set()
        alert = Alert(
            "summary", level, reason, now, self.told, self.told_at, offline=(start, end),
            restarted=self.summary_restarted, shown=shown, shown_reason=shown_reason or reason,
            loud=shown == Overall.CRITICAL or (louder and self._sound(level)), keys=tuple(sorted(keys)),
        )
        self.summary_window = None
        self._mark(keys, now)
        self._tell(level, now)
        self._note_critical(level, drivers)
        return alert

    # --- Bookkeeping ---

    def _sound(self, level: Overall) -> bool:
        return level == Overall.CRITICAL or (level == Overall.ELEVATED and self.cfg.elevated_makes_sound)

    def _recent(self, key: str, now: float) -> bool:
        at = self.alerted.get(key)
        return at is not None and now - at < self.cfg.repeat_after_minutes * 60

    def _mark(self, keys: set[str], now: float) -> None:
        for key in keys:
            self.alerted[key] = now

    def _note_critical(self, level: Overall, drivers: tuple[Signal, ...]) -> None:
        if level == Overall.CRITICAL:
            self.announced |= {s.key for s in drivers}

    def _tell(self, level: Overall, now: float) -> None:
        self.told, self.told_at = level, now
        self.lowering_since = None
        self._candidates.clear()
        if level != Overall.CRITICAL:
            self.announced.clear()


class NotifyError(Exception):
    """Delivery failed. The message says why in plain English.

    final is True when trying again won't help until the settings change (a wrong
    token, say), and False for passing trouble (the service couldn't be reached).
    """

    def __init__(self, message: str, *, final: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.final = final
        self.retry_after = retry_after


class Notifier(ABC):
    """Somewhere alerts are delivered. Subclasses fill in check() and send()."""

    name: str
    retry_first_seconds = 30.0
    retry_max_seconds = 1800.0

    def __init__(self, db: Database, clock: Callable[[], float]) -> None:
        self.db = db
        self.clock = clock
        self.pending: Alert | None = None
        self.failures = 0
        self.problem: str | None = None  # why alerts can't be delivered right now
        self.who = ""  # where alerts go, for the dashboard (e.g. the bot's name)
        self._wake = asyncio.Event()
        saved = db.kv_get(self._last_sent_key)
        self.last_sent_at: float | None = float(saved) if saved else None

    @property
    def _last_sent_key(self) -> str:
        return f"alerts:{self.name.lower()}:last_sent"

    @abstractmethod
    async def check(self) -> None:
        """Make sure the settings work. Raise NotifyError if they don't."""

    @abstractmethod
    async def send(self, alert: Alert) -> None:
        """Deliver one alert. Raise NotifyError if it didn't get through."""

    def queue(self, alert: Alert) -> None:
        self.pending = alert  # a newer alert replaces one that hasn't got through yet
        self._wake.set()

    async def run(self) -> None:
        await asyncio.gather(self._check_until_answered(), self._deliver())

    async def _deliver(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.pending is not None:
                alert = self.pending
                try:
                    await self.send(alert)
                except NotifyError as exc:
                    self.failures += 1
                    self.problem = str(exc)
                    delay = retry_delay(self.failures, self.retry_first_seconds, self.retry_max_seconds, exc.retry_after)
                    log.warning("%s alert not sent (%s); trying again in %.0f seconds", self.name, exc, delay)
                    await asyncio.sleep(delay)
                    continue
                if self.pending is alert:
                    self.pending = None
                self.failures = 0
                self.problem = None
                self.last_sent_at = self.clock()
                self.db.kv_set(self._last_sent_key, str(self.last_sent_at))
                log.info("%s alert sent: %s", self.name, alert.level.value)

    async def _check_until_answered(self) -> None:
        """Check the settings at start-up. Keep trying while the service can't be reached."""
        tries = 0
        while True:
            try:
                await self.check()
                self.problem = None
                return
            except NotifyError as exc:
                self.problem = str(exc)
                log.warning("%s alerts: %s", self.name, exc)
                if exc.final:
                    return  # a definite answer (a wrong token, say); retrying won't change it
                tries += 1
                await asyncio.sleep(retry_delay(tries, self.retry_first_seconds, self.retry_max_seconds, exc.retry_after))

    def status(self) -> dict[str, Any]:
        state = "problem" if self.problem else "on"
        return {"name": self.name, "state": state, "detail": self.problem or self.who,
                "last_sent_at": self.last_sent_at, "waiting": self.pending is not None}


def summarise_items(items: list[dict[str, Any]], names: dict[str, str], top: int) -> dict[str, Any]:
    """Counts per source and the most important items, for the "while you were offline" message."""
    counts: dict[str, int] = {}
    for item in items:
        name = names.get(item["source_id"], item["source_id"])
        counts[name] = counts.get(name, 0) + 1
    order = {"critical": 0, "elevated": 1, "watch": 2}
    ranked = sorted(items, key=lambda i: (order.get(i["level"], 3), -(i["ts"] or i["version_seen"])))
    return {
        "total": len(items),
        "counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "top": [
            {"source": names.get(i["source_id"], i["source_id"]), "title": i["title"], "level": i["level"],
             "ts": i["ts"], "url": i["url"]}
            for i in ranked[:top]
        ],
    }


class Alerts:
    """Runs the policy on every scoring pass and hands alerts to each notifier."""

    def __init__(self, cfg: AlertsSettings, db: Database, notifiers: list[Notifier],
                 off: list[dict[str, Any]] | None = None, *, started_at: float | None = None,
                 restart_gap: tuple[float, float] | None = None, names: dict[str, str] | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.notifiers = notifiers
        self.off = off or []  # status rows for notifiers that aren't set up
        self.names = names or {}  # source id -> name, for the summary
        told, told_at = self._load_told()
        alerted = self._load_alerted(started_at)
        self.policy = AlertPolicy(cfg, told, told_at, started_at=started_at, alerted=alerted, restart_gap=restart_gap)

    def update(
        self, level: Overall, reason: str, now: float, *, drivers: tuple[Signal, ...] = (),
        shown: Overall | None = None, shown_reason: str = "",
    ) -> Alert | None:
        if not self.notifiers:
            return None
        before = (self.policy.told, self.policy.told_at)
        alert = self.policy.update(level, reason, now, drivers=drivers, shown=shown, shown_reason=shown_reason)
        if (self.policy.told, self.policy.told_at) != before and self.policy.told is not None:
            self.db.kv_set(TOLD_KEY, json.dumps({"level": self.policy.told.value, "at": self.policy.told_at}))
        if alert is None:
            return None
        if alert.keys:
            self.db.mark_alerted(list(alert.keys), now)
        if alert.kind == "summary" and alert.offline:
            start, end = alert.offline
            items = summarise_items(self.db.items_seen_between(start, now), self.names, self.cfg.summary_top_items)
            alert = replace(alert, items=items)
        log.info("Alert: %s (%s)", alert.level.value, alert.kind)
        for notifier in self.notifiers:
            notifier.queue(alert)
        return alert

    async def run(self) -> None:
        if self.notifiers:
            await asyncio.gather(*(n.run() for n in self.notifiers))

    def status(self) -> list[dict[str, Any]]:
        return [n.status() for n in self.notifiers] + self.off

    def _load_told(self) -> tuple[Overall | None, float | None]:
        try:
            saved = json.loads(self.db.kv_get(TOLD_KEY) or "null")
            return Overall(saved["level"]), float(saved["at"])
        except (TypeError, KeyError, ValueError):
            return None, None

    def _load_alerted(self, now: float | None) -> dict[str, float]:
        if now is None:
            return {}
        since = now - self.cfg.repeat_after_minutes * 60
        rows = self.db.query("SELECT key, alerted_at FROM alert_keys WHERE alerted_at >= ?", (since,))
        return {row["key"]: row["alerted_at"] for row in rows}
