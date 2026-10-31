"""Australian ransomware victims, from ransomware.live.

ransomware.live watches ransomware gangs' leak sites and lists each organisation a gang
claims to have hit. Every hour sitrep asks its API for Australian victims (it needs a free
key in .env: RANSOMWARE_LIVE_API_KEY; see "ransomware.live key" in the README). Its data
is cached for 30 minutes, and its fair-use rules say polling faster than that is a breach,
so the hour can't be set below 30 minutes.

How it's judged:

- info: a new Australian victim (about one every two days);
- elevated: a new victim in telecoms, health, energy and utilities (which includes water),
  government, finance or transport, for 24 hours. ransomware.live has no telecoms
  sector, so a telco is spotted by name (Telstra, Optus, "broadband" and so on);
- elevated: more Australian victims in the last 24 hours than is plausible at the usual
  rate (from the last 90 days), and at least 5. Gangs often post several at once, so
  only a big burst counts.
- never critical by itself: a victim being listed is a claim, often weeks after the
  attack, not proof of an ongoing outage.

sitrep keeps only each victim's name, the gang, the sector, when ransomware.live first
saw it and its ransomware.live page. Never the leak-site links, screenshots or
descriptions. ransomware.live's data is for non-commercial use, credited "Source:
Ransomware.live" with a link back, and mustn't be passed on as a feed or API; the
dashboard is for you only.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import Field, SecretStr

from ..models import Assessment, Category, Level, Signal, national
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, retry_after_seconds
from .bluesky import poisson_tail

SITE = "https://www.ransomware.live"
ATTRIBUTION = "Source: Ransomware.live"
KEY_HELP = 'See "ransomware.live key" in the README.'

DEFAULT_CRITICAL_SECTORS = [
    "Healthcare", "Energy & Utilities", "Government & Defense", "Financial Services", "Transportation",
]
# ransomware.live has no telecoms sector, so telcos are found by name. These match the
# start of a word, ignoring case, in the victim's name or website.
DEFAULT_TELCO_WORDS = [
    "telecom", "telco", "Telstra", "Optus", "TPG", "Vodafone", "iiNet", "Aussie Broadband", "Vocus", "NBN",
    "broadband",
]


class RansomwareSettings(PollingSettings):
    # ransomware.live caches for 30 minutes; polling faster breaks its fair-use rules.
    interval_seconds: float = Field(3600, ge=1800)
    api_key: SecretStr = SecretStr("")
    url: str = "https://api-pro.ransomware.live"
    country: str = "AU"
    # A victim in a critical sector counts as elevated for this many hours.
    recent_hours: float = Field(24, gt=0)
    critical_sectors: list[str] = Field(default_factory=lambda: list(DEFAULT_CRITICAL_SECTORS))
    telco_words: list[str] = Field(default_factory=lambda: list(DEFAULT_TELCO_WORDS))
    # The usual rate comes from this many days of history.
    baseline_days: int = Field(90, ge=14)
    # A burst is elevated when it's at least this big and this unlikely at the usual rate.
    burst_min_victims: int = Field(5, ge=2)
    burst_probability: float = Field(0.001, gt=0, lt=1)
    # How many of the latest victims the dashboard lists.
    show_victims: int = Field(8, ge=1)


@dataclass(frozen=True)
class Victim:
    id: str
    name: str
    group: str
    sector: str
    discovered: float  # when ransomware.live first saw the listing
    link: str | None  # its ransomware.live page
    telco: bool = False

    def critical(self, sectors: list[str]) -> bool:
        return self.telco or self.sector.lower() in {s.lower() for s in sectors}

    @property
    def kind(self) -> str:
        return "Telecoms" if self.telco else self.sector

    def describe(self) -> str:
        return f"{self.name} ({self.kind}), listed by {self.group}"


def _time(text: Any) -> float | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def _missing(value: Any) -> bool:
    return value is None or str(value).strip() in ("", "None", "null")


def parse_victims(doc: Any, telco_words: list[str]) -> list[Victim]:
    victims = doc.get("victims") if isinstance(doc, dict) else None
    if not isinstance(victims, list):
        raise SourceError("ransomware.live sent no victim list")
    telco = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in telco_words) + ")", re.IGNORECASE) \
        if telco_words else None
    out = []
    for v in victims:
        if not isinstance(v, dict):
            continue
        discovered = _time(v.get("discovered"))
        name = "" if _missing(v.get("victim")) else str(v["victim"]).strip()
        if discovered is None or not name:
            continue
        website = "" if _missing(v.get("website")) else str(v["website"])
        out.append(Victim(
            id=str(v.get("id") or f"{v.get('group')}:{name}"), name=name,
            group="an unknown gang" if _missing(v.get("group")) else str(v["group"]).strip(),
            sector="Unknown sector" if _missing(v.get("activity")) or v.get("activity") == "Not Found"
            else str(v["activity"]).strip(),
            discovered=discovered, link=None if _missing(v.get("permalink")) else str(v["permalink"]),
            telco=bool(telco and telco.search(f"{name} {website}")),
        ))
    return out


def usual_per_day(victims: list[Victim], now: float, days: int) -> float:
    """Victims a day over the baseline period, not counting the last 24 hours."""
    start, end = now - days * 86400, now - 86400
    return sum(1 for v in victims if start <= v.discovered < end) / (days - 1)


def assess(victims: list[Victim], s: RansomwareSettings, now: float,
           alertable: dict[str, bool] | None = None) -> Assessment:
    alertable = alertable or {}
    latest = sorted((v for v in victims if v.discovered <= now + 600), key=lambda v: v.discovered, reverse=True)
    day = [v for v in latest if now - v.discovered <= 86400]
    usual = usual_per_day(latest, now, s.baseline_days)
    chance = poisson_tail(len(day), usual)
    burst = len(day) >= s.burst_min_victims and chance < s.burst_probability
    critical = [v for v in latest if now - v.discovered <= s.recent_hours * 3600 and v.critical(s.critical_sectors)]

    rate = f"usually {usual:.1f} a day"
    signals = tuple(
        Signal(f"ransomware:{v.id}", Level.ELEVATED, (national("cyber"),),
               title=f"Australian ransomware victim in {v.kind}: {v.describe()}",
               alertable=alertable.get(v.id, True), at=v.discovered)
        for v in critical
    )
    if burst:
        signals += (Signal(
            "ransomware:burst", Level.ELEVATED, (national("cyber"),),
            title=f"{len(day)} Australian ransomware victims in the last 24 hours ({rate})",
            alertable=any(alertable.get(v.id, True) for v in day), at=day[0].discovered,
        ),)

    parts = [f"New Australian ransomware victim in {v.kind}: {v.describe()} "
             f"({format_duration(max(0.0, now - v.discovered))} ago)" for v in critical]
    if burst:
        parts.insert(0, f"{len(day)} Australian ransomware victims in the last 24 hours ({rate})")
    if parts:
        level, reason = Level.ELEVATED, "; ".join(parts)
    else:
        level = Level.NORMAL
        count = f"{len(day)} Australian victim{'' if len(day) == 1 else 's'} in the last 24 hours ({rate})"
        if latest:
            v = latest[0]
            reason = f"{count}. Latest: {v.describe()}, {format_duration(max(0.0, now - v.discovered))} ago"
        else:
            reason = f"{count}. None in the last {s.baseline_days} days"

    details = {
        "victims": [
            {"name": v.name, "group": v.group, "sector": v.kind, "discovered": v.discovered, "link": v.link,
             "critical": v.critical(s.critical_sectors), "recent": now - v.discovered <= s.recent_hours * 3600}
            for v in latest[: s.show_victims]
        ],
        "last_24h": len(day),
        "usual_per_day": round(usual, 2),
        "burst": burst,
        "critical_sectors": s.critical_sectors + ["Telecoms"],
        "attribution": ATTRIBUTION,
        "site": SITE,
    }
    return Assessment(level, reason, details, content_at=latest[0].discovered if latest else None, signals=signals)


def months_back(now: float, days: int) -> list[tuple[int, int]]:
    """The (year, month) pairs from `days` ago up to now, newest first."""
    when = datetime.fromtimestamp(now, timezone.utc)
    start = datetime.fromtimestamp(now - days * 86400, timezone.utc)
    year, month, out = when.year, when.month, []
    while (year, month) >= (start.year, start.month):
        out.append((year, month))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return out


def month_end(year: int, month: int) -> float:
    last = calendar.monthrange(year, month)[1]
    return datetime(year, month, last, 23, 59, 59, tzinfo=timezone.utc).timestamp() + 1


@dataclass
class _Month:
    fetched_at: float
    victims: list[Victim]


class RansomwareAdapter(PollingAdapter):
    id = "ransomware"
    name = "Ransomware victims (ransomware.live)"
    category = Category.CYBER
    link = SITE
    Settings = RansomwareSettings
    scope = national("cyber")
    # ransomware.live caches its data for 30 minutes.
    publish_delay_minutes = 30
    settings: RansomwareSettings

    def __init__(self, ctx: AdapterContext, settings: RansomwareSettings) -> None:
        super().__init__(ctx, settings)
        self._months: dict[tuple[int, int], _Month] = {}

    async def fetch_month(self, year: int, month: int) -> list[Victim]:
        key = self.settings.api_key.get_secret_value().strip()
        if not key:
            raise SourceError(f"No ransomware.live key yet: add RANSOMWARE_LIVE_API_KEY to .env. {KEY_HELP}")
        response = await self.ctx.http.get(
            f"{self.settings.url.rstrip('/')}/victims/",
            params={"country": self.settings.country, "year": str(year), "month": f"{month:02d}"},
            headers={"X-API-KEY": key, "Accept": "application/json"},
        )
        if response.status_code in (401, 403):
            raise SourceError(f"ransomware.live didn't accept the key. Check RANSOMWARE_LIVE_API_KEY in .env. "
                              f"{KEY_HELP}")
        if response.status_code == 429:
            raise SourceError("ransomware.live asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 400:
            raise SourceError(f"ransomware.live answered HTTP {response.status_code}")
        try:
            doc = response.json()
        except ValueError as exc:
            raise SourceError("ransomware.live sent something that isn't JSON") from exc
        return parse_victims(doc, self.settings.telco_words)

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        wanted = months_back(now, self.settings.baseline_days)
        for year, month in wanted:
            cached = self._months.get((year, month))
            # A month can still change until it's over; after that one more look makes it final.
            if cached is None or cached.fetched_at < month_end(year, month):
                self._months[(year, month)] = _Month(now, await self.fetch_month(year, month))
        self._months = {m: self._months[m] for m in wanted}
        victims = list({v.id: v for m in self._months.values() for v in m.victims}.values())

        alertable = {}
        for v in victims:
            if now - v.discovered > 7 * 86400:
                continue
            critical = v.critical(self.settings.critical_sectors)
            level = Level.ELEVATED.value if critical else "info"
            title = f"Ransomware: {v.describe()}"
            alertable[v.id] = self.see_item(
                v.id, v.discovered, title=title, level=level, url=v.link, now=now,
                details={"group": v.group, "sector": v.kind},
            ).alertable
            self.ctx.db.add_event(v.discovered, self.id, self.category.value, level, title, url=v.link,
                                  details={"reason": f"Sector: {v.kind}. {ATTRIBUTION}"},
                                  dedupe_key=f"ransomware:{v.id}")
        return assess(victims, self.settings, now, alertable)
