"""Live Traffic NSW: road hazards across NSW, from Transport for NSW's Open Data Hub.

Every 2 minutes sitrep reads the open incidents, fires and floods affecting roads, and
every 15 minutes the major events (concerts, marathons). It needs a free token in .env
(TFNSW_API_KEY; see "Transport for NSW token" in the README). That's about 2,300 of the
60,000 requests a day the free plan allows. The data is © Transport for NSW, under
CC BY 4.0.

Live Traffic never raises sitrep's level on its own: crashes and breakdowns happen many
times a day. Its hazards go on the map and the timeline, and they're there for the
signals that look across sources (story clusters and convergence, which come later).
Only hazards Transport for NSW marks "major" count as an official source in a story
cluster; routine ones still show on the map.

"Open" includes planned works that can run for years (the Harbour Bridge maintenance,
say), so the map and the list show unplanned hazards, major ones, and major events
happening now, and leave long-term roadworks out.

Each hazard has its own created and last-updated times (in milliseconds). An update
after an outage counts as new; an old one can't set off anything. If no hazard anywhere
in NSW has been updated for 3 hours, the feed has probably stopped moving on, and the
source shows as frozen.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import Field, SecretStr

from ..geo import cell_of, km_between
from ..models import Assessment, Category, Level
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, retry_after_seconds

TOKEN_HELP = 'See "Transport for NSW token" in the README.'


class LiveTrafficSettings(PollingSettings):
    interval_seconds: float = 120
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://api.transport.nsw.gov.au/v1/live/hazards"
    # Read every interval...
    types: list[str] = Field(default_factory=lambda: ["incident", "fire", "flood"])
    # ...and these less often.
    slow_types: list[str] = Field(default_factory=lambda: ["majorevent"])
    slow_every_minutes: float = 15
    # Hazards this close to home are listed on the dashboard.
    show_km: float = Field(25, gt=0)
    show_hazards: int = Field(10, ge=1)


@dataclass(frozen=True)
class Hazard:
    id: str
    kind: str  # the feed it came from: incident, fire, flood or majorevent
    category: str  # Transport for NSW's main category, such as "CRASH" or "FLOODING"
    title: str
    lat: float | None
    lon: float | None
    created: float | None
    updated: float | None
    major: bool
    planned: bool
    start: float | None = None
    end: float | None = None
    road: str = ""
    suburb: str = ""
    advice: str = ""

    def happening(self, now: float) -> bool:
        """True for a planned hazard (an event, say) that is on now."""
        return (self.start is None or self.start <= now + 3600) and (self.end is None or now <= self.end)


def _ms(value: Any) -> float | None:
    return value / 1000 if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _text(value: Any) -> str:
    """Plain text from a field that may hold HTML or the string "null"."""
    if not isinstance(value, str) or value.strip().lower() in ("", "null"):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value))).strip()


def _point(geometry: Any) -> tuple[float, float] | None:
    """(lat, lon). The developer guide says [lat, lon] but the feed sends [lon, lat]; NSW
    latitudes are always negative and its longitudes positive, so the order is clear."""
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2 or not all(isinstance(c, (int, float)) for c in coords[:2]):
        return None
    a, b = float(coords[0]), float(coords[1])
    if a < 0 < b:
        a, b = b, a
    lon, lat = a, b
    return (lat, lon) if -90 <= lat <= 0 and 0 <= lon <= 180 else None


def describe(category: str, road: str, suburb: str, fallback: str) -> str:
    what = category.replace("  ", " ").strip().capitalize() or fallback
    where = ", ".join(x for x in (road, suburb) if x)
    return f"{what}: {where}" if where else what


def parse(data: Any, kind: str) -> list[Hazard]:
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise SourceError(f"Live Traffic's {kind} list isn't the GeoJSON sitrep expects")
    hazards = []
    for feature in data["features"]:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict) or props.get("ended") is True:
            continue
        roads = [r for r in props.get("roads") or [] if isinstance(r, dict)]
        first = roads[0] if roads else {}
        road, suburb = _text(first.get("mainStreet")), _text(first.get("suburb"))
        category = _text(props.get("mainCategory")) or kind.upper()
        point = _point(feature.get("geometry"))
        advice = "; ".join(a for a in (_text(props.get("adviceA")), _text(props.get("adviceB"))) if a)
        hazards.append(Hazard(
            id=f"{kind}:{feature.get('id') or props.get('created')}",
            kind=kind, category=category,
            title=describe(category, road, suburb, _text(props.get("displayName"))),
            lat=point[0] if point else None, lon=point[1] if point else None,
            created=_ms(props.get("created")), updated=_ms(props.get("lastUpdated")) or _ms(props.get("created")),
            major=props.get("isMajor") is True, planned=str(props.get("incidentKind", "")).lower() == "planned",
            start=_ms(props.get("start")), end=_ms(props.get("end")),
            road=road, suburb=suburb, advice=advice,
        ))
    return hazards


@dataclass
class Placed:
    hazard: Hazard
    km: float | None
    in_area: bool  # inside the map's area (Greater Sydney)

    @property
    def mark(self) -> str:
        return "watch" if self.hazard.major else "info"


def worth_showing(p: Placed, now: float) -> bool:
    """Unplanned hazards, major ones, and major events on now. Not long-term roadworks."""
    h = p.hazard
    if h.kind == "majorevent":
        return h.happening(now)
    return not h.planned or h.major


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def assess(hazards: list[Hazard], home: tuple[float, float], bbox: list[float], s: LiveTrafficSettings,
           now: float) -> Assessment:
    west, south, east, north = bbox
    placed = []
    for h in hazards:
        km = km_between(home[0], home[1], h.lat, h.lon) if h.lat is not None and h.lon is not None else None
        inside = h.lat is not None and h.lon is not None and west <= h.lon <= east and south <= h.lat <= north
        placed.append(Placed(h, km, inside))
    shown = [p for p in placed if worth_showing(p, now)]
    sydney = [p for p in shown if p.in_area and p.hazard.kind != "majorevent"]
    events = [p for p in shown if p.in_area and p.hazard.kind == "majorevent"]
    major = sorted((p for p in sydney if p.hazard.major), key=lambda p: p.km if p.km is not None else 1e9)

    reason = (f"{_plural(len(sydney), 'hazard')} on Greater Sydney's roads ({len(major)} major)"
              if sydney else "No unplanned or major hazards on Greater Sydney's roads")
    near = sorted((p for p in sydney if p.km is not None), key=lambda p: p.km)
    if near:
        nearest = near[0]
        reason += f". Nearest: {nearest.hazard.title}, {nearest.km:.0f} km"
    if major:
        reason += ". Major: " + "; ".join(p.hazard.title for p in major[:3])
    if events:
        reason += f". {_plural(len(events), 'major event')} on now"
    statewide = sum(1 for p in shown if p.hazard.kind != "majorevent")
    reason += f". Across NSW: {_plural(statewide, 'hazard')}"

    listed = [p for p in near if p.km <= s.show_km]
    details = {
        "show_km": s.show_km,
        "near": [{**asdict(p.hazard), "km": round(p.km, 1), "mark": p.mark} for p in listed[: s.show_hazards]],
        "near_total": len(listed),
        "sydney": {"hazards": len(sydney), "major": len(major), "events": len(events)},
        "statewide": statewide,
        "map": [
            {"kind": "traffic", "id": p.hazard.id, "title": p.hazard.title,
             "label": "Major event" if p.hazard.kind == "majorevent" else ("Major hazard" if p.hazard.major
                                                                          else p.hazard.category.capitalize()),
             "mark": p.mark, "lat": p.hazard.lat, "lon": p.hazard.lon, "km": round(p.km, 1),
             "at": p.hazard.updated, "detail": p.hazard.advice}
            for p in shown if p.in_area and p.km is not None
        ],
    }
    times = [h.updated for h in hazards if h.kind != "majorevent" and h.updated is not None and h.updated <= now + 600]
    return Assessment(Level.NORMAL, reason, details, content_at=max(times, default=None))


class LiveTrafficAdapter(PollingAdapter):
    id = "livetraffic"
    name = "Live Traffic NSW"
    category = Category.OFFICIAL
    link = "https://www.livetraffic.com/"
    Settings = LiveTrafficSettings
    # Some hazard somewhere in NSW is updated every few minutes; 3 quiet hours means the feed is stuck.
    content_budget_minutes = 180
    publish_delay_minutes = 5
    settings: LiveTrafficSettings

    def __init__(self, ctx: AdapterContext, settings: LiveTrafficSettings) -> None:
        super().__init__(ctx, settings)
        self._cache: dict[str, tuple[str | None, str | None, list[Hazard]]] = {}  # kind -> (etag, modified, hazards)
        self._slow_at = float("-inf")
        self._published: float | None = None

    async def fetch(self, kind: str) -> list[Hazard]:
        token = self.settings.api_key.get_secret_value().strip()
        if not token:
            raise SourceError(f"No Transport for NSW token yet: add TFNSW_API_KEY to .env. {TOKEN_HELP}")
        headers = {"Authorization": f"apikey {token}"}
        etag, modified, cached = self._cache.get(kind, (None, None, []))
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        response = await self.ctx.http.get(f"{self.settings.base_url.rstrip('/')}/{kind}/open", headers=headers)
        if response.status_code == 304 and kind in self._cache:
            return cached
        if response.status_code == 401:
            raise SourceError(f"Transport for NSW didn't accept the token. Check TFNSW_API_KEY in .env. {TOKEN_HELP}")
        if response.status_code == 403:
            detail = response.headers.get("X-Error-Detail") or "no reason given"
            raise SourceError(f"Transport for NSW refused the request ({detail[:120]}); the daily limit may be used up",
                              retry_after_seconds(response))
        if response.status_code == 429:
            raise SourceError("Transport for NSW asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError("Live Traffic sent something that isn't JSON") from exc
        hazards = parse(data, kind)
        published = _ms(data.get("lastPublished"))
        if published:
            self._published = max(self._published or 0, published)
        self._cache[kind] = (response.headers.get("ETag"), response.headers.get("Last-Modified"), hazards)
        return hazards

    async def poll(self) -> Assessment:
        s = self.settings
        now = self.ctx.clock()
        kinds = list(s.types)
        if now - self._slow_at >= s.slow_every_minutes * 60 - 5:
            kinds += s.slow_types
            self._slow_at = now
        answers = await asyncio.gather(*(self.fetch(kind) for kind in kinds))
        fresh = {kind: hazards for kind, hazards in zip(kinds, answers)}
        hazards = [h for kind in [*s.types, *s.slow_types]
                   for h in fresh.get(kind, self._cache.get(kind, (None, None, []))[2])]
        loc = self.ctx.config.home.location
        result = assess(hazards, (loc.lat, loc.lon), self.ctx.config.map.bbox, s, now)
        self._remember(hazards, (loc.lat, loc.lon), now)
        return Assessment(result.level, result.reason, result.details, content_at=result.content_at,
                          feed_at=self._published)

    def _remember(self, hazards: list[Hazard], home: tuple[float, float], now: float) -> None:
        """Keep every hazard as an item (for story clusters and convergence later), and put
        major ones in Greater Sydney on the timeline."""
        west, south, east, north = self.ctx.config.map.bbox
        for h in hazards:
            if h.lat is None or h.lon is None:
                continue
            km = km_between(home[0], home[1], h.lat, h.lon)
            self.see_item(
                h.id, h.updated, title=f"Live Traffic: {h.title}", level="watch" if h.major else "info", now=now,
                details={"lat": h.lat, "lon": h.lon, "km": round(km, 1), "cell": list(cell_of(h.lat, h.lon)),
                         "category": h.category, "suburb": h.suburb, "road": h.road, "kind": h.kind,
                         "major": h.major, "planned": h.planned},
            )
            if h.major and west <= h.lon <= east and south <= h.lat <= north:
                self.ctx.db.add_event(
                    h.created or h.updated or now, self.id, self.category.value, "info", f"Live Traffic major hazard: {h.title}",
                    url=self.link, details={"reason": f"{km:.0f} km away" + (f"; {h.advice}" if h.advice else "")},
                    dedupe_key=f"livetraffic:{h.id}",
                )
