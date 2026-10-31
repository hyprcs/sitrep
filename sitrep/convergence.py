"""Geographic convergence: several kinds of event in one small area at the same time.

Greater Sydney is divided into squares about 2 km across (see geo.py). Every scoring pass,
sitrep places the last 2 hours of events in their squares. The kinds of event are:

- fire: an RFS incident (planned burns aren't counted);
- road: a Live Traffic hazard (unplanned or major; not long-term roadworks or events);
- trains: an unplanned train disruption, at the stations its headline names (stations are
  almost always named after their suburb);
- news: a headline naming a suburb there;
- social: posts from at least 2 different people naming a suburb there;
- aircraft: an aircraft circling there (added with the aircraft source).

When 3 or more different kinds land in one square, or in the squares around it, that's a
convergence, named after the place most of its events mention. Neighbouring busy squares
join into one convergence rather than several.

A convergence is elevated, in each category its events come from, with its square as its
scope, so it backs up anything else about the same area. It only becomes critical inside
Greater Sydney and when it includes an emergency-level item (an RFS Emergency Warning) or
its headlines and posts match one of the keyword groups. A crash with a news story and a
helicopter overhead is elevated, not critical. Unlike story clusters, a convergence
doesn't need the events to share any words: a grass fire, a closed road and people
posting about smoke in one suburb are enough.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import ConvergenceSettings
from .db import Database
from .geo import cell_of
from .models import Assessment, Category, Level, Signal, area
from .places import gazetteer
from .state import SourceView

KIND_LABELS = {"fire": "fire", "road": "road hazard", "trains": "train disruption", "news": "news",
               "social": "posts", "aircraft": "aircraft circling"}
KIND_CATEGORY = {"fire": Category.OFFICIAL, "road": Category.OFFICIAL, "trains": Category.OFFICIAL,
                 "news": Category.NEWS, "social": Category.SOCIAL, "aircraft": Category.SENSORS}
# Roughly Greater Sydney (the map's area). Only convergences inside it can become critical.
SYDNEY_BOX = (-34.40, -32.95, 149.95, 151.70)


@dataclass
class Event:
    kind: str
    cell: tuple[int, int]
    lat: float
    lon: float
    at: float
    title: str
    place: str | None = None
    alertable: bool = True
    emergency: bool = False
    who: str = ""  # for posts: a one-way code for the author


@dataclass
class Hotspot:
    cells: set[tuple[int, int]]
    events: list[Event] = field(default_factory=list)
    keywords: tuple[str, ...] = ()

    @property
    def centre(self) -> tuple[int, int]:
        counts = Counter(e.cell for e in self.events)
        return max(sorted(counts), key=lambda c: counts[c])

    @property
    def key(self) -> str:
        return f"convergence:{self.centre[0]},{self.centre[1]}"

    def kinds(self, s: ConvergenceSettings, alertable_only: bool = False) -> set[str]:
        events = [e for e in self.events if e.alertable or not alertable_only]
        found = {e.kind for e in events if e.kind != "social"}
        if len({e.who for e in events if e.kind == "social"}) >= s.social_min_people:
            found.add("social")
        return found

    @property
    def where(self) -> str:
        named = Counter(e.place for e in self.events if e.place)
        if named:
            name = named.most_common(1)[0][0]
            return "Sydney CBD" if name == "Sydney" else name
        lat = sum(e.lat for e in self.events) / len(self.events)
        lon = sum(e.lon for e in self.events) / len(self.events)
        return nearest_place(lat, lon)

    @property
    def point(self) -> tuple[float, float]:
        centre = [e for e in self.events if e.cell == self.centre]
        return sum(e.lat for e in centre) / len(centre), sum(e.lon for e in centre) / len(centre)

    def inside_sydney(self) -> bool:
        lat, lon = self.point
        south, north, west, east = SYDNEY_BOX
        return south <= lat <= north and west <= lon <= east

    def level(self, s: ConvergenceSettings, alertable_only: bool = False) -> Level:
        if len(self.kinds(s, alertable_only)) < s.min_kinds:
            return Level.NORMAL
        events = [e for e in self.events if e.alertable or not alertable_only]
        if self.inside_sydney() and (any(e.emergency for e in events) or self.keywords):
            return Level.CRITICAL
        return Level.ELEVATED

    def describe(self, s: ConvergenceSettings) -> str:
        kinds = ", ".join(KIND_LABELS[k] for k in sorted(self.kinds(s), key=list(KIND_LABELS).index))
        return f"Convergence at {self.where}: {kinds}"


def nearest_place(lat: float, lon: float) -> str:
    return gazetteer().nearest(lat, lon).name


def neighbours(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return abs(a[0] - b[0]) <= 1 and abs(a[1] - b[1]) <= 1


def find_hotspots(events: list[Event], s: ConvergenceSettings) -> list[Hotspot]:
    """Squares whose neighbourhood has enough kinds of event, joined up where they touch."""
    cells = {e.cell for e in events}
    busy = []
    for cell in cells:
        nearby = Hotspot({cell}, [e for e in events if neighbours(e.cell, cell)])
        if len(nearby.kinds(s)) >= s.min_kinds:
            busy.append(cell)
    hotspots: list[Hotspot] = []
    for cell in sorted(busy):
        joined = [h for h in hotspots if any(neighbours(cell, c) for c in h.cells)]
        merged = Hotspot({cell}.union(*(h.cells for h in joined)))
        hotspots = [h for h in hotspots if not any(h is j for j in joined)] + [merged]
    for h in hotspots:
        h.events = sorted((e for e in events if any(neighbours(e.cell, c) for c in h.cells)), key=lambda e: e.at)
    return hotspots


def _events_from_items(rows: list[dict[str, Any]], s: ConvergenceSettings) -> list[Event]:
    places = gazetteer()
    events = []
    for row in rows:
        d, source = row["details"], row["source_id"]
        at = row["ts"] or row["version_seen"]
        common = {"at": at, "title": row["title"], "alertable": bool(row["alertable"])}
        if source == "rfs" and d.get("lat") is not None and d.get("level") != "Planned burn":
            events.append(Event("fire", cell_of(d["lat"], d["lon"]), d["lat"], d["lon"],
                                emergency=d.get("level") == "Emergency Warning", **common))
        elif source == "livetraffic" and d.get("lat") is not None and d.get("kind") != "majorevent" and (
                not d.get("planned") or d.get("major")):
            events.append(Event("road", cell_of(d["lat"], d["lon"]), d["lat"], d["lon"], place=d.get("suburb"),
                                **common))
        elif source == "transit":
            for p in places.find(row["title"], nsw_feed=True):
                if p.sydney:
                    events.append(Event("trains", cell_of(p.lat, p.lon), p.lat, p.lon, place=p.name, **common))
        elif source == "news" and d.get("lat") is not None:
            place = next((p for p in d.get("places") or [] if (g := places.get(p)) and g.sydney), None)
            events.append(Event("news", cell_of(d["lat"], d["lon"]), d["lat"], d["lon"], place=place, **common))
        elif source == "adsb" and d.get("lat") is not None and d.get("orbit"):
            events.append(Event("aircraft", cell_of(d["lat"], d["lon"]), d["lat"], d["lon"], **common))
        elif source == "bluesky" and d.get("lat") is not None:  # simulated posts arrive as items
            events.append(Event("social", cell_of(d["lat"], d["lon"]), d["lat"], d["lon"], who=row["item_id"],
                                **common))
    return events


class Convergence:
    """Works out the current convergences on each scoring pass."""

    def __init__(self, db: Database, settings: ConvergenceSettings, clock: Callable[[], float] = time.time, *,
                 keyword_groups: Callable[[str], list[str]] | None = None,
                 posts: Callable[[float], list[tuple[str, Any]]] | None = None) -> None:
        self.db = db
        self.s = settings
        self.clock = clock
        self.keyword_groups = keyword_groups or (lambda text: [])
        self.posts = posts or (lambda since: [])  # (author code, post) for recent Bluesky posts
        self.hotspots: list[Hotspot] = []
        self._announced: set[str] = set()

    def update(self, now: float | None = None) -> list[Hotspot]:
        now = self.clock() if now is None else now
        since = now - self.s.window_hours * 3600
        rows = self.db.query(
            """SELECT source_id, item_id, ts, version_seen, alertable, title, details FROM items
               WHERE source_id IN ('rfs', 'livetraffic', 'transit', 'news', 'adsb', 'bluesky')
                 AND COALESCE(ts, version_seen) >= ?""",
            (since,),
        )
        events = _events_from_items([{**dict(r), "details": json.loads(r["details"])} for r in rows], self.s)
        for who, post in self.posts(since):
            if post.cell is not None:
                events.append(Event("social", post.cell, post.lat, post.lon, post.at, post.title, place=post.place,
                                    alertable=post.alertable, who=who))
        hotspots = find_hotspots(events, self.s)
        for h in hotspots:
            texts = [e.title for e in h.events if e.kind in ("news", "social")]
            h.keywords = tuple(self.keyword_groups("\n".join(texts))) if texts else ()
        self.hotspots = sorted(hotspots, key=lambda h: (-len(h.kinds(self.s)), -max(e.at for e in h.events)))
        self._announce(now)
        return self.hotspots

    def views(self, now: float, alertable_only: bool = False) -> list[SourceView]:
        """A stand-in view for each category a convergence's events come from, for scoring."""
        by_category: dict[Category, list[tuple[Hotspot, Level]]] = {}
        for h in self.hotspots:
            level = h.level(self.s, alertable_only)
            if level is Level.NORMAL:
                continue
            for category in {KIND_CATEGORY[k] for k in h.kinds(self.s, alertable_only)}:
                by_category.setdefault(category, []).append((h, level))
        views = []
        for category, found in by_category.items():
            worst = Level.CRITICAL if any(lv is Level.CRITICAL for _, lv in found) else Level.ELEVATED
            signals = tuple(
                Signal(h.key, lv, (area(*h.centre),), title=h.describe(self.s),
                       alertable=alertable_only or h.level(self.s, True) is not Level.NORMAL,
                       emergency=any(e.emergency for e in h.events), keywords=h.keywords)
                for h, lv in found
            )
            reason = "; ".join(h.describe(self.s) for h, _ in found)
            views.append(SourceView(
                id=f"convergence:{category.value}", name="Convergence", category=category, link=None, level=worst,
                reason=reason, flags=frozenset(), details={}, last_known_level=worst, last_known_reason=reason,
                observed_at=now, last_success_at=now, last_attempt_at=now, last_error=None, failures=0,
                signals=signals, derived=True,
            ))
        return views

    def assessment(self, now: float) -> Assessment:
        """What the "Convergence" tile shows. Information only: scoring uses views()."""
        live = [h for h in self.hotspots if h.level(self.s) is not Level.NORMAL]
        if live:
            level = Level.CRITICAL if any(h.level(self.s) is Level.CRITICAL for h in live) else Level.ELEVATED
            reason = "; ".join(h.describe(self.s) for h in live[:3])
        else:
            level = Level.NORMAL
            reason = (f"No area has {self.s.min_kinds} or more kinds of event in the last "
                      f"{self.s.window_hours:g} hours")
        details = {
            "window_hours": self.s.window_hours,
            "hotspots": [
                {"key": h.key, "where": h.where, "level": h.level(self.s).value,
                 "kinds": sorted(h.kinds(self.s), key=list(KIND_LABELS).index), "keywords": list(h.keywords),
                 "events": [{"kind": e.kind, "title": e.title if e.kind != "social" else "A Bluesky post",
                             "at": e.at, "place": e.place} for e in h.events[:12]],
                 "size": len(h.events)}
                for h in live
            ],
            "map": [
                {"kind": "convergence", "id": h.key, "title": h.describe(self.s), "label": "Convergence",
                 "mark": h.level(self.s).value, "lat": h.point[0], "lon": h.point[1], "km": None,
                 "at": max(e.at for e in h.events), "detail": f"{len(h.events)} events in the last "
                 f"{self.s.window_hours:g} hours"}
                for h in live
            ],
        }
        return Assessment(level, reason, details)

    def _announce(self, now: float) -> None:
        for h in self.hotspots:
            if h.level(self.s) is Level.NORMAL:
                continue
            stage = f"{h.key}:{','.join(sorted(h.kinds(self.s)))}"
            if stage in self._announced:
                continue
            self._announced.add(stage)
            self.db.add_event(now, "convergence", "info", h.level(self.s).value, h.describe(self.s),
                              details={"reason": f"{len(h.events)} events in the last {self.s.window_hours:g} hours"},
                              dedupe_key=f"{stage}:{int(now // 3600)}")
