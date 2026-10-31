"""Shared vocabulary: levels, categories, scopes, and what a source reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Level(StrEnum):
    """A single source's state. Only NORMAL, ELEVATED and CRITICAL count for scoring.

    Every source is always in one of four states: ok (normal, elevated or critical),
    learning, fetch failing or frozen.
    """

    NORMAL = "normal"
    ELEVATED = "elevated"
    CRITICAL = "critical"
    LEARNING = "learning"  # still building its idea of "normal", or waiting for its first update
    FAILING = "failing"  # "fetch failing": its last tries failed, or it hasn't answered for too long
    FROZEN = "frozen"  # it answers, but the newest data inside the answer is too old
    PENDING = "pending"  # just registered, nothing heard yet (shown as learning)


SEVERITY = {Level.NORMAL: 0, Level.ELEVATED: 1, Level.CRITICAL: 2}


def is_live(level: Level) -> bool:
    """True for levels that count towards the overall status."""
    return level in SEVERITY


def health(level: Level) -> str:
    """The source's state in four words: ok, learning, failing or frozen."""
    if is_live(level):
        return "ok"
    return "learning" if level in (Level.LEARNING, Level.PENDING) else level.value


class Category(StrEnum):
    CONNECTIVITY = "connectivity"
    NETWORK = "network"
    OFFICIAL = "official"
    CYBER = "cyber"
    NEWS = "news"
    SOCIAL = "social"
    STRESS = "stress"
    SENSORS = "sensors"
    INFO = "info"  # shown on the dashboard but never scored

    @property
    def label(self) -> str:
        return _CATEGORY_LABELS[self]


_CATEGORY_LABELS = {
    Category.CONNECTIVITY: "Connectivity",
    Category.NETWORK: "Network measurement",
    Category.OFFICIAL: "Official",
    Category.CYBER: "Cyber intel",
    Category.NEWS: "News",
    Category.SOCIAL: "Social",
    Category.STRESS: "Stress",
    Category.SENSORS: "Sensors",
    Category.INFO: "Info",
}

SCORED_CATEGORIES = (
    Category.CONNECTIVITY, Category.NETWORK, Category.OFFICIAL, Category.CYBER,
    Category.NEWS, Category.SOCIAL, Category.STRESS, Category.SENSORS,
)


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


# --- Scopes: what a raised signal is about ---
#
# Two categories only back each other up when their signals plausibly describe the
# same event, so every raised signal names its scope:
#
#   national:<theme>   Australia-wide, about the internet, cyber, power, fuel or security
#   sydney             about Sydney or NSW as a whole, with no particular place
#   area:<row>,<col>   a 2 km square of the map grid
#   cluster:<id>       a story cluster (news, official and social items about one story)

SYDNEY = "sydney"
THEMES = ("internet", "cyber", "power", "fuel", "security")


def national(theme: str) -> str:
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r}; use one of {', '.join(THEMES)}")
    return f"national:{theme}"


def area(row: int, col: int) -> str:
    return f"area:{row},{col}"


def cluster(cluster_id: str) -> str:
    return f"cluster:{cluster_id}"


def valid_scope(scope: str) -> bool:
    kind, _, value = scope.partition(":")
    if scope == SYDNEY:
        return True
    if kind == "national":
        return value in THEMES
    if kind == "area":
        row, _, col = value.partition(",")
        return row.lstrip("-").isdigit() and col.lstrip("-").isdigit()
    return kind == "cluster" and bool(value)


@dataclass(frozen=True)
class Signal:
    """One thing a source is raised about: an RFS warning, a spike, a carrier dropping.

    key is stable for as long as the event lasts (for example "acsc:<guid>"), so the
    same event can't alert twice. alertable is False for backlog: items that were
    already old when sitrep first saw them. They show on the dashboard, but can't
    raise an alert.
    """

    key: str
    level: Level
    scopes: tuple[str, ...]
    title: str = ""
    alertable: bool = True
    at: float | None = None  # the newest own timestamp behind it, if it has one
    emergency: bool = False  # an official emergency-level item (RFS Emergency Warning, LOR3, ...)
    keywords: tuple[str, ...] = ()  # keyword groups it matched

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "level": self.level.value, "scopes": list(self.scopes), "title": self.title,
            "alertable": self.alertable, "at": self.at, "emergency": self.emergency,
            "keywords": list(self.keywords),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Signal:
        return cls(
            key=str(raw["key"]), level=Level(raw["level"]), scopes=tuple(raw.get("scopes") or ()),
            title=str(raw.get("title") or ""), alertable=bool(raw.get("alertable", True)),
            at=raw.get("at"), emergency=bool(raw.get("emergency", False)),
            keywords=tuple(raw.get("keywords") or ()),
        )


@dataclass(frozen=True)
class Assessment:
    """What a source reports: a level, a one-line reason, and optional extras.

    content_at is the newest timestamp inside the data the source received (not when
    it was fetched); None means the data had no usable dates. feed_at is a date for the
    feed as a whole (lastBuildDate, Last-Modified), for quiet feeds with few items.
    signals say what a raised level is about; a raised report without any gets one
    standing for the whole source.
    """

    level: Level
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    flags: frozenset[str] = frozenset()
    content_at: float | None = None
    feed_at: float | None = None
    signals: tuple[Signal, ...] = ()
