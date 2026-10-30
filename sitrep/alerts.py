"""Alerts: tell you on your phone when the overall level changes.

The rules (numbers come from config.yaml, under alerts):

- Worse news goes out at once: a level higher than the last one you were told about.
- Better news waits. The overall level already needs 15 calm minutes before it comes
  down; the message ("all clear", or "eased to elevated") is only sent once the level
  has stayed lower for all_clear_after_minutes more. If it flares up again before then,
  nothing is sent at all, because what you were last told is still true. That's the
  cooldown: one situation gives one alert and one all-clear, however much it flickers.
- Better news is sent silently (no sound); worse news makes a sound.
- Nothing can be sent while the internet is down. Once it's back, one message says how
  long it was down, if it was down at least internet_outage_min_minutes.
- What you were last told is saved, so a restart doesn't repeat an alert.

A Notifier delivers alerts somewhere (Telegram now; a Meshtastic mesh could be another).
Each keeps only the newest undelivered alert and retries with backoff until it gets through.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from .adapters.base import retry_delay
from .config import AlertsSettings
from .db import Database
from .models import Overall

log = logging.getLogger("sitrep.alerts")

RANK = {Overall.NORMAL: 0, Overall.ELEVATED: 1, Overall.CRITICAL: 2}
TOLD_KEY = "alerts:told"


@dataclass(frozen=True)
class Alert:
    kind: str  # "raised", "lowered" or "back_online"
    level: Overall  # the level being announced
    reason: str
    at: float
    told: Overall | None = None  # what you'd been told before this
    told_at: float | None = None
    lowered_at: float | None = None  # "lowered": when the level first came down
    offline: tuple[float, float] | None = None  # "back_online": when the internet was down
    loud: bool = True  # False sends it silently


class AlertPolicy:
    """Decides which changes in the overall level are worth a message."""

    def __init__(self, cfg: AlertsSettings, told: Overall | None = None, told_at: float | None = None) -> None:
        self.cfg = cfg
        self.told = told
        self.told_at = told_at
        self.lowering_since: float | None = None
        self.offline_since: float | None = None

    def update(self, level: Overall, reason: str, now: float) -> Alert | None:
        """Feed the shown overall level (every scoring pass). Returns an alert to send, or None."""
        if level == Overall.UNKNOWN:
            return None
        if level == Overall.OFFLINE:
            if self.offline_since is None:
                self.offline_since = now
            return None

        if self.offline_since is not None:
            down_from, self.offline_since = self.offline_since, None
            if self.cfg.report_internet_outages and now - down_from >= self.cfg.internet_outage_min_minutes * 60:
                louder = self.told is None or RANK[level] > RANK[self.told]
                alert = Alert("back_online", level, reason, now, self.told, self.told_at,
                              offline=(down_from, now), loud=louder and level != Overall.NORMAL)
                self._tell(level, now)
                return alert

        if self.told is None:
            if level == Overall.NORMAL:
                self._tell(level, now)  # starting up calm isn't news
                return None
            return self._raise(level, reason, now)
        if RANK[level] > RANK[self.told]:
            return self._raise(level, reason, now)
        if level == self.told:
            self.lowering_since = None
            return None
        if self.lowering_since is None:
            self.lowering_since = now
        if now - self.lowering_since < self.cfg.all_clear_after_minutes * 60:
            return None
        alert = Alert("lowered", level, reason, now, self.told, self.told_at,
                      lowered_at=self.lowering_since, loud=False)
        self._tell(level, now)
        return alert

    def _raise(self, level: Overall, reason: str, now: float) -> Alert:
        alert = Alert("raised", level, reason, now, self.told, self.told_at)
        self._tell(level, now)
        return alert

    def _tell(self, level: Overall, now: float) -> None:
        self.told, self.told_at = level, now
        self.lowering_since = None


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


class Alerts:
    """Runs the policy on every scoring pass and hands alerts to each notifier."""

    def __init__(self, cfg: AlertsSettings, db: Database, notifiers: list[Notifier],
                 off: list[dict[str, Any]] | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.notifiers = notifiers
        self.off = off or []  # status rows for notifiers that aren't set up
        told, told_at = self._load_told()
        self.policy = AlertPolicy(cfg, told, told_at)

    def update(self, level: Overall, reason: str, now: float) -> Alert | None:
        if not self.notifiers:
            return None
        before = (self.policy.told, self.policy.told_at)
        alert = self.policy.update(level, reason, now)
        if (self.policy.told, self.policy.told_at) != before and self.policy.told is not None:
            self.db.kv_set(TOLD_KEY, json.dumps({"level": self.policy.told.value, "at": self.policy.told_at}))
        if alert is not None:
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
