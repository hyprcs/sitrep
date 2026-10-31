"""NSW Rural Fire Service: current incidents across NSW, and how close they are to home.

Every 5 minutes sitrep reads the RFS's public GeoJSON feed of current incidents (the
same list as Fires Near Me). It asks "has it changed?" first (ETag), so most checks
cost a few hundred bytes. No key is needed. The data is © State of New South Wales
(NSW Rural Fire Service), under CC BY 4.0.

Each incident has an alert level from the Australian Warning System, set by the RFS:
Advice, Watch and Act, or Emergency Warning ("Not Applicable" and "Planned Burn" for
the rest). How it's judged, by distance from home (config.yaml, home.location):

- critical: an Emergency Warning within 15 km;
- elevated: an Emergency Warning within 50 km, or a Watch and Act within 15 km;
- everything else is shown (and placed on the map) but doesn't count.

An Emergency Warning is an "emergency-level" item wherever it is: later, a story
cluster or a convergence alert that includes one can become critical.

The feed's times (pubDate) are UTC with no zone marked, like "30/10/2026 5:47:00 AM";
each incident's description repeats the time in Sydney time, which is how that was
checked. An incident updated after an outage counts as new, so a warning still in force
can alert; its older copy can't. If no incident anywhere in NSW has been updated for
6 hours, the feed has probably stopped moving on, and the source shows as frozen.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from pydantic import Field

from ..geo import cell_of, first_point, km_between
from ..models import Assessment, Category, Level, Signal, area
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, retry_after_seconds

EMERGENCY = "Emergency Warning"
WATCH = "Watch and Act"
ADVICE = "Advice"
# How the feed spells the levels, in lower case, mapped to how sitrep shows them.
LEVELS = {"emergency warning": EMERGENCY, "watch and act": WATCH, "advice": ADVICE,
          "not applicable": "Not applicable", "planned burn": "Planned burn"}
# Worst first, for sorting.
ORDER = {EMERGENCY: 0, WATCH: 1, ADVICE: 2}


class RfsSettings(PollingSettings):
    interval_seconds: float = 300
    url: str = "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json"
    # An Emergency Warning this close to home is critical...
    critical_km: float = Field(15, gt=0)
    # ...and this close, elevated.
    emergency_elevated_km: float = Field(50, gt=0)
    # A Watch and Act this close is elevated.
    watch_and_act_km: float = Field(15, gt=0)
    # Incidents this close are listed on the dashboard.
    show_km: float = Field(50, gt=0)
    show_incidents: int = Field(10, ge=1)


@dataclass(frozen=True)
class Incident:
    id: str
    title: str
    level: str  # Emergency Warning, Watch and Act, Advice, Not applicable, Planned burn
    updated: float | None  # the feed's pubDate, as a timestamp
    lat: float | None
    lon: float | None
    council: str = ""
    status: str = ""
    kind: str = ""  # the incident type, such as "Bush Fire" or "Hazard Reduction"
    size: str = ""
    agency: str = ""
    url: str | None = None


def feed_time(text: Any) -> float | None:
    """'30/10/2026 5:47:00 AM' (UTC, though the feed doesn't say so) as a timestamp."""
    try:
        return datetime.strptime(str(text).strip(), "%d/%m/%Y %I:%M:%S %p").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def description_fields(text: str) -> dict[str, str]:
    """'ALERT LEVEL: Advice <br />LOCATION: ...' as {'ALERT LEVEL': 'Advice', 'LOCATION': ...}."""
    fields = {}
    for part in re.split(r"<br\s*/?>", html.unescape(text or ""), flags=re.IGNORECASE):
        key, sep, value = part.partition(":")
        if sep and key.strip().isupper():
            fields[key.strip()] = re.sub(r"<[^>]+>", "", value).strip()
    return fields


def incident_id(guid: str, title: str) -> str:
    match = re.search(r"/incidents/(\d+)", guid or "")
    return match.group(1) if match else (guid or title)


def parse_feed(data: Any) -> list[Incident]:
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise SourceError("The RFS feed isn't the GeoJSON sitrep expects")
    incidents = []
    for feature in data["features"]:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict) or not props.get("title"):
            continue
        fields = description_fields(str(props.get("description") or ""))
        raw_level = str(props.get("category") or fields.get("ALERT LEVEL") or "").strip()
        point = first_point(feature.get("geometry"))
        guid = str(props.get("guid") or "")
        incidents.append(Incident(
            id=incident_id(guid, str(props["title"])),
            title=re.sub(r"\s+", " ", str(props["title"])).strip(),
            level=LEVELS.get(raw_level.lower(), raw_level or "Unknown"),
            updated=feed_time(props.get("pubDate")),
            lat=point[0] if point else None,
            lon=point[1] if point else None,
            council=fields.get("COUNCIL AREA", ""),
            status=fields.get("STATUS", ""),
            kind=fields.get("TYPE", ""),
            size=fields.get("SIZE", ""),
            agency=fields.get("RESPONSIBLE AGENCY", ""),
            url=guid if guid.startswith("http") else None,
        ))
    return incidents


@dataclass
class Placed:
    incident: Incident
    km: float | None  # from home
    mark: str  # how much it matters here: critical, elevated, watch or info

    @property
    def emergency(self) -> bool:
        return self.incident.level == EMERGENCY


def place(incident: Incident, home: tuple[float, float], s: RfsSettings) -> Placed:
    km = None
    if incident.lat is not None and incident.lon is not None:
        km = km_between(home[0], home[1], incident.lat, incident.lon)
    mark = "info"
    if incident.level in (EMERGENCY, WATCH):
        mark = "watch"
    if km is not None:
        if incident.level == EMERGENCY and km <= s.critical_km:
            mark = "critical"
        elif (incident.level == EMERGENCY and km <= s.emergency_elevated_km) or (
            incident.level == WATCH and km <= s.watch_and_act_km
        ):
            mark = "elevated"
    return Placed(incident, km, mark)


def _where(p: Placed) -> str:
    distance = f"{p.km:.0f} km away" if p.km is not None else "location not given"
    council = f", {p.incident.council}" if p.incident.council else ""
    return f"{p.incident.title} ({distance}{council})"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def assess(incidents: list[Incident], home: tuple[float, float], home_name: str, s: RfsSettings, now: float,
           alertable: dict[str, bool] | None = None) -> Assessment:
    placed = [place(i, home, s) for i in incidents]
    raised = sorted((p for p in placed if p.mark in ("critical", "elevated")),
                    key=lambda p: (p.mark != "critical", ORDER.get(p.incident.level, 9), p.km or 0))
    statewide = {level: sum(1 for i in incidents if i.level == level) for level in (EMERGENCY, WATCH, ADVICE)}

    if raised:
        level = Level.CRITICAL if raised[0].mark == "critical" else Level.ELEVATED
        reason = "; ".join(f"{p.incident.level}: {_where(p)}" for p in raised)
    else:
        level = Level.NORMAL
        near = sorted((p for p in placed if p.km is not None), key=lambda p: p.km)
        reason = (f"No Emergency Warning within {s.emergency_elevated_km:.0f} km of {home_name}, and no Watch "
                  f"and Act within {s.watch_and_act_km:.0f} km")
        if near:
            reason += f". Nearest incident: {near[0].incident.title}, {near[0].km:.0f} km ({near[0].incident.level})"
    counts = [_plural(statewide[EMERGENCY], "Emergency Warning"), f"{statewide[WATCH]} at Watch and Act",
              f"{statewide[ADVICE]} at Advice"]
    reason += f". Across NSW: {_plural(len(incidents), 'incident')} ({', '.join(counts)})"

    signals = tuple(
        Signal(
            f"rfs:{p.incident.id}", Level.CRITICAL if p.mark == "critical" else Level.ELEVATED,
            (area(*cell_of(p.incident.lat, p.incident.lon)),) if p.incident.lat is not None else (),
            title=f"RFS {p.incident.level}: {_where(p)}",
            alertable=(alertable or {}).get(p.incident.id, True), at=p.incident.updated, emergency=p.emergency,
        )
        for p in raised
    )
    near = sorted((p for p in placed if p.km is not None and p.km <= s.show_km),
                  key=lambda p: (ORDER.get(p.incident.level, 9), p.km))
    details = {
        "home": {"name": home_name, "lat": home[0], "lon": home[1]},
        "show_km": s.show_km,
        "near": [{**asdict(p.incident), "km": round(p.km, 1), "mark": p.mark} for p in near[: s.show_incidents]],
        "near_total": len(near),
        "statewide": {"total": len(incidents), **statewide},
        # Every incident with a location, for the map page.
        "map": [
            {"kind": "rfs", "id": p.incident.id, "title": p.incident.title, "label": p.incident.level,
             "mark": p.mark, "lat": p.incident.lat, "lon": p.incident.lon, "km": round(p.km, 1),
             "at": p.incident.updated,
             "detail": " · ".join(x for x in (p.incident.kind, p.incident.status, p.incident.size, p.incident.council) if x)}
            for p in placed if p.km is not None
        ],
    }
    times = [i.updated for i in incidents if i.updated is not None and i.updated <= now + 600]
    return Assessment(level, reason, details, content_at=max(times, default=None), signals=signals)


class RfsAdapter(PollingAdapter):
    id = "rfs"
    name = "NSW RFS incidents"
    category = Category.OFFICIAL
    link = "https://www.rfs.nsw.gov.au/fire-information/fires-near-me"
    Settings = RfsSettings
    # Something somewhere in NSW is updated every few minutes; 6 quiet hours means the feed is stuck.
    content_budget_minutes = 360
    # An update can take a few minutes to reach the feed.
    publish_delay_minutes = 10
    settings: RfsSettings

    def __init__(self, ctx: AdapterContext, settings: RfsSettings) -> None:
        super().__init__(ctx, settings)
        self._etag: str | None = None
        self._modified: float | None = None
        self._incidents: list[Incident] = []

    async def fetch(self) -> list[Incident]:
        headers = {"If-None-Match": self._etag} if self._etag else {}
        response = await self.ctx.http.get(self.settings.url, headers=headers)
        if response.status_code == 304 and self._etag:
            return self._incidents
        if response.status_code == 429:
            raise SourceError("The RFS asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError("The RFS sent something that isn't JSON") from exc
        self._incidents = parse_feed(data)
        self._etag = response.headers.get("ETag")
        try:
            self._modified = parsedate_to_datetime(response.headers.get("Last-Modified", "")).timestamp()
        except (TypeError, ValueError):
            self._modified = None
        return self._incidents

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        incidents = await self.fetch()
        loc = self.ctx.config.home.location
        home = (loc.lat, loc.lon)
        alertable = {}
        for p in (place(i, home, self.settings) for i in incidents):
            i = p.incident
            alertable[i.id] = self.see_item(
                i.id, i.updated, title=f"RFS {i.level}: {i.title}", level=p.mark, url=i.url, now=now,
                details={"lat": i.lat, "lon": i.lon, "km": p.km, "level": i.level, "council": i.council},
            ).alertable
        result = assess(incidents, home, loc.name, self.settings, now, alertable)
        self._add_to_timeline(incidents, home, now)
        return Assessment(result.level, result.reason, result.details, content_at=result.content_at,
                          feed_at=self._modified, signals=result.signals)

    def _add_to_timeline(self, incidents: list[Incident], home: tuple[float, float], now: float) -> None:
        """Put each Watch and Act or Emergency Warning near home on the timeline, once per level."""
        for i in incidents:
            p = place(i, home, self.settings)
            if i.level not in (EMERGENCY, WATCH) or p.km is None or p.km > self.settings.show_km:
                continue
            self.ctx.db.add_event(
                i.updated or now, self.id, self.category.value, p.mark if p.mark != "watch" else "info",
                f"RFS {i.level}: {i.title}", url=self.link,
                details={"reason": f"{p.km:.0f} km away" + (f", {i.council}" if i.council else "")
                                   + (f"; {i.status}" if i.status else "")},
                dedupe_key=f"rfs:{i.id}:{i.level}",
            )
