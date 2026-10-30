"""Telco status pages: what the telcos themselves say about outages.

Two telcos publish their status as data that sitrep may read:

- TPG: a small summary of which services have incidents now, then the list of
  incidents for each of those services;
- Aussie Broadband: its outage feed, with current, recent and upcoming events.

The rest (Telstra, Belong, Optus, Vodafone, iiNet, NBN Co, Vocus) are links only:
their pages need your address and JavaScript, their terms rule out automated checks,
or (Vocus) they refuse a request that honestly says it comes from a program.
Downdetector is never used, directly or through sites that republish it.

These tiles are for information: they're shown but never change the overall status.
Telcos post planned maintenance all the time, so sitrep sets maintenance apart from
real (unplanned) outages, and only marks a telco elevated when an unplanned outage
is widespread:

- TPG: one covering several states or the whole country, or many in your state at once;
- Aussie Broadband: one it rates major or critical, one classed as a major outage
  under the ACMA's outage rules, or one affecting many customers.

TPG's incident data names the staff member who posted each incident; sitrep drops
that as soon as it's read. Aussie Broadband's own wording isn't stored or shown (its
terms don't allow republishing), only facts such as the areas and customer numbers.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from pydantic import Field

from ..config import Settings
from ..models import Assessment, Category, Level
from ..util import format_duration, sydney_timestamp
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, describe_error, get_json


class TelcoLink(Settings):
    name: str
    url: str
    # Whose network it runs on, matched against the carrier in your home settings.
    network: str = ""


DEFAULT_LINKS = [
    {"name": "Telstra", "url": "https://www.telstra.com.au/outages", "network": "Telstra"},
    {"name": "Belong", "url": "https://www.belong.com.au/go/network-outages", "network": "Telstra"},
    {"name": "Optus", "url": "https://www.optus.com.au/living-network/service-status", "network": "Optus"},
    {"name": "Vodafone", "url": "https://www.vodafone.com.au/network/status", "network": "TPG group"},
    {"name": "iiNet", "url": "https://www.iinet.net.au/network-status", "network": "TPG group"},
    {"name": "NBN Co", "url": "https://www.nbnco.com.au/support/network-status"},
    {"name": "Vocus", "url": "https://www.vocus.com.au/help-and-support/outages", "network": "Vocus"},
]


class TelcosSettings(PollingSettings):
    interval_seconds: float = 600
    # Your state, for "in NSW" counts.
    state: str = Field("NSW", pattern=r"^(NSW|VIC|QLD|SA|WA|TAS|NT|ACT)$")
    tpg: bool = True
    tpg_url: str = "https://www.tpg.com.au/servicestatus"
    tpg_page: str = "https://www.tpg.com.au/network-status"
    # TPG groups its services; incidents are read for these groups only.
    tpg_groups: list[str] = Field(default_factory=lambda: ["Internet", "Mobile", "Phone"])
    aussie_broadband: bool = True
    aussie_url: str = "https://wrapper.aussiebroadband.com.au/api/signup/system-outages"
    aussie_page: str = "https://www.aussiebroadband.com.au/network/outages/"
    # This many unplanned outages in your state at once marks a telco elevated...
    many_in_state: int = Field(5, ge=1)
    # ...as does one unplanned outage affecting at least this many customers.
    major_customers: int = Field(10000, ge=1)
    links: list[TelcoLink] = Field(default_factory=lambda: [TelcoLink(**link) for link in DEFAULT_LINKS])
    # How many unplanned outages each telco's card lists.
    show_outages: int = Field(4, ge=1)


STATES = ("NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT")
_STATE = re.compile(r"\b(NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\b")
_NATIONAL = re.compile(r"\b(?:national(?:ly|wide)?|all states|Australia-wide)\b", re.IGNORECASE)
_PLANNED = re.compile(r"\b(?:planned|scheduled|maintenance)\b", re.IGNORECASE)


def postcode_state(code: int) -> str | None:
    """The state or territory an Australian postcode belongs to."""
    for low, high, state in (
        (200, 299, "ACT"), (800, 999, "NT"), (2600, 2618, "ACT"), (2900, 2920, "ACT"),
        (2000, 2999, "NSW"), (3000, 3999, "VIC"), (4000, 4999, "QLD"), (5000, 5999, "SA"),
        (6000, 6999, "WA"), (7000, 7999, "TAS"),
    ):
        if low <= code <= high:
            return state
    return None


@dataclass(frozen=True)
class Outage:
    telco: str
    id: str
    where: str  # "Newtown (NSW)", "Newman (WA)"
    service: str  # "NBN", "Mobile"
    planned: bool
    states: tuple[str, ...] = ()
    national: bool = False
    since: float | None = None
    until: float | None = None  # when the telco expects it to end, if it says
    customers: int | None = None
    major: bool = False  # the telco rates it major or critical, or the ACMA rules class it as major
    note: str = ""  # the ACMA class, when there is one

    @property
    def scope(self) -> str:
        if self.national:
            return "nationwide"
        return " and ".join(self.states) if self.states else ""


def _wall_time(text: str | None) -> float | None:
    """TPG's '2026-10-23 08:00:00', which is Sydney time."""
    if not text:
        return None
    try:
        return sydney_timestamp(datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def _iso_time(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --- TPG ---


def tpg_categories(data: Any, groups: list[str]) -> list[tuple[str, str]]:
    """(category id, service name) for each service TPG says has an incident now."""
    try:
        services = data["data"]
    except (TypeError, KeyError) as exc:
        raise SourceError("TPG's status summary is missing its services") from exc
    found = []
    for group in groups:
        for category_id, info in (services.get(group) or {}).items():
            if isinstance(info, dict) and info.get("now"):
                found.append((str(category_id), str(info.get("title") or group)))
    return found


def parse_tpg(data: Any, service: str) -> list[Outage]:
    try:
        items = data["data"]
    except (TypeError, KeyError) as exc:
        raise SourceError(f"TPG's {service} incident list is missing its data") from exc
    outages = []
    for item in items or []:
        if not isinstance(item, dict) or str(item.get("internal_only", "0")) == "1":
            continue
        heading = re.sub(r"\s+", " ", str(item.get("heading") or "")).strip()
        message = str(item.get("message") or "")
        sites = str(item.get("affected_sites") or "")
        outages.append(Outage(
            telco="TPG",
            id=str(item.get("id")),
            where=re.sub(r"\s+Customers\b", "", heading).strip() or sites or "Unknown area",
            service=service,
            planned=bool(_PLANNED.search(f"{heading} {message}")),
            states=tuple(s for s in STATES if s in _STATE.findall(f"{sites} {heading}")),
            national=sites.strip().upper() == "ALL" or bool(_NATIONAL.search(f"{sites} {heading}")),
            since=_wall_time(item.get("date")),
            until=_wall_time(item.get("estimate")),
        ))
    return outages


# --- Aussie Broadband ---

_CUSTOMERS = re.compile(r"Impacted customers:\s*([\d,]+)", re.IGNORECASE)
_DESCRIPTION = re.compile(r"Description:\s*([^<\n]+)", re.IGNORECASE)


def _area_name(area: str) -> tuple[str, str | None]:
    """'NEWMAN(6753)' -> ('Newman', 'WA'); 'NBN CVC: Aspley (Link 5)' -> ('Aspley', None)."""
    match = re.match(r"^\s*(.+?)\s*\((\d{3,4})\)\s*$", area)
    if match:
        return match.group(1).title(), postcode_state(int(match.group(2)))
    name = re.sub(r"^.*?:\s*", "", area)
    return re.sub(r"\s*\(Link \d+\)$", "", name).strip() or area, None


def parse_aussie(data: Any) -> list[Outage]:
    """Aussie Broadband's events happening now (unplanned outages and maintenance)."""
    try:
        current = data[0]["current"]
    except (TypeError, KeyError, IndexError) as exc:
        raise SourceError("Aussie Broadband's outage feed is missing its current outages") from exc
    items = current.values() if isinstance(current, dict) else current
    outages = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        public = str(item.get("public") or "")  # read for facts only; never stored
        areas = [_area_name(str(a)) for a in item.get("areas") or []]
        states = {state for _, state in areas if state}
        described = _DESCRIPTION.search(public)
        if described:
            states.update(_STATE.findall(described.group(1)))
        customers = item.get("acma_premises_affected")
        found = _CUSTOMERS.search(public)
        if not customers and found:
            customers = found.group(1).replace(",", "")
        try:
            customers = int(customers) if customers else None
        except (TypeError, ValueError):
            customers = None
        where = areas[0][0] if areas else "Unknown area"
        if areas and areas[0][1]:
            where += f" ({areas[0][1]})"
        if len(areas) > 1:
            where += f" and {len(areas) - 1} other {'area' if len(areas) == 2 else 'areas'}"
        title = str(item.get("title") or "")
        planned = bool(_PLANNED.search(title))
        classification = str(item.get("acma_classification") or "").strip()
        outages.append(Outage(
            telco="Aussie Broadband",
            id=str(item.get("ref")),
            where=where,
            service=str(item.get("services") or "").replace(",", ", "),
            planned=planned,
            states=tuple(s for s in STATES if s in states),
            since=_iso_time(item.get("start_date")),
            until=_iso_time(item.get("end_date")) if planned else None,
            customers=customers,
            major=str(item.get("prio") or "").lower() in ("major", "critical") or bool(classification),
            note=f"ACMA: {classification}" if classification else "",
        ))
    return outages


# --- Judging ---


@dataclass
class TelcoStatus:
    name: str
    page: str
    level: str = Level.NORMAL.value
    summary: str = ""
    error: str | None = None
    unplanned: list[dict[str, Any]] = field(default_factory=list)
    unplanned_count: int = 0
    unplanned_in_state: int = 0
    planned_now: int = 0
    planned_in_state: int = 0


def widespread(outage: Outage, s: TelcosSettings) -> bool:
    if outage.planned:
        return False
    return (outage.national or len(outage.states) >= 2 or outage.major
            or (outage.customers or 0) >= s.major_customers)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _describe(outage: Outage) -> str:
    parts = [outage.where]
    if outage.scope and outage.scope not in outage.where:
        parts.append(outage.scope)
    if outage.service:
        parts.append(outage.service)
    if outage.customers:
        parts.append(f"{outage.customers:,} customers")
    if outage.note:
        parts.append(outage.note)
    return ", ".join(parts)


def judge(name: str, page: str, outages: list[Outage], s: TelcosSettings, now: float) -> TelcoStatus:
    state = s.state
    unplanned = [o for o in outages if not o.planned]
    planned = [o for o in outages if o.planned]
    in_state = [o for o in unplanned if state in o.states or o.national]
    big = [o for o in unplanned if widespread(o, s)]
    # Biggest first: widespread ones, then your state's, then by customers, newest first.
    unplanned.sort(key=lambda o: (not widespread(o, s), o not in in_state, -(o.customers or 0), -(o.since or 0)))

    status = TelcoStatus(
        name, page,
        unplanned_count=len(unplanned), unplanned_in_state=len(in_state),
        planned_now=len(planned), planned_in_state=sum(state in o.states or o.national for o in planned),
        unplanned=[{**asdict(o), "scope": o.scope, "widespread": widespread(o, s)} for o in unplanned[: s.show_outages]],
    )
    maintenance = (f"{_plural(len(planned), 'maintenance window')} now, {status.planned_in_state} in {state}"
                   if planned else "no maintenance now")
    if big or len(in_state) >= s.many_in_state:
        status.level = Level.ELEVATED.value
    if not unplanned:
        status.summary = f"no unplanned outages ({maintenance})"
    else:
        lead = big[0] if big else (in_state[0] if in_state else unplanned[0])
        since = f", for {format_duration(now - lead.since)}" if lead.since and lead.since <= now else ""
        count = f"{_plural(len(unplanned), 'unplanned outage')}, {len(in_state) or 'none'} in {state}"
        if len(unplanned) == 1:
            status.summary = f"{count}: {_describe(lead)}{since} ({maintenance})"
        else:
            label = "widespread" if big else "biggest"
            status.summary = f"{count}. {label.capitalize()}: {_describe(lead)}{since} ({maintenance})"
    return status


def assess(statuses: list[TelcoStatus], links: list[dict[str, Any]], s: TelcosSettings) -> Assessment:
    checked = [t for t in statuses if not t.error]
    if statuses and not checked:
        raise SourceError("Couldn't check " + " or ".join(f"{t.name} ({t.error})" for t in statuses))
    level = Level.ELEVATED if any(t.level == Level.ELEVATED.value for t in checked) else Level.NORMAL
    reasons = [f"{t.name}: {t.summary}" if not t.error else f"{t.name}: couldn't check ({t.error})" for t in statuses]
    if not statuses:
        reasons = ["No telco feeds are switched on; the status page links are below"]
    details = {"state": s.state, "telcos": [asdict(t) for t in statuses], "links": links}
    return Assessment(level, "; ".join(reasons), details)


# --- The adapter ---


class TelcosAdapter(PollingAdapter):
    id = "telcos"
    name = "Telco status pages"
    category = Category.INFO
    Settings = TelcosSettings
    settings: TelcosSettings

    def __init__(self, ctx: AdapterContext, settings: TelcosSettings) -> None:
        super().__init__(ctx, settings)
        self._links = [
            {**link.model_dump(), "tag": self._tag(link.name, link.network)}
            for link in settings.links
        ]
        self._tags = {"TPG": self._tag("TPG", "TPG group"), "Aussie Broadband": self._tag("Aussie Broadband", "Aussie Broadband")}

    def _tag(self, name: str, network: str) -> str | None:
        home = self.ctx.config.home
        if home.isp_name and name.casefold() == home.isp_name.strip().casefold():
            return "your provider"
        if home.carrier and network.casefold() == home.carrier.strip().casefold():
            return "your network"
        return None

    async def fetch_tpg(self) -> list[Outage]:
        s = self.settings
        summary = await get_json(self.ctx.http, f"{s.tpg_url}/aggregated", "TPG")
        categories = tpg_categories(summary, s.tpg_groups)
        lists = await asyncio.gather(*(
            get_json(self.ctx.http, f"{s.tpg_url}/categories/now/{category_id}", "TPG") for category_id, _ in categories
        ))
        return [outage for (_, service), data in zip(categories, lists) for outage in parse_tpg(data, service)]

    async def fetch_aussie(self) -> list[Outage]:
        return parse_aussie(await get_json(self.ctx.http, self.settings.aussie_url, "Aussie Broadband"))

    async def poll(self) -> Assessment:
        s = self.settings
        now = self.ctx.clock()
        feeds = []
        if s.tpg:
            feeds.append(("TPG", s.tpg_page, self.fetch_tpg()))
        if s.aussie_broadband:
            feeds.append(("Aussie Broadband", s.aussie_page, self.fetch_aussie()))
        results = await asyncio.gather(*(fetch for _, _, fetch in feeds), return_exceptions=True)
        statuses = []
        for (name, page, _), result in zip(feeds, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                self.log.warning("Couldn't check %s: %s", name, describe_error(result))
                statuses.append(TelcoStatus(name, page, error=describe_error(result)))
            else:
                statuses.append(judge(name, page, result, s, now))
                self._add_to_timeline(result, now)
        assessment = assess(statuses, self._links, s)
        for telco in assessment.details["telcos"]:
            telco["tag"] = self._tags.get(telco["name"])
        return assessment

    def _add_to_timeline(self, outages: list[Outage], now: float) -> None:
        """Put each widespread unplanned outage on the timeline once, from when it started."""
        for outage in outages:
            if not widespread(outage, self.settings):
                continue
            self.ctx.db.add_event(
                outage.since if outage.since and outage.since <= now else now,
                self.id, self.category.value, Level.ELEVATED.value,
                f"{outage.telco} reports an outage: {outage.where}",
                details={"reason": _describe(outage)},
                dedupe_key=f"telco:{outage.telco}:{outage.id}",
            )
