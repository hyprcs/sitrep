"""Turn per-source levels into category levels and one overall level.

The rules (numbers come from config.yaml, under scoring):

- Sources that are learning, stale or pending don't count.
- A category takes the worst level among its live sources. A category can
  also be pushed to CRITICAL when several of its sources are elevated at the
  same time (social: two social sources together).
- Overall is OFFLINE when the probes say the internet can't be reached,
  CRITICAL when any category is critical, ELEVATED when at least two
  categories are elevated, and NORMAL otherwise.
- OverallTracker adds hysteresis so the overall level doesn't flicker.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ScoringSettings
from .models import (
    INTERNET_DOWN,
    OFFLINE_HEADLINE,
    SCORED_CATEGORIES,
    SEVERITY,
    Category,
    Level,
    Overall,
    is_live,
)
from .state import SourceView


@dataclass(frozen=True)
class CategoryResult:
    category: Category
    level: Level | None  # None when the category has no live sources
    reason: str
    blamed: tuple[SourceView, ...]  # the sources responsible for the level
    total_sources: int


@dataclass(frozen=True)
class OverallResult:
    level: Overall
    reason: str


def describe(view: SourceView) -> str:
    return f"{view.name}: {view.reason}"


def score_categories(views: list[SourceView], cfg: ScoringSettings) -> dict[Category, CategoryResult]:
    results: dict[Category, CategoryResult] = {}
    for category in SCORED_CATEGORIES:
        members = [v for v in views if v.category == category]
        live = [v for v in members if is_live(v.level)]
        if not live:
            reason = "No live sources right now" if members else "No sources set up yet"
            results[category] = CategoryResult(category, None, reason, (), len(members))
            continue

        worst = max((v.level for v in live), key=SEVERITY.__getitem__)
        raised = [v for v in live if v.level != Level.NORMAL]
        needed = cfg.category_critical_when_sources_elevated.get(category.value)
        if worst != Level.CRITICAL and needed and len(raised) >= needed:
            reason = f"{len(raised)} sources elevated together: " + "; ".join(map(describe, raised))
            results[category] = CategoryResult(category, Level.CRITICAL, reason, tuple(raised), len(members))
            continue

        blamed = tuple(v for v in live if v.level == worst)
        reason = "All normal" if worst == Level.NORMAL else "; ".join(map(describe, blamed))
        results[category] = CategoryResult(category, worst, reason, blamed, len(members))
    return results


def score_overall(
    views: list[SourceView], cfg: ScoringSettings
) -> tuple[OverallResult, dict[Category, CategoryResult]]:
    categories = score_categories(views, cfg)

    offline = [v for v in views if v.category == Category.CONNECTIVITY and INTERNET_DOWN in v.flags]
    if offline:
        reason = offline[0].reason.removeprefix(f"{OFFLINE_HEADLINE}: ")
        reason = f"{reason[:1].upper()}{reason[1:]}. Other tiles show their last-known state"
        return OverallResult(Overall.OFFLINE, reason), categories

    live = [c for c in categories.values() if c.level is not None]
    if not live:
        return OverallResult(Overall.UNKNOWN, "Waiting for the first results"), categories

    critical = [c for c in live if c.level == Level.CRITICAL]
    elevated = [c for c in live if c.level == Level.ELEVATED]
    if critical:
        reason = "; and ".join(c.reason for c in critical)
        if elevated:
            reason += ". Also elevated: " + "; ".join(c.reason for c in elevated)
        return OverallResult(Overall.CRITICAL, reason), categories
    if len(elevated) >= cfg.elevated_when_categories:
        return OverallResult(Overall.ELEVATED, "; and ".join(c.reason for c in elevated)), categories
    if elevated:
        only = elevated[0]
        reason = f"Nothing big. One category is elevated, which isn't enough on its own: {only.reason}"
        return OverallResult(Overall.NORMAL, reason), categories
    return OverallResult(Overall.NORMAL, "Nothing unusual from any source"), categories


_RANK = {Overall.NORMAL: 0, Overall.ELEVATED: 1, Overall.CRITICAL: 2}


class OverallTracker:
    """Hysteresis for the overall level.

    A higher level has to be seen raise_after_checks times in a row before it
    shows. A lower level only shows after lower_after_minutes of calm.
    UNKNOWN and OFFLINE are facts rather than judgments, so they switch at
    once (the probes already wait several rounds before declaring OFFLINE).
    """

    def __init__(self, cfg: ScoringSettings, now: float) -> None:
        self.cfg = cfg
        self.current = OverallResult(Overall.UNKNOWN, "Starting up")
        self.since = now
        self.easing_since: float | None = None  # when a lower level was first seen
        self.easing_to: OverallResult | None = None
        self._raise_count = 0

    def update(self, candidate: OverallResult, now: float) -> bool:
        """Feed one scoring result. Returns True if the shown level changed."""
        current = self.current.level
        if candidate.level == current:
            self.current = candidate  # same level, fresher reason
            self._calm()
            return False
        if current not in _RANK or candidate.level not in _RANK:
            return self._switch(candidate, now)
        if _RANK[candidate.level] > _RANK[current]:
            self.easing_since = self.easing_to = None
            self._raise_count += 1
            if self._raise_count >= self.cfg.raise_after_checks:
                return self._switch(candidate, now)
            return False
        self._raise_count = 0
        if self.easing_since is None:
            self.easing_since = now
        self.easing_to = candidate
        if now - self.easing_since >= self.cfg.lower_after_minutes * 60:
            return self._switch(candidate, now)
        return False

    @property
    def lowers_at(self) -> float | None:
        if self.easing_since is None:
            return None
        return self.easing_since + self.cfg.lower_after_minutes * 60

    def _calm(self) -> None:
        self._raise_count = 0
        self.easing_since = self.easing_to = None

    def _switch(self, candidate: OverallResult, now: float) -> bool:
        self.current = candidate
        self.since = now
        self._calm()
        return True
