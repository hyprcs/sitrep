"""The status board: the latest report from every source.

Adapters write to it, and the scorer and the dashboard read from it. Every
report is also saved to SQLite, so after a restart the dashboard still shows
each source's last-known state.

Every source is in exactly one of four states:

- ok: its latest report is current, so its level (normal, elevated or critical) counts;
- learning: it's still building its idea of normal, or waiting for its first update;
- fetch failing: its last tries in a row have failed, or it hasn't answered for too long;
- frozen: it answers, but the newest timestamp inside its data is older than the
  source's content-age budget. A source can keep answering while its data has stopped
  changing, so "the fetch worked" isn't proof the data is current.

Fetch failing and frozen sources show their last-known state, but never count.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .db import Database
from .models import Assessment, Category, Level, Signal, health, is_live
from .util import format_duration, sydney_clock


@dataclass
class SourceState:
    id: str
    name: str
    category: Category
    link: str | None
    stale_after: float
    started_at: float
    content_budget: float | None = None  # seconds; None means the source is quiet by nature
    default_scope: str | None = None
    failing_after: int = 2
    level: Level = Level.PENDING
    reason: str = "Waiting for the first update"
    details: dict[str, Any] = field(default_factory=dict)
    flags: frozenset[str] = frozenset()
    signals: tuple[Signal, ...] = ()
    content_at: float | None = None
    feed_at: float | None = None
    observed_at: float | None = None
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    last_error: str | None = None
    failures: int = 0


@dataclass(frozen=True)
class SourceView:
    """A source as it should be judged right now, with its state worked out."""

    id: str
    name: str
    category: Category
    link: str | None
    level: Level
    reason: str
    flags: frozenset[str]
    details: dict[str, Any]
    last_known_level: Level
    last_known_reason: str
    observed_at: float | None
    last_success_at: float | None
    last_attempt_at: float | None
    last_error: str | None
    failures: int
    signals: tuple[Signal, ...] = ()
    content_at: float | None = None
    feed_at: float | None = None
    content_budget: float | None = None
    since: float | None = None  # when it started failing, or the time its data froze at
    # True for stand-ins worked out from other sources (stories, terms, convergence), which
    # mustn't count as independent sources.
    derived: bool = False

    @property
    def health(self) -> str:
        return health(self.level)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category.value,
            "link": self.link,
            "level": self.level.value,
            "health": self.health,
            "reason": self.reason,
            "flags": sorted(self.flags),
            "details": self.details,
            "signals": [s.to_dict() for s in self.signals],
            "last_known_level": self.last_known_level.value,
            "last_known_reason": self.last_known_reason,
            "observed_at": self.observed_at,
            "last_success_at": self.last_success_at,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
            "failures": self.failures,
            "content_at": self.content_at,
            "feed_at": self.feed_at,
            "content_budget": self.content_budget,
            "since": self.since,
        }


def _first_sentence(text: str) -> str:
    head, dot, _ = text.partition(". ")
    return head if dot else text.rstrip(".")


def describe_gap(view: SourceView, now: float) -> str | None:
    """One plain line saying what sitrep can't see because of this source, or None if it's ok."""
    if view.level is Level.FAILING:
        if view.last_success_at is None:
            text = f"No {view.name} data since sitrep started"
        else:
            text = f"No {view.name} data for {format_duration(now - view.last_success_at)}"
        return text + (f" (last error: {view.last_error})" if view.last_error else "")
    if view.level is Level.FROZEN:
        if view.content_at is None:
            return f"{view.name} is answering, but its data has no dates, so sitrep can't tell if it's current"
        return (f"{view.name} frozen since {sydney_clock(view.content_at, now)}: its newest data is "
                f"{format_duration(now - view.content_at)} old")
    if view.level in (Level.LEARNING, Level.PENDING):
        if view.last_success_at is None:
            return f"{view.name}: waiting for the first update"
        return f"{view.name}: {_first_sentence(view.reason)}"
    return None


class StatusBoard:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock
        self._sources: dict[str, SourceState] = {}

    def register(
        self, source_id: str, name: str, category: Category, link: str | None, stale_after: float,
        *, content_budget: float | None = None, default_scope: str | None = None, failing_after: int = 2,
    ) -> None:
        state = SourceState(
            source_id, name, category, link, stale_after, started_at=self._clock(),
            content_budget=content_budget, default_scope=default_scope, failing_after=failing_after,
        )
        saved = self._db.load_source_status(source_id)
        if saved:
            state.level = Level(saved["level"]) if saved["level"] in Level._value2member_map_ else Level.PENDING
            state.reason = saved["reason"]
            state.details = saved["details"]
            state.flags = saved["flags"]
            state.signals = tuple(Signal.from_dict(s) for s in saved["signals"])
            state.content_at = saved["content_at"]
            state.feed_at = saved["feed_at"]
            state.observed_at = saved["observed_at"]
            state.last_attempt_at = saved["last_attempt_at"]
            state.last_success_at = saved["last_success_at"]
            state.last_error = saved["last_error"]
        self._sources[source_id] = state

    def report(self, source_id: str, assessment: Assessment) -> None:
        now = self._clock()
        state = self._sources[source_id]
        state.level = assessment.level
        state.reason = assessment.reason
        state.details = assessment.details
        state.flags = assessment.flags
        state.signals = self._signals_for(state, assessment)
        state.content_at = assessment.content_at
        state.feed_at = assessment.feed_at
        state.observed_at = state.last_attempt_at = state.last_success_at = now
        state.last_error = None
        state.failures = 0
        self._save(state)

    def report_failure(self, source_id: str, error: str) -> None:
        state = self._sources[source_id]
        state.last_attempt_at = self._clock()
        state.last_error = error
        state.failures += 1
        self._save(state)

    def views(self, now: float | None = None) -> list[SourceView]:
        now = self._clock() if now is None else now
        return [self._view(state, now) for state in self._sources.values()]

    @staticmethod
    def _signals_for(state: SourceState, assessment: Assessment) -> tuple[Signal, ...]:
        """The report's signals, or one standing for the whole source if a raised report has none."""
        if assessment.signals or not is_live(assessment.level) or assessment.level is Level.NORMAL:
            return assessment.signals
        scopes = (state.default_scope,) if state.default_scope else ()
        return (Signal(state.id, assessment.level, scopes, title=assessment.reason, at=assessment.content_at),)

    def _view(self, state: SourceState, now: float) -> SourceView:
        level, reason, since = state.level, state.reason, None
        failing = state.failures >= state.failing_after and state.last_error
        if state.last_success_at is None:
            if failing or now - state.started_at > state.stale_after:
                level, reason, since = Level.FAILING, "No successful update yet", state.started_at
            else:
                level, reason = Level.LEARNING, "Waiting for the first update"
        elif failing or now - state.last_success_at > state.stale_after:
            level, since = Level.FAILING, state.last_success_at
            reason = f"No update for {format_duration(now - state.last_success_at)}"
        elif state.content_budget is not None and state.level is not Level.PENDING and (
            state.content_at is None or now - state.content_at > state.content_budget
        ):
            level, since = Level.FROZEN, state.content_at
            if state.content_at is None:
                reason = "It answers, but its data has no dates, so sitrep can't tell if it's current"
            else:
                reason = (f"It answers, but its newest data is {format_duration(now - state.content_at)} old "
                          f"(it usually updates within {format_duration(state.content_budget)})")
        elif level is Level.PENDING:
            level = Level.LEARNING
        if level is Level.FAILING and state.last_error:
            reason += f" (last error: {state.last_error})"
        live = is_live(level)
        return SourceView(
            id=state.id,
            name=state.name,
            category=state.category,
            link=state.link,
            level=level,
            reason=reason,
            flags=state.flags if live else frozenset(),  # old data can't raise alarms
            details=state.details,
            last_known_level=state.level,
            last_known_reason=state.reason,
            observed_at=state.observed_at,
            last_success_at=state.last_success_at,
            last_attempt_at=state.last_attempt_at,
            last_error=state.last_error,
            failures=state.failures,
            signals=state.signals if live else (),
            content_at=state.content_at,
            feed_at=state.feed_at,
            content_budget=state.content_budget,
            since=since,
        )

    def _save(self, state: SourceState) -> None:
        self._db.save_source_status(
            state.id,
            level=state.level.value,
            reason=state.reason,
            details=state.details,
            flags=state.flags,
            observed_at=state.observed_at,
            last_attempt_at=state.last_attempt_at,
            last_success_at=state.last_success_at,
            last_error=state.last_error,
            content_at=state.content_at,
            feed_at=state.feed_at,
            signals=[s.to_dict() for s in state.signals],
        )
