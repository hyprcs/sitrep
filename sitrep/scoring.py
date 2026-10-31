"""Turn per-source levels into category levels and one overall level.

The rules (numbers come from config.yaml, under scoring):

- Only sources that are ok count: learning, fetch failing and frozen ones don't.
- A category takes the worst level among its sources. A category can also be pushed
  to CRITICAL when several of its sources are elevated at the same time (social:
  two social sources together).
- Overall is OFFLINE when the probes say the internet can't be reached, and
  CRITICAL when any category is critical.
- Overall is ELEVATED when two categories are elevated about what is plausibly the
  same event. Every raised signal names its scope (see models.py), and two
  categories back each other up when they share a story cluster, sit in the same or
  neighbouring map squares, are both about Sydney as a whole, or are both national
  with related themes (the same theme, or a pair listed in related_themes).
  Elevated categories that don't back each other up leave the overall NORMAL.
- OverallTracker adds hysteresis so the overall level doesn't flicker.

The dashboard scores every signal. Alerts score only the alertable ones
(alerting_views), so backlog that arrives after an outage shows on the dashboard
without setting off a burst of messages.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .config import ScoringSettings
from .models import (
    INTERNET_DOWN,
    OFFLINE_HEADLINE,
    SCORED_CATEGORIES,
    SEVERITY,
    Category,
    Level,
    Overall,
    Signal,
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
    signals: tuple[Signal, ...] = ()  # what the category's raised sources are about


@dataclass(frozen=True)
class OverallResult:
    level: Overall
    reason: str
    drivers: tuple[Signal, ...] = ()  # the signals behind the level


def describe(view: SourceView) -> str:
    return f"{view.name}: {view.reason}"


def _reasons(categories: list[CategoryResult]) -> str:
    """The categories' reasons, each source said once: a story spanning two categories
    shouldn't be described twice."""
    parts: list[str] = []
    for c in categories:
        if c.blamed and c.reason == "; ".join(map(describe, c.blamed)):
            parts += [describe(v) for v in c.blamed]
        else:
            parts.append(c.reason)  # "2 sources elevated together: ..."
    return "; and ".join(dict.fromkeys(parts))


def _worst(levels: list[Level]) -> Level:
    return max(levels, key=SEVERITY.__getitem__)


def score_categories(views: list[SourceView], cfg: ScoringSettings) -> dict[Category, CategoryResult]:
    results: dict[Category, CategoryResult] = {}
    for category in SCORED_CATEGORIES:
        members = [v for v in views if v.category == category]
        live = [v for v in members if is_live(v.level)]
        if not live:
            reason = "No live sources right now" if members else "No sources set up yet"
            results[category] = CategoryResult(category, None, reason, (), len(members))
            continue

        worst = _worst([v.level for v in live])
        raised = [v for v in live if v.level != Level.NORMAL]
        signals = tuple(s for v in raised for s in v.signals)
        needed = cfg.category_critical_when_sources_elevated.get(category.value)
        # Only independent sources count here: a story and a convergence built from the same
        # posts aren't two social sources agreeing.
        independent = [v for v in raised if not v.derived]
        if worst != Level.CRITICAL and needed and len(independent) >= needed:
            reason = f"{len(raised)} sources elevated together: " + "; ".join(map(describe, raised))
            results[category] = CategoryResult(category, Level.CRITICAL, reason, tuple(raised), len(members), signals)
            continue

        blamed = tuple(v for v in live if v.level == worst)
        reason = "All normal" if worst == Level.NORMAL else "; ".join(map(describe, blamed))
        results[category] = CategoryResult(category, worst, reason, blamed, len(members), signals)
    return results


# --- Do two signals plausibly describe the same event? ---


def scopes_related(a: str, b: str, cfg: ScoringSettings) -> bool:
    if a == b:
        return True  # the same cluster, the same square, both Sydney-wide, or the same national theme
    kind_a, _, value_a = a.partition(":")
    kind_b, _, value_b = b.partition(":")
    if kind_a == kind_b == "national":
        if not cfg.national_needs_related_themes:
            return True
        return any({value_a, value_b} == set(pair) for pair in cfg.related_themes)
    if kind_a == kind_b == "area":
        try:
            (row_a, col_a), (row_b, col_b) = (map(int, v.split(",")) for v in (value_a, value_b))
        except ValueError:
            return False
        return abs(row_a - row_b) <= 1 and abs(col_a - col_b) <= 1
    return False


def backing(a: CategoryResult, b: CategoryResult, cfg: ScoringSettings) -> tuple[Signal, Signal] | None:
    """A pair of signals, one from each category, that are about the same thing, if any."""
    for sa in a.signals:
        for sb in b.signals:
            if any(scopes_related(x, y, cfg) for x in sa.scopes for y in sb.scopes):
                return sa, sb
    return None


def _groups(elevated: list[CategoryResult], cfg: ScoringSettings) -> list[list[CategoryResult]]:
    """Elevated categories joined up by backing each other, biggest group first."""
    groups: list[list[CategoryResult]] = []
    for result in elevated:
        joined = [g for g in groups if any(backing(result, other, cfg) for other in g)]
        merged = [result] + [c for g in joined for c in g]
        groups = [g for g in groups if not any(g is j for j in joined)] + [merged]
    order = {r.category: i for i, r in enumerate(elevated)}
    for g in groups:
        g.sort(key=lambda r: order[r.category])
    return sorted(groups, key=lambda g: (-len(g), order[g[0].category]))


def score_overall(
    views: list[SourceView], cfg: ScoringSettings
) -> tuple[OverallResult, dict[Category, CategoryResult]]:
    categories = score_categories(views, cfg)

    offline = [v for v in views if v.category == Category.CONNECTIVITY and INTERNET_DOWN in v.flags]
    if offline:
        reason = offline[0].reason.removeprefix(f"{OFFLINE_HEADLINE}: ")
        reason = f"{reason[:1].upper()}{reason[1:]}. Other tiles show their last-known state"
        return OverallResult(Overall.OFFLINE, reason, tuple(offline[0].signals)), categories

    live = [c for c in categories.values() if c.level is not None]
    if not live:
        return OverallResult(Overall.UNKNOWN, "Waiting for the first results"), categories

    critical = [c for c in live if c.level == Level.CRITICAL]
    elevated = [c for c in live if c.level == Level.ELEVATED]
    if critical:
        reason = _reasons(critical)
        if elevated:
            also = [p for p in _reasons(elevated).split("; and ") if p not in reason]
            if also:
                reason += ". Also elevated: " + "; ".join(also)
        drivers = tuple(s for c in critical for s in c.signals)
        return OverallResult(Overall.CRITICAL, reason, drivers), categories

    groups = _groups(elevated, cfg)
    if groups and len(groups[0]) >= cfg.elevated_when_categories:
        group = groups[0]
        drivers = tuple(s for c in group for s in c.signals)
        return OverallResult(Overall.ELEVATED, _reasons(group), drivers), categories
    if len(elevated) == 1:
        only = elevated[0]
        reason = f"Nothing big. One category is elevated, which isn't enough on its own: {only.reason}"
        return OverallResult(Overall.NORMAL, reason), categories
    if elevated:
        reason = ("Nothing big. Some categories are elevated, but they don't seem to be about the same thing: "
                  + "; ".join(f"{c.category.label}: {c.reason}" for c in elevated))
        return OverallResult(Overall.NORMAL, reason), categories
    return OverallResult(Overall.NORMAL, "Nothing unusual from any source"), categories


def alerting_views(views: list[SourceView]) -> list[SourceView]:
    """The views as alerts see them: raised only by signals that are new enough to alert on."""
    out = []
    for view in views:
        if not is_live(view.level) or view.level is Level.NORMAL or all(s.alertable for s in view.signals):
            out.append(view)
            continue
        signals = tuple(s for s in view.signals if s.alertable)
        if not signals:
            out.append(replace(view, level=Level.NORMAL, signals=(),
                               reason="Raised only by backlog from before sitrep caught up"))
            continue
        level = _worst([s.level for s in signals])
        out.append(replace(view, level=level, signals=signals, reason="; ".join(s.title for s in signals)))
    return out


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
