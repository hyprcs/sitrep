"""Shared vocabulary: levels, categories, and what a source reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Level(StrEnum):
    """A single source's state. Only NORMAL, ELEVATED and CRITICAL count for scoring."""

    NORMAL = "normal"
    ELEVATED = "elevated"
    CRITICAL = "critical"
    LEARNING = "learning"  # still building its idea of "normal"; can't alert yet
    STALE = "stale"  # hasn't updated recently; the dashboard shows its last-known state
    PENDING = "pending"  # just started, no result yet


SEVERITY = {Level.NORMAL: 0, Level.ELEVATED: 1, Level.CRITICAL: 2}


def is_live(level: Level) -> bool:
    """True for levels that count towards the overall status."""
    return level in SEVERITY


class Category(StrEnum):
    CONNECTIVITY = "connectivity"
    NETWORK = "network"
    OFFICIAL = "official"
    SOCIAL = "social"
    INFO = "info"  # shown on the dashboard but never scored

    @property
    def label(self) -> str:
        return _CATEGORY_LABELS[self]


_CATEGORY_LABELS = {
    Category.CONNECTIVITY: "Connectivity",
    Category.NETWORK: "Network measurement",
    Category.OFFICIAL: "Official",
    Category.SOCIAL: "Social",
    Category.INFO: "Info",
}

SCORED_CATEGORIES = (Category.CONNECTIVITY, Category.NETWORK, Category.OFFICIAL, Category.SOCIAL)


class Overall(StrEnum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    ELEVATED = "elevated"
    CRITICAL = "critical"
    OFFLINE = "offline"  # can't reach the internet: shown as its own state


# Flags a source can attach to its report.
INTERNET_DOWN = "internet_down"  # nothing outside the house is answering
ROUTER_DOWN = "router_down"  # the home router isn't answering either

# How an internet-down reason starts. The dashboard headline already says it,
# so the overall reason drops this prefix.
OFFLINE_HEADLINE = "Can't reach the internet"


@dataclass(frozen=True)
class Assessment:
    """What a source reports: a level, a one-line reason, and optional extras."""

    level: Level
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    flags: frozenset[str] = frozenset()
