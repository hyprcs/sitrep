"""IODA: is a whole carrier, NSW, or Australia dropping off the internet?

IODA (Internet Outage Detection and Analysis, run by Georgia Tech) measures every
network on the internet in independent ways ("methods"):

- routing (BGP): how many of a network's address blocks the rest of the internet
  can still find a route to;
- active probing: how many of its address blocks answer pings;
- a network telescope: stray background traffic. It fires often on small
  Australian networks in quiet weeks, so sitrep ignores it by default.

IODA raises an alert when a method falls below its usual level ("critical") and
another when it recovers ("normal"). So something is down on a method when the
latest alert for that method is critical.

Every poll (5 minutes) asks for the alerts of the last 14 days, with one call per
kind of place (country, region, network) however many are watched. Every 15
minutes it also fetches the last 24 hours of active-probing counts for the charts.

How it's judged (the numbers are in config.yaml):

- Australia or NSW down on 1 method -> elevated; on 2 or more -> critical.
- A carrier counts as down when one of its networks is down on 2 or more methods
  at once. One carrier down -> elevated; 2 or more -> critical.
- A carrier down on a single method is shown as a dip but doesn't raise the
  level. Those happen often and are rarely outages anyone notices.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, model_validator

from ..config import Carrier, Settings
from ..models import Assessment, Category, Level
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, describe_error, get_json

METHOD_LABELS = {
    "bgp": "routing",
    "ping-slash24": "active probing",
    "merit-nt": "telescope traffic",
    "gtr": "Google traffic",
}
METHOD_ORDER = list(METHOD_LABELS)
PROBING = "ping-slash24"

# IODA returns at most this many alerts per call (its default page size).
ALERT_LIMIT = 500

Code = Annotated[str, BeforeValidator(str)]  # YAML may read 173 as a number


class Area(Settings):
    name: str
    type: Literal["country", "region"]
    code: Code


class IodaSettings(PollingSettings):
    interval_seconds: float = 300
    base_url: str = "https://api.ioda.inetintel.cc.gatech.edu/v2"
    # How far back to look for the alert that started an outage. IODA itself
    # closes off outages after 14 days.
    lookback_days: float = Field(14, gt=0)
    signals_every_minutes: float = 15
    signals_hours: float = 24
    areas: list[Area] = Field(default_factory=list)
    carriers: list[Carrier] = Field(default_factory=list)  # from the top of config.yaml
    ignore_methods: list[str] = Field(default_factory=lambda: ["merit-nt"])
    area_critical_methods: int = Field(2, ge=1)
    carrier_down_methods: int = Field(2, ge=1)
    critical_when_carriers_down: int = Field(2, ge=1)

    @model_validator(mode="after")
    def _something_to_watch(self) -> IodaSettings:
        if not self.areas and not self.carriers:
            raise ValueError("list at least one area or carrier to watch")
        return self


@dataclass(frozen=True)
class Alert:
    code: str
    method: str
    time: float
    critical: bool
    value: float | None = None
    usual: float | None = None


@dataclass(frozen=True)
class Drop:
    """A method something is down on right now."""

    method: str
    since: float
    ratio: float | None  # the latest reading as a share of IODA's usual level


@dataclass(frozen=True)
class Series:
    start: float
    step: float
    values: tuple[float | None, ...]


Drops = dict[str, dict[str, Drop]]  # entity code -> method -> Drop


# --- Reading IODA's responses ---


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _data(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise SourceError("IODA sent something sitrep doesn't understand")
    if payload.get("error"):
        raise SourceError(f"IODA said: {payload['error']}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise SourceError("IODA's answer had no data in it")
    return data


def parse_alerts(payload: Any) -> list[Alert]:
    alerts = []
    for item in _data(payload):
        try:
            alerts.append(
                Alert(
                    code=str(item["entity"]["code"]),
                    method=str(item["datasource"]),
                    time=float(item["time"]),
                    critical=item["level"] == "critical",
                    value=_number(item.get("value")),
                    usual=_number(item.get("historyValue")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"IODA sent an alert sitrep can't read (missing {exc})") from exc
    return alerts


def current_drops(alerts: list[Alert], ignore: tuple[str, ...] | list[str] = ()) -> Drops:
    """Every method each entity is down on now: those whose latest alert is critical."""
    latest: dict[tuple[str, str], Drop | None] = {}
    for alert in sorted(alerts, key=lambda a: a.time):
        if alert.method in ignore:
            continue
        key = (alert.code, alert.method)
        if not alert.critical:
            latest[key] = None
            continue
        ratio = alert.value / alert.usual if alert.value is not None and alert.usual else None
        before = latest.get(key)
        latest[key] = Drop(alert.method, before.since if before else alert.time, ratio)
    drops: Drops = {}
    for (code, method), drop in latest.items():
        if drop is not None:
            drops.setdefault(code, {})[method] = drop
    return drops


def parse_signals(payload: Any) -> dict[tuple[str, str], Series]:
    """(entity type, code) -> time series, from IODA's /signals/raw answer."""
    out: dict[tuple[str, str], Series] = {}
    for group in _data(payload):
        for item in group if isinstance(group, list) else [group]:
            try:
                key = (str(item["entityType"]), str(item["entityCode"]))
                out[key] = Series(
                    float(item["from"]), float(item["step"]), tuple(_number(v) for v in item["values"])
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceError(f"IODA sent a chart series sitrep can't read (missing {exc})") from exc
    return out


def combine(parts: list[Series | None]) -> Series | None:
    """Add up several networks' series (a carrier with several ASNs) point by point."""
    present = [p for p in parts if p is not None]
    if not present:
        return None
    first = present[0]
    same = [p for p in present if p.start == first.start and p.step == first.step]
    n = min(len(p.values) for p in same)
    values = []
    for i in range(n):
        points = [p.values[i] for p in same]
        values.append(None if any(v is None for v in points) else sum(points))
    return Series(first.start, first.step, tuple(values))


def normalise(series: Series | None) -> dict[str, Any] | None:
    """The series as a share of its median over the window, for the dashboard's lines."""
    if series is None:
        return None
    known = [v for v in series.values if v is not None]
    usual = statistics.median(known) if known else 0
    if usual <= 0:
        return None
    return {
        "from": series.start,
        "step": series.step,
        "values": [None if v is None else round(v / usual, 3) for v in series.values],
    }


# --- Judging ---


@dataclass
class EntityStatus:
    key: str
    name: str
    kind: str  # "area" or "carrier"
    drops: list[Drop]  # for a carrier: from its worst network
    asns: list[int]
    home: bool = False
    level: str = "normal"  # normal | watch (a dip that doesn't count) | elevated | critical
    label: str = "OK"
    worst_asn: int | None = None
    detail: str | None = None

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(d.method for d in self.drops)

    @property
    def title(self) -> str:
        return f"{self.name} (your network)" if self.home else self.name


def _sorted(drops: dict[str, Drop]) -> list[Drop]:
    rank = lambda d: METHOD_ORDER.index(d.method) if d.method in METHOD_ORDER else len(METHOD_ORDER)  # noqa: E731
    return sorted(drops.values(), key=lambda d: (rank(d), d.method))


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _percent(ratios: list[float | None]) -> str | None:
    known = [round(r * 100) for r in ratios if r is not None]
    if not known:
        return None
    low, high = min(known), max(known)
    return f"{low}%" if low == high else f"{low}–{high}%"


def describe_methods(entities: list[EntityStatus], now: float) -> str:
    """e.g. 'active probing at 21% of usual for 25 minutes' (same methods for every entity)."""
    parts = []
    any_percent = False
    for i, method in enumerate(entities[0].methods):
        label = METHOD_LABELS.get(method, method)
        percent = _percent([e.drops[i].ratio for e in entities])
        any_percent |= percent is not None
        parts.append(f"{label} at {percent}" if percent else f"{label} down")
    since = min(d.since for e in entities for d in e.drops)
    return f"{_join(parts)}{' of usual' if any_percent else ''} for {format_duration(now - since)}"


def _methods_brief(entities: list[EntityStatus]) -> str:
    """e.g. 'routing (40% of usual) and active probing (30%)'."""
    parts = []
    for i, method in enumerate(entities[0].methods):
        label = METHOD_LABELS.get(method, method)
        percent = _percent([e.drops[i].ratio for e in entities])
        if percent and not any("%" in p for p in parts):
            percent += " of usual"
        parts.append(f"{label} ({percent})" if percent else label)
    return _join(parts)


def _grouped(entities: list[EntityStatus], now: float, verb: str = "down on") -> list[str]:
    """One phrase per set of methods: 'Australia and NSW down on active probing (21% of usual) for 30 minutes'."""
    groups: dict[tuple[str, ...], list[EntityStatus]] = {}
    for entity in entities:
        groups.setdefault(entity.methods, []).append(entity)
    phrases = []
    for group in groups.values():
        since = min(d.since for e in group for d in e.drops)
        names = _join([e.title for e in group])
        phrases.append(f"{names} {verb} {_methods_brief(group)} for {format_duration(now - since)}")
    return phrases


def entity_statuses(
    drops_by_type: dict[str, Drops], s: IodaSettings, home_carrier: str = ""
) -> list[EntityStatus]:
    out = []
    for area in s.areas:
        drops = _sorted(drops_by_type.get(area.type, {}).get(area.code, {}))
        status = EntityStatus(f"{area.type}:{area.code}", area.name, "area", drops, [])
        if len(drops) >= s.area_critical_methods:
            status.level, status.label = "critical", "Critical"
        elif drops:
            status.level, status.label = "elevated", "Elevated"
        out.append(status)
    asn_drops = drops_by_type.get("asn", {})
    for carrier in s.carriers:
        worst = max(carrier.asns, key=lambda asn: len(asn_drops.get(str(asn), {})))
        drops = _sorted(asn_drops.get(str(worst), {}))
        status = EntityStatus(
            f"carrier:{carrier.name}", carrier.name, "carrier", drops, list(carrier.asns),
            home=bool(home_carrier) and carrier.name.casefold() == home_carrier.strip().casefold(),
            worst_asn=worst if drops and len(carrier.asns) > 1 else None,
        )
        if len(drops) >= s.carrier_down_methods:
            status.level, status.label = "elevated", "Down"
        elif drops:
            status.level, status.label = "watch", "Dip"
        out.append(status)
    return out


def _looks_like_measurement_glitch(entities: list[EntityStatus], s: IodaSettings) -> bool:
    """Every network dipping on probing alone at the same moment, with routing fine.

    On 16 and 18 October 2026 IODA's probing showed Australia, NSW and every big
    carrier falling to about 6-21% of usual at the same moment, with routing
    unchanged and gaps in IODA's own data.
    """
    methods = {d.method for e in entities for d in e.drops}
    country_dip = any(e.kind == "area" and e.drops and e.key.startswith("country:") for e in entities)
    dipping_carriers = sum(1 for e in entities if e.kind == "carrier" and e.drops)
    return methods == {PROBING} and country_dip and dipping_carriers >= max(2, len(s.carriers) - 1)


def assess(drops_by_type: dict[str, Drops], s: IodaSettings, now: float, home_carrier: str = "") -> Assessment:
    entities = entity_statuses(drops_by_type, s, home_carrier)
    areas = [e for e in entities if e.kind == "area"]
    carriers = [e for e in entities if e.kind == "carrier"]
    areas_critical = [e for e in areas if e.level == "critical"]
    areas_elevated = [e for e in areas if e.level == "elevated"]
    down = sorted((e for e in carriers if e.level == "elevated"), key=lambda e: not e.home)
    dips = sorted((e for e in carriers if e.level == "watch"), key=lambda e: not e.home)

    for entity in entities:
        if entity.drops:
            text = describe_methods([entity], now)
            entity.detail = _capitalise(f"AS{entity.worst_asn}: {text}" if entity.worst_asn else text)

    if areas_critical or len(down) >= s.critical_when_carriers_down:
        level = Level.CRITICAL
        reason = "; ".join(_grouped(areas_critical, now) + _grouped(down, now) + _grouped(areas_elevated, now))
    elif areas_elevated or down:
        level = Level.ELEVATED
        reason = "; ".join(_grouped(areas_elevated, now) + _grouped(down, now))
        if _looks_like_measurement_glitch(entities, s):
            reason += (
                ". Routing looks normal and every network dipped at once, "
                "which can be a glitch in IODA's own measurements"
            )
    else:
        level = Level.NORMAL
        watched = [a.name for a in areas]
        if carriers:
            watched.append(f"{len(carriers)} carrier{'s' if len(carriers) != 1 else ''}")
        reason = f"No outages for {_or(watched)}"
        if dips:
            reason += ". " + "; ".join(_grouped(dips, now, "dipped on")) + ", but one method isn't enough on its own"

    details = {
        "entities": [
            {
                "key": e.key,
                "name": e.name,
                "kind": e.kind,
                "home": e.home,
                "level": e.level,
                "label": e.label,
                "detail": e.detail,
                "asns": e.asns,
                "drops": [
                    {"method": d.method, "label": METHOD_LABELS.get(d.method, d.method), "since": d.since, "ratio": d.ratio}
                    for d in e.drops
                ],
            }
            for e in entities
        ],
        "ignored_methods": list(s.ignore_methods),
    }
    return Assessment(level, _capitalise(reason), details)


def _or(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _capitalise(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- The adapter ---


class IodaAdapter(PollingAdapter):
    id = "ioda"
    name = "Australian networks (IODA)"
    category = Category.NETWORK
    link = "https://ioda.inetintel.cc.gatech.edu/country/AU"
    Settings = IodaSettings
    uses_carriers = True
    settings: IodaSettings

    def __init__(self, ctx: AdapterContext, settings: IodaSettings) -> None:
        super().__init__(ctx, settings)
        self._charts: dict[str, dict[str, Any] | None] = {}
        self._charts_at: float | None = None

    def _groups(self) -> dict[str, list[str]]:
        """Entity codes to ask about, by IODA entity type, in config order."""
        groups: dict[str, list[str]] = {}
        for area in self.settings.areas:
            groups.setdefault(area.type, []).append(area.code)
        for carrier in self.settings.carriers:
            groups.setdefault("asn", []).extend(str(asn) for asn in carrier.asns)
        return {kind: list(dict.fromkeys(codes)) for kind, codes in groups.items()}

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = self.settings.base_url.rstrip("/") + path
        return await get_json(self.ctx.http, url, "IODA", params)

    async def fetch_alerts(self, kind: str, codes: list[str], now: float) -> list[Alert]:
        lookback = self.settings.lookback_days
        # If the list comes back full, IODA may have cut it short, so ask for less.
        windows = sorted({lookback, min(lookback, 1), min(lookback, 0.25)}, reverse=True)
        alerts: list[Alert] = []
        for days in windows:
            params = {
                "entityType": kind,
                "entityCode": ",".join(codes),
                "from": int(now - days * 86400),
                "until": int(now),
                "limit": ALERT_LIMIT,
            }
            alerts = parse_alerts(await self._get("/outages/alerts", params))
            if len(alerts) < ALERT_LIMIT:
                break
            self.log.warning("IODA returned a full page of alerts for %d days; asking for less", days)
        return alerts

    async def fetch_charts(self, now: float) -> dict[str, dict[str, Any] | None]:
        groups = self._groups()
        params = {"datasource": PROBING, "from": int(now - self.settings.signals_hours * 3600), "until": int(now)}
        answers = await asyncio.gather(
            *(self._get(f"/signals/raw/{kind}/{','.join(codes)}", params) for kind, codes in groups.items())
        )
        series: dict[tuple[str, str], Series] = {}
        for payload in answers:
            series.update(parse_signals(payload))
        charts: dict[str, dict[str, Any] | None] = {}
        for area in self.settings.areas:
            charts[f"{area.type}:{area.code}"] = normalise(series.get((area.type, area.code)))
        for carrier in self.settings.carriers:
            parts = [series.get(("asn", str(asn))) for asn in carrier.asns]
            charts[f"carrier:{carrier.name}"] = normalise(combine(parts))
        return charts

    def _charts_due(self, now: float) -> bool:
        if self._charts_at is None:
            return True
        return now - self._charts_at >= self.settings.signals_every_minutes * 60 - 5

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        groups = self._groups()
        results = await asyncio.gather(*(self.fetch_alerts(kind, codes, now) for kind, codes in groups.items()))
        ignore = tuple(self.settings.ignore_methods)
        drops = {kind: current_drops(alerts, ignore) for kind, alerts in zip(groups, results)}

        if self._charts_due(now):
            try:
                self._charts = await self.fetch_charts(now)
                self._charts_at = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the charts are a nice-to-have; the alerts still count
                self.log.warning("IODA charts not updated (%s); will try again next poll", describe_error(exc))

        result = assess(drops, self.settings, now, self.ctx.config.home.carrier)
        for entity in result.details["entities"]:
            entity["chart"] = self._charts.get(entity["key"])
        result.details["charts_at"] = self._charts_at
        result.details["chart_hours"] = self.settings.signals_hours
        return result
