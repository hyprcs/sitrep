"""Sydney Trains and Sydney Metro: service alerts, to spot network-wide or multi-line suspensions.

Every 2 minutes sitrep reads the train and metro alerts from Transport for NSW's Open
Data Hub (GTFS-realtime, as JSON; about 17 kB each, compressed), with the same free token
as Live Traffic. The data is © Transport for NSW, under CC BY 4.0.

Most alerts are planned trackwork, published days ahead: "buses replace trains between
Hornsby and Strathfield on Saturday". Transport for NSW doesn't mark them as planned, but
their cause is MAINTENANCE or CONSTRUCTION, and "Station Update" alerts are about lifts,
ramps and stairs. Everything else that's in force now and says services are disrupted is
an unplanned disruption.

How it's judged:

- elevated: an unplanned suspension (no trains running) in force now that covers the
  whole network, 3 or more lines, or the whole Metro line;
- a suspension on one or two lines, delays, and all planned trackwork are shown but
  don't count. Never critical.

Lines are recognised by the codes in Transport for NSW's route ids (IWL is the T2 Inner
West & Leppington line, for example). Station alerts also list bus routes that stop
there, which aren't counted.

Alerts carry no time of their own (only when they're in force), so an alert counts as new
when sitrep first sees it, as long as its previous look at the feed worked and was recent.
The feed's own generation time is checked: if it stops moving on for 15 minutes, the
source shows as frozen.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import Field, SecretStr

from ..models import SYDNEY, Assessment, Category, Level, Signal
from .base import PollingAdapter, PollingSettings, SourceError, retry_after_seconds
from .livetraffic import TOKEN_HELP

# Transport for NSW's line codes, as they start route ids, and the names passengers know.
LINES = {
    "NSN": "T1 North Shore", "WST": "T1 Western", "IWL": "T2 Inner West & Leppington", "BNK": "T3 Bankstown",
    "T3": "T3 Liverpool & Inner West", "ESI": "T4 Eastern Suburbs & Illawarra", "CMB": "T5 Cumberland",
    "T6": "T6 Lidcombe & Bankstown", "OLY": "T7 Olympic Park", "APS": "T8 Airport & South", "NTH": "T9 Northern",
    "BMT": "Blue Mountains", "CCN": "Central Coast & Newcastle", "HUN": "Hunter", "SCO": "South Coast",
    "SHL": "Southern Highlands", "SMNW": "Metro M1",
}
METRO = {"SMNW"}
PLANNED_CAUSES = {"MAINTENANCE", "CONSTRUCTION"}
DISRUPTIVE_EFFECTS = {"NO_SERVICE", "REDUCED_SERVICE", "SIGNIFICANT_DELAYS", "DETOUR", "MODIFIED_SERVICE"}
DISRUPTION_WORDS = re.compile(
    r"\b(not running|no trains|suspended|delays?|allow (extra|plenty of) (travel )?time|buses replace|stopped|"
    r"signal (fault|failure|repairs)|power (outage|supply|failure)|police|medical|emergency|incident)\b", re.IGNORECASE)
SUSPENSION_WORDS = re.compile(
    r"\b(not running|no trains|suspended|trains do not run|services have stopped|no services|buses replace)\b",
    re.IGNORECASE)
WHOLE_NETWORK_WORDS = re.compile(r"\b(all lines|all services|entire network|network[- ]wide|across the network)\b",
                                 re.IGNORECASE)


class TransitSettings(PollingSettings):
    interval_seconds: float = 120
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://api.transport.nsw.gov.au/v2/gtfs/alerts"
    feeds: list[str] = Field(default_factory=lambda: ["sydneytrains", "metro"])
    # An unplanned suspension covering this many lines is elevated.
    lines_for_elevated: int = Field(3, ge=1)


@dataclass
class Alert:
    id: str
    feed: str
    header: str
    description: str
    cause: str
    effect: str
    url: str | None
    lines: list[str]  # line codes it names (only known train and metro lines)
    whole_network: bool  # it names an operator with no particular line or stop
    whole_lines: bool  # it names lines without particular stops
    periods: list[tuple[float | None, float | None]] = field(default_factory=list)

    def in_force(self, now: float) -> bool:
        if not self.periods:
            return True
        return any((s is None or s <= now) and (e is None or now <= e) for s, e in self.periods)

    @property
    def planned(self) -> bool:
        return self.cause in PLANNED_CAUSES or self.header.lower().startswith("station update")

    @property
    def text(self) -> str:
        return f"{self.header}\n{self.description}"

    @property
    def disruption(self) -> bool:
        return not self.planned and (self.effect in DISRUPTIVE_EFFECTS or bool(DISRUPTION_WORDS.search(self.text)))

    @property
    def suspension(self) -> bool:
        return self.disruption and (self.effect == "NO_SERVICE" or bool(SUSPENSION_WORDS.search(self.text)))

    def widespread(self, s: TransitSettings) -> bool:
        if self.whole_network or WHOLE_NETWORK_WORDS.search(self.text):
            return True
        if len(self.lines) >= s.lines_for_elevated:
            return True
        return set(self.lines) == METRO and self.whole_lines  # the whole Metro line

    @property
    def line_names(self) -> list[str]:
        return [LINES[c] for c in self.lines]


def _english(value: Any) -> str:
    for item in (value or {}).get("translation", []) if isinstance(value, dict) else []:
        if isinstance(item, dict) and item.get("language") in ("en", None):
            return re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
    return ""


def _seconds(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def line_code(route_id: str) -> str | None:
    """'IWL_2b' -> 'IWL'; '4T.T.BMT' -> 'BMT'; a bus route -> None."""
    code = route_id.split("_")[0].split(".")[-1]
    return code if code in LINES else None


def parse(data: Any, feed: str) -> tuple[list[Alert], float | None]:
    """The alerts in a feed, and when the feed itself was made."""
    if not isinstance(data, dict) or not isinstance(data.get("entity", []), list):
        raise SourceError(f"The {feed} alerts aren't the GTFS-realtime JSON sitrep expects")
    made = _seconds((data.get("header") or {}).get("timestamp"))
    alerts = []
    for entity in data.get("entity", []):
        alert = entity.get("alert") if isinstance(entity, dict) else None
        if not isinstance(alert, dict):
            continue
        informed = [i for i in alert.get("informedEntity") or [] if isinstance(i, dict)]
        lines = sorted({c for i in informed if (c := line_code(str(i.get("routeId") or "")))},
                       key=list(LINES).index)
        line_wide = [i for i in informed if line_code(str(i.get("routeId") or "")) and not i.get("stopId")]
        alerts.append(Alert(
            id=str(entity.get("id") or ""), feed=feed,
            header=_english(alert.get("headerText")), description=_english(alert.get("descriptionText")),
            cause=str(alert.get("cause") or "UNKNOWN_CAUSE"), effect=str(alert.get("effect") or "UNKNOWN_EFFECT"),
            url=_english(alert.get("url")) or None, lines=lines,
            whole_network=any(i.get("agencyId") and not i.get("routeId") and not i.get("stopId") for i in informed),
            whole_lines=bool(line_wide) and len(line_wide) == len([i for i in informed if line_code(str(i.get("routeId") or ""))]),
            periods=[(_seconds(p.get("start")), _seconds(p.get("end"))) for p in alert.get("activePeriod") or []
                     if isinstance(p, dict)],
        ))
    return alerts, made


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _where(a: Alert) -> str:
    names = a.line_names
    if a.whole_network or not names:
        return "the network"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def assess(alerts: list[Alert], s: TransitSettings, now: float, alertable: dict[str, bool] | None = None,
           made: float | None = None) -> Assessment:
    on_now = [a for a in alerts if a.in_force(now)]
    disruptions = [a for a in on_now if a.disruption]
    raised = [a for a in disruptions if a.suspension and a.widespread(s)]
    trackwork_now = [a for a in on_now if a.planned and a.cause in PLANNED_CAUSES]
    planned_total = sum(1 for a in alerts if a.cause in PLANNED_CAUSES)

    if raised:
        level = Level.ELEVATED
        reason = "; ".join(f"Unplanned suspension across {_where(a)}: {a.header}" for a in raised)
    elif disruptions:
        level = Level.NORMAL
        reason = (f"{_plural(len(disruptions), 'unplanned disruption')}, none across several lines: "
                  + "; ".join(f"{_where(a)}: {a.header}" for a in disruptions[:3]))
    else:
        level = Level.NORMAL
        reason = "No unplanned disruptions on Sydney Trains or Metro"
    reason += (f". {_plural(planned_total, 'planned trackwork alert')} published"
               f" ({len(trackwork_now)} in force now)")

    signals = tuple(
        Signal(f"transit:{a.id}", Level.ELEVATED, (SYDNEY,), title=f"Unplanned suspension across {_where(a)}: {a.header}",
               alertable=(alertable or {}).get(a.id, True))
        for a in raised
    )
    details = {
        "disruptions": [{**asdict(a), "line_names": a.line_names, "widespread": a.widespread(s),
                         "suspension": a.suspension} for a in disruptions],
        "trackwork_now": [{"header": a.header, "line_names": a.line_names, "url": a.url} for a in trackwork_now[:10]],
        "planned_total": planned_total,
        "alerts_total": len(alerts),
    }
    return Assessment(level, reason, details, content_at=made, signals=signals)


class TransitAdapter(PollingAdapter):
    id = "transit"
    name = "Sydney Trains and Metro"
    category = Category.OFFICIAL
    link = "https://transportnsw.info/alerts"
    Settings = TransitSettings
    scope = SYDNEY
    # The feed says when it was made; it's remade every few seconds.
    content_budget_minutes = 15
    settings: TransitSettings

    async def fetch(self, feed: str) -> tuple[list[Alert], float | None]:
        token = self.settings.api_key.get_secret_value().strip()
        if not token:
            raise SourceError(f"No Transport for NSW token yet: add TFNSW_API_KEY to .env. {TOKEN_HELP}")
        response = await self.ctx.http.get(f"{self.settings.base_url.rstrip('/')}/{feed}", params={"format": "json"},
                                           headers={"Authorization": f"apikey {token}"})
        if response.status_code == 401:
            raise SourceError(f"Transport for NSW didn't accept the token. Check TFNSW_API_KEY in .env. {TOKEN_HELP}")
        if response.status_code in (403, 429):
            detail = response.headers.get("X-Error-Detail") or f"HTTP {response.status_code}"
            raise SourceError(f"Transport for NSW refused the request ({detail[:120]})", retry_after_seconds(response))
        response.raise_for_status()
        try:
            return parse(response.json(), feed)
        except ValueError as exc:
            raise SourceError(f"The {feed} alerts aren't JSON") from exc

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        answers = await asyncio.gather(*(self.fetch(feed) for feed in self.settings.feeds))
        seen: dict[str, Alert] = {}
        for alerts, _ in answers:
            for a in alerts:
                seen.setdefault(a.id, a)  # an alert about Central can be in both feeds
        alerts = list(seen.values())
        made = [m for _, m in answers if m is not None]
        alertable = {}
        for a in alerts:
            if a.in_force(now) and a.disruption:
                alertable[a.id] = self.see_item(
                    a.id, None, title=f"{_where(a)}: {a.header}", now=now, url=a.url,
                    level=Level.ELEVATED.value if a.suspension and a.widespread(self.settings) else "info",
                    details={"lines": a.lines, "cause": a.cause, "effect": a.effect, "suspension": a.suspension},
                ).alertable
        result = assess(alerts, self.settings, now, alertable, min(made) if made else None)
        self._add_to_timeline(alerts, now)
        return result

    def _add_to_timeline(self, alerts: list[Alert], now: float) -> None:
        """Put each unplanned suspension on the timeline once, when sitrep first sees it."""
        for a in alerts:
            if a.in_force(now) and a.suspension:
                self.ctx.db.add_event(
                    now, self.id, self.category.value,
                    Level.ELEVATED.value if a.widespread(self.settings) else "info",
                    f"Trains: {_where(a)}: {a.header}", url=a.url or self.link,
                    details={"reason": a.description[:300]}, dedupe_key=f"transit:{a.id}",
                )
