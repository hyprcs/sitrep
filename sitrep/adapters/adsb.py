"""Aircraft over Sydney, from adsb.lol (with adsb.fi to double-check).

Every minute sitrep asks adsb.lol for the aircraft within 40 km of the CBD. It does two
things with the answer:

- **Counts.** How many aircraft are in the air there (not counting ones on the ground or
  airport vehicles), against what's usual for this hour of the week. Far fewer than
  usual might mean the airspace has been closed, but an adsb.lol problem is much more
  likely, so before it says anything sitrep asks a second, independent network
  (adsb.fi) once. Only if adsb.fi also sees far fewer is it elevated (about Sydney as a
  whole). At night, when few aircraft fly (Sydney Airport's curfew), it can't tell.
- **Circling.** A helicopter, or any aircraft below 3,000 ft, that stays within 2 km of
  one spot for 10 minutes or more is circling there, which police, rescue and news
  helicopters do over an incident. It's shown on the map and goes into the convergence
  grid as "aircraft circling", but never raises anything on its own. Not every police
  aircraft broadcasts, so no circling means nothing. Airport circuits (Sydney Airport,
  Bankstown), balloons, gliders and airport vehicles don't count.

Positions are kept in memory for 15 minutes only; sitrep saves the hourly counts and the
circling events (aircraft, place, how long), never tracks. Aircraft data: adsb.lol
(ODbL 1.0) and adsb.fi (personal, non-commercial use, credited with a link).
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from ..config import Settings
from ..geo import cell_of, km_between
from ..models import SYDNEY, Assessment, Category, Level, Signal
from ..places import gazetteer
from ..util import sydney_datetime
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, describe_error, retry_after_seconds

NM_PER_KM = 1 / 1.852
CREDIT = "Aircraft data: adsb.lol (ODbL 1.0); adsb.fi"


class Airport(Settings):
    name: str
    lat: float
    lon: float
    km: float


DEFAULT_AIRPORTS = [
    {"name": "Sydney Airport", "lat": -33.9461, "lon": 151.1772, "km": 4},
    {"name": "Bankstown Airport", "lat": -33.9244, "lon": 150.9883, "km": 5},
]


class AdsbSettings(PollingSettings):
    interval_seconds: float = Field(60, ge=60, le=120)
    url: str = "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}"
    check_url: str = "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}"
    # The circle to watch: 40 km around the CBD.
    centre_lat: float = -33.8688
    centre_lon: float = 151.2093
    radius_km: float = Field(40, gt=0, le=400)
    # Far fewer aircraft than usual: at most this share of the usual count, for this many
    # checks in a row, at an hour when at least collapse_min_usual are usually up.
    collapse_ratio: float = Field(0.3, gt=0, lt=1)
    collapse_checks: int = Field(3, ge=1)
    collapse_min_usual: float = Field(8, ge=1)
    # Circling: within orbit_km of one spot for orbit_minutes, below orbit_below_ft (any
    # helicopter counts at any height), and above orbit_above_ft (landed is not circling).
    orbit_km: float = Field(2, gt=0)
    orbit_minutes: float = Field(10, ge=3)
    orbit_below_ft: float = 3000
    orbit_above_ft: float = 300
    airports: list[Airport] = Field(default_factory=lambda: [Airport(**a) for a in DEFAULT_AIRPORTS])


@dataclass(frozen=True)
class Plane:
    hex: str
    callsign: str
    type: str
    category: str
    lat: float
    lon: float
    alt: float | None  # feet; None on the ground
    at: float  # when this position was current

    @property
    def airborne(self) -> bool:
        return self.alt is not None and not self.category.startswith("C")

    @property
    def helicopter(self) -> bool:
        return self.category == "A7"

    @property
    def name(self) -> str:
        who = self.callsign or self.hex.upper()
        return f"{who} ({self.type})" if self.type else who


def parse_aircraft(doc: Any, centre: tuple[float, float], radius_km: float) -> tuple[list[Plane], float | None]:
    if not isinstance(doc, dict) or not isinstance(doc.get("ac"), list):
        raise SourceError("the answer had no aircraft list")
    now = doc.get("now")
    now = now / 1000 if isinstance(now, (int, float)) else None
    planes = []
    for a in doc["ac"]:
        if not isinstance(a, dict) or not isinstance(a.get("lat"), (int, float)) or not isinstance(a.get("lon"), (int, float)):
            continue
        if km_between(*centre, a["lat"], a["lon"]) > radius_km:
            continue
        alt = a.get("alt_baro")
        seen = a.get("seen_pos") if isinstance(a.get("seen_pos"), (int, float)) else 0
        planes.append(Plane(
            hex=str(a.get("hex") or "").lower(), callsign=str(a.get("flight") or "").strip(),
            type=str(a.get("t") or "").strip(), category=str(a.get("category") or ""),
            lat=float(a["lat"]), lon=float(a["lon"]), alt=float(alt) if isinstance(alt, (int, float)) else None,
            at=(now or 0) - seen,
        ))
    return planes, now


def airborne(planes: list[Plane]) -> int:
    return sum(1 for p in planes if p.airborne)


# --- Circling ---


def may_circle(p: Plane, s: AdsbSettings) -> bool:
    """Whether this position can be part of an orbit: low (or a helicopter), flying, not near an airport."""
    if not p.airborne or p.category.startswith("B") or p.alt is None or p.alt < s.orbit_above_ft:
        return False
    if not (p.helicopter or p.alt < s.orbit_below_ft):
        return False
    return not any(km_between(a.lat, a.lon, p.lat, p.lon) <= a.km for a in s.airports)


@dataclass
class Orbit:
    hex: str
    name: str
    started: float
    lat: float
    lon: float
    last: float
    low_ft: float
    high_ft: float
    helicopter: bool

    @property
    def id(self) -> str:
        return f"{self.hex}:{int(self.started)}"

    def minutes(self, now: float) -> float:
        return (min(self.last, now) - self.started) / 60

    @property
    def place(self) -> str:
        return gazetteer().nearest(self.lat, self.lon).name

    def describe(self) -> str:
        what = "Helicopter" if self.helicopter else "Aircraft"
        return f"{what} {self.name} circling near {self.place} for {self.minutes(self.last):.0f} minutes"


def circling(track: list[Plane], s: AdsbSettings) -> tuple[float, float] | None:
    """The centre, if the track's last orbit_minutes stay within orbit_km of one spot."""
    if not track:
        return None
    end = track[-1].at
    window = [p for p in track if p.at >= end - s.orbit_minutes * 60 - s.interval_seconds / 2]
    if end - window[0].at < s.orbit_minutes * 60 - s.interval_seconds / 2:
        return None  # not watched long enough yet
    if len(window) < 0.6 * s.orbit_minutes * 60 / s.interval_seconds:
        return None  # too many gaps to be sure
    if any(b.at - a.at > 3 * s.interval_seconds for a, b in zip(window, window[1:])):
        return None
    lat = statistics.fmean(p.lat for p in window)
    lon = statistics.fmean(p.lon for p in window)
    if any(km_between(lat, lon, p.lat, p.lon) > s.orbit_km for p in window):
        return None
    return lat, lon


@dataclass
class Tracks:
    """The last 15 minutes of positions for aircraft that might be circling, in memory only."""

    keep_seconds: float = 900
    tracks: dict[str, deque[Plane]] = field(default_factory=dict)
    orbits: dict[str, Orbit] = field(default_factory=dict)

    def update(self, planes: list[Plane], now: float, s: AdsbSettings) -> list[Orbit]:
        """Add this check's positions; returns the orbits going on now."""
        for p in planes:
            if not p.hex:
                continue
            if may_circle(p, s):
                track = self.tracks.setdefault(p.hex, deque())
                if not track or p.at > track[-1].at:
                    track.append(p)
            else:
                self.tracks.pop(p.hex, None)  # climbed away, landed or reached an airport: start again
        for hex_, track in list(self.tracks.items()):
            while track and track[0].at < now - self.keep_seconds:
                track.popleft()
            if not track:
                del self.tracks[hex_]
        current = {}
        for hex_, track in self.tracks.items():
            centre = circling(list(track), s)
            if centre is None:
                continue
            latest = track[-1]
            known = self.orbits.get(hex_)
            if known and latest.at - known.last <= 3 * s.interval_seconds:
                known.last, known.lat, known.lon = latest.at, *centre
                known.low_ft = min(known.low_ft, *(p.alt or 0 for p in track))
                known.high_ft = max(known.high_ft, *(p.alt or 0 for p in track))
                current[hex_] = known
            else:
                started = latest.at - s.orbit_minutes * 60
                alts = [p.alt or 0 for p in track]
                current[hex_] = Orbit(hex_, latest.name, started, centre[0], centre[1], latest.at, min(alts), max(alts),
                                      latest.helicopter)
        self.orbits = current
        return list(current.values())


# --- Counts ---


def hour_of_week(ts: float) -> tuple[int, int]:
    when = sydney_datetime(ts)
    return when.weekday(), when.hour


def usual_count(history: dict[int, float], now: float) -> tuple[float | None, str]:
    """What's usual for this hour: the same hour of the week over the last 4 weeks, or,
    until there are two of those, the same hour of the day over the last week."""
    this_week, this_hour = hour_of_week(now)
    past = {ts: v for ts, v in history.items() if ts < now - 3600}
    weekly = [v for ts, v in past.items() if hour_of_week(ts) == (this_week, this_hour) and ts >= now - 28 * 86400]
    if len(weekly) >= 2:
        return statistics.median(weekly), "this hour of the week"
    daily = [v for ts, v in past.items() if hour_of_week(ts)[1] == this_hour and ts >= now - 7 * 86400]
    if len(daily) >= 3:
        return statistics.median(daily), "this hour of the day"
    return None, ""


@dataclass(frozen=True)
class Check:
    """What adsb.fi said when asked to double-check a low count."""

    at: float
    count: int | None  # None if it couldn't be asked
    error: str | None = None


def assess(planes: list[Plane], count: int, usual: float | None, basis: str, low_checks: int, check: Check | None,
           orbits: list[Orbit], s: AdsbSettings, now: float, data_at: float | None) -> Assessment:
    low_line = usual is not None and usual >= s.collapse_min_usual and count <= usual * s.collapse_ratio
    suspected = low_line and low_checks >= s.collapse_checks
    confirmed = suspected and check is not None and check.count is not None and check.count <= usual * s.collapse_ratio
    where = f"within {s.radius_km:g} km of the CBD"
    usual_text = f"usually {usual:.0f} at {basis}" if usual is not None else "learning what's usual"
    signals: tuple[Signal, ...] = ()
    if confirmed:
        level = Level.ELEVATED
        reason = (f"Far fewer aircraft over Sydney than usual: {count} in the air {where} ({usual_text}); "
                  f"adsb.fi agrees ({check.count})")
        signals = (Signal("adsb:collapse", Level.ELEVATED, (SYDNEY,), title=reason, at=now),)
    elif usual is None and not orbits:
        level = Level.LEARNING
        reason = f"{count} aircraft in the air {where}. Learning what's usual for each hour"
    else:
        level = Level.NORMAL
        reason = f"{count} aircraft in the air {where} ({usual_text})"
        if suspected and check is not None:
            reason += (f". adsb.lol sees far fewer than usual, but adsb.fi sees {check.count}: probably an adsb.lol "
                       "problem" if check.count is not None else
                       f". adsb.lol sees far fewer than usual; couldn't check with adsb.fi ({check.error})")
    if orbits:
        reason += ". " + "; ".join(o.describe() for o in orbits)
    up = [p for p in planes if p.airborne]
    details = {
        "airborne": count,
        "on_ground": len(planes) - len(up),
        "helicopters": sum(1 for p in up if p.helicopter),
        "usual": usual,
        "basis": basis,
        "radius_km": s.radius_km,
        "check": {"count": check.count, "at": check.at, "error": check.error} if check else None,
        "orbits": [{"name": o.name, "place": o.place, "lat": o.lat, "lon": o.lon, "minutes": round(o.minutes(now), 1),
                    "started": o.started, "low_ft": o.low_ft, "high_ft": o.high_ft, "helicopter": o.helicopter}
                   for o in orbits],
        "map": [{"kind": "aircraft", "id": f"adsb:{o.id}", "title": o.describe(), "label": "Aircraft circling",
                 "mark": "info", "lat": o.lat, "lon": o.lon, "km": None, "at": o.last,
                 "detail": f"{o.low_ft:,.0f} to {o.high_ft:,.0f} ft"} for o in orbits],
        "credit": CREDIT,
    }
    return Assessment(level, reason, details, content_at=data_at, signals=signals)


class AdsbAdapter(PollingAdapter):
    id = "adsb"
    name = "Aircraft over Sydney"
    category = Category.SENSORS
    link = "https://adsb.lol/"
    Settings = AdsbSettings
    scope = SYDNEY
    content_budget_minutes = 5
    settings: AdsbSettings

    def __init__(self, ctx: AdapterContext, settings: AdsbSettings) -> None:
        super().__init__(ctx, settings)
        self.tracks = Tracks()
        self._low_checks = 0
        self._check: Check | None = None
        self._hour: tuple[int, list[int]] | None = None  # this hour's counts, for its average

    def _url(self, template: str) -> str:
        s = self.settings
        return template.format(lat=s.centre_lat, lon=s.centre_lon, nm=round(s.radius_km * NM_PER_KM + 0.5))

    async def _get(self, url: str, name: str) -> Any:
        response = await self.ctx.http.get(url)
        if response.status_code == 429:
            raise SourceError(f"{name} asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 400:
            raise SourceError(f"{name} answered HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{name} sent something that isn't JSON") from exc

    async def double_check(self, now: float) -> Check:
        """Ask adsb.fi once how many aircraft it sees (at most every 5 minutes)."""
        if self._check and now - self._check.at < 300:
            return self._check
        try:
            doc = await self._get(self._url(self.settings.check_url), "adsb.fi")
            planes, _ = parse_aircraft(doc, (self.settings.centre_lat, self.settings.centre_lon), self.settings.radius_km)
            self._check = Check(now, airborne(planes))
        except Exception as exc:  # a failed double-check is reported, never fatal
            self._check = Check(now, None, describe_error(exc))
        return self._check

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        s = self.settings
        doc = await self._get(self._url(s.url), "adsb.lol")
        planes, data_at = parse_aircraft(doc, (s.centre_lat, s.centre_lon), s.radius_km)
        count = airborne(planes)
        history = self.ctx.db.gauges_since(self.id, "airborne", now - 29 * 86400)
        usual, basis = usual_count(history, now)
        self._save_count(count, now)

        low = usual is not None and usual >= s.collapse_min_usual and count <= usual * s.collapse_ratio
        self._low_checks = self._low_checks + 1 if low else 0
        check = None
        if self._low_checks >= s.collapse_checks:
            check = await self.double_check(now)
        elif not low:
            self._check = None

        orbits = self.tracks.update(planes, data_at or now, s)
        for o in orbits:
            self._see_orbit(o, now)
        return assess(planes, count, usual, basis, self._low_checks, check, orbits, s, now, data_at)

    def _save_count(self, count: int, now: float) -> None:
        """Keep this hour's average count (one number an hour is all that's saved)."""
        hour = int(now // 3600)
        if self._hour is None or self._hour[0] != hour:
            self._hour = (hour, [])
        self._hour[1].append(count)
        self.ctx.db.set_gauge(self.id, "airborne", now, statistics.fmean(self._hour[1]))

    def _see_orbit(self, o: Orbit, now: float) -> None:
        cell = cell_of(o.lat, o.lon)
        self.see_item(
            o.id, o.started, title=o.describe(), now=now,
            details={"lat": o.lat, "lon": o.lon, "orbit": True, "suburb": o.place, "cell": list(cell),
                     "aircraft": o.name, "helicopter": o.helicopter, "minutes": round(o.minutes(now), 1),
                     "low_ft": o.low_ft, "high_ft": o.high_ft},
        )
        self.ctx.db.add_event(o.started, self.id, self.category.value, "info",
                              f"{'Helicopter' if o.helicopter else 'Aircraft'} {o.name} circling near {o.place}",
                              details={"reason": f"Within {self.settings.orbit_km:g} km of one spot for "
                                                 f"{self.settings.orbit_minutes:g}+ minutes, {o.low_ft:,.0f} to "
                                                 f"{o.high_ft:,.0f} ft"},
                              dedupe_key=f"adsb:{o.id}")
