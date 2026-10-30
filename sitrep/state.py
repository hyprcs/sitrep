"""The status board: the latest report from every source.

Adapters write to it, and the scorer and the dashboard read from it. Every
report is also saved to SQLite, so after a restart the dashboard still shows
each source's last-known state (marked stale if it's too old).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .db import Database
from .models import Assessment, Category, Level, is_live
from .util import format_duration


@dataclass
class SourceState:
    id: str
    name: str
    category: Category
    link: str | None
    stale_after: float
    started_at: float
    level: Level = Level.PENDING
    reason: str = "Waiting for the first update"
    details: dict[str, Any] = field(default_factory=dict)
    flags: frozenset[str] = frozenset()
    observed_at: float | None = None
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    last_error: str | None = None
    failures: int = 0


@dataclass(frozen=True)
class SourceView:
    """A source as it should be judged right now, with staleness applied."""

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category.value,
            "link": self.link,
            "level": self.level.value,
            "reason": self.reason,
            "flags": sorted(self.flags),
            "details": self.details,
            "last_known_level": self.last_known_level.value,
            "last_known_reason": self.last_known_reason,
            "observed_at": self.observed_at,
            "last_success_at": self.last_success_at,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
            "failures": self.failures,
        }


class StatusBoard:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock
        self._sources: dict[str, SourceState] = {}

    def register(
        self, source_id: str, name: str, category: Category, link: str | None, stale_after: float
    ) -> None:
        state = SourceState(source_id, name, category, link, stale_after, started_at=self._clock())
        saved = self._db.load_source_status(source_id)
        if saved:
            state.level = Level(saved["level"])
            state.reason = saved["reason"]
            state.details = saved["details"]
            state.flags = saved["flags"]
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

    def _view(self, state: SourceState, now: float) -> SourceView:
        level, reason, flags = state.level, state.reason, state.flags
        if state.last_success_at is None:
            if now - state.started_at > state.stale_after:
                level, reason = Level.STALE, "No successful update yet"
            else:
                level = Level.PENDING
        elif now - state.last_success_at > state.stale_after:
            level = Level.STALE
            reason = f"No update for {format_duration(now - state.last_success_at)}"
        if level is Level.STALE and state.last_error:
            reason += f" (last error: {state.last_error})"
        if not is_live(level):
            flags = frozenset()  # old data can't raise alarms
        return SourceView(
            id=state.id,
            name=state.name,
            category=state.category,
            link=state.link,
            level=level,
            reason=reason,
            flags=flags,
            details=state.details,
            last_known_level=state.level,
            last_known_reason=state.reason,
            observed_at=state.observed_at,
            last_success_at=state.last_success_at,
            last_attempt_at=state.last_attempt_at,
            last_error=state.last_error,
            failures=state.failures,
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
        )
