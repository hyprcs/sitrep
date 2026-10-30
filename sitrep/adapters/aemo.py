"""AEMO: the Australian Energy Market Operator, which runs the east-coast grid (the NEM).

Every 5 minutes sitrep reads the summary behind AEMO's public NEM dashboard
(visualisations.aemo.com.au): each region's demand, price and market flags, plus
AEMO's 10 latest market notices. When all 10 notices are new to sitrep (at
start-up, or after a burst of notices) it fills in the rest of the last day from
NEMWeb, AEMO's public file server. At start-up it also fetches the last two days
of 5-minute demand once, for the chart.

How it's judged, for your region (NSW unless you change it):

- critical: AEMO declares an actual LOR3 (not enough supply, so customers are
  being cut off), directs load shedding, or suspends the market;
- elevated: an actual LOR2 (one big failure away from load shedding), capped
  prices after a long run of extreme ones, a "significant power system event",
  or demand falling 25% or more within about half an hour, which usually means a
  large area has lost power;
- elsewhere in the NEM, an actual LOR3, load shedding or a suspended market is
  elevated;
- AEMO's forecasts (such as a forecast LOR3) are shown but never raise the level.

Prices are shown but not judged: price spikes are routine on hot evenings.

AEMO's timestamps are "market time", which is AEST (UTC+10) all year round.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx
from pydantic import Field

from ..models import Assessment, Category, Level
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, describe_error, get_json

MARKET_TIME = timezone(timedelta(hours=10))
STEP = 300  # AEMO's figures are for 5-minute intervals


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    pattern: re.Pattern[str]  # how notices write the region's name


REGIONS = {
    r.id: r
    for r in (
        Region("NSW1", "NSW", re.compile(r"\b(?:NSW1?|New South Wales)\b", re.IGNORECASE)),
        Region("QLD1", "Queensland", re.compile(r"\b(?:QLD1?|Queensland)\b", re.IGNORECASE)),
        Region("VIC1", "Victoria", re.compile(r"\b(?:VIC1?|Victorian?)\b", re.IGNORECASE)),
        # "SA" only in capitals, so the word "sa" in some other sense can't match.
        Region("SA1", "South Australia", re.compile(r"\bSA1?\b|\b(?i:South Australian?)\b")),
        Region("TAS1", "Tasmania", re.compile(r"\b(?:TAS1?|Tasmanian?)\b", re.IGNORECASE)),
    )
}


class AemoSettings(PollingSettings):
    interval_seconds: float = 300
    summary_url: str = "https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY"
    history_url: str = "https://visualisations.aemo.com.au/aemo/apps/api/report/5MIN"
    notices_url: str = "https://nemweb.com.au/Reports/Current/Market_Notice/"
    # Your region: NSW1, QLD1, VIC1, SA1 or TAS1.
    region: str = Field("NSW1", pattern=r"^(NSW1|QLD1|VIC1|SA1|TAS1)$")
    # Figures older than this mean AEMO's data has stopped updating.
    max_age_minutes: float = 20
    # Notices from this far back are shown and, when sitrep has missed some, fetched from NEMWeb.
    notice_hours: float = 24
    backfill_max_notices: int = Field(100, ge=10)
    # Demand falling by this share (compared with the highest reading in the half hour
    # before) for 10 minutes running is elevated, and stays so for drop_hold_minutes.
    demand_drop: float = Field(0.25, gt=0, lt=1)
    drop_hold_minutes: float = 60
    # Below this much demand, swings are too large in proportion to mean anything
    # (South Australia at midday, for example).
    demand_min_mw: float = 1000
    show_notices: int = Field(8, ge=1)


def market_time(text: str) -> float:
    """'2026-10-29T15:00:00' (AEMO market time) as a Unix timestamp."""
    return datetime.fromisoformat(text.strip()).replace(tzinfo=MARKET_TIME).timestamp()


def market_day(ts: float) -> str:
    """The market-time date of a timestamp, as NEMWeb writes it in file names: '20261029'."""
    return datetime.fromtimestamp(ts, MARKET_TIME).strftime("%Y%m%d")


# --- Market notices ---


@dataclass(frozen=True)
class Notice:
    id: int
    time: float  # when AEMO issued it
    type: str  # AEMO's notice type, such as "RESERVE NOTICE"
    title: str
    body: str
    regions: tuple[str, ...]  # region ids the notice is about


def notice_regions(title: str, body: str) -> tuple[str, ...]:
    """The regions a notice is about: the ones its title names, or failing that its text."""
    for text in (title, body):
        found = tuple(r.id for r in REGIONS.values() if r.pattern.search(text))
        if found:
            return found
    return ()


def make_notice(notice_id: int, time: float, type_: str, title: str, body: str) -> Notice:
    title = re.sub(r"\s+", " ", title).strip()
    body = body.replace("\r\n", "\n").strip()
    return Notice(notice_id, time, type_.strip().upper(), title, body, notice_regions(title, body))


def parse_summary_notice(raw: dict[str, Any]) -> Notice:
    return make_notice(
        int(raw["NOTICEID"]), market_time(raw["EFFECTIVEDATE"]),
        raw.get("TYPEID") or "", raw.get("EXTERNALREFERENCE") or "", raw.get("REASON") or "",
    )


_FILE_FIELDS = {
    "id": re.compile(r"^Notice ID\s*:\s*(\d+)", re.MULTILINE),
    "type": re.compile(r"^Notice Type ID\s*:[ \t]*(.*)$", re.MULTILINE),
    "title": re.compile(r"^External Reference\s*:[ \t]*(.*)$", re.MULTILINE),
    "created": re.compile(r"^Creation Date\s*:\s*(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})", re.MULTILINE),
}
_FILE_BODY = re.compile(r"^Reason\s*:\s*$(.*?)^-{20,}\s*$\s*^END OF REPORT", re.MULTILINE | re.DOTALL)


def parse_notice_file(text: str) -> Notice:
    """One of NEMWeb's plain-text market notice files."""
    found = {name: pattern.search(text) for name, pattern in _FILE_FIELDS.items()}
    if not all(found.values()):
        missing = ", ".join(name for name, m in found.items() if not m)
        raise SourceError(f"A NEMWeb market notice is missing its {missing}")
    day, clock = found["created"].groups()
    created = datetime.strptime(f"{day} {clock}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=MARKET_TIME)
    body = _FILE_BODY.search(text)
    return make_notice(
        int(found["id"].group(1)), created.timestamp(), found["type"].group(1),
        found["title"].group(1), body.group(1) if body else "",
    )


@dataclass(frozen=True)
class ListedNotice:
    id: int
    day: str  # market-time date from the file name, '20261029'
    url: str


_LISTING_ROW = re.compile(r'HREF="([^"]*?MKTNOTICE_(\d{8})\.R(\d+))"', re.IGNORECASE)


def parse_listing(html: str, base: str) -> list[ListedNotice]:
    """NEMWeb's directory page: one link per notice file."""
    root = httpx.URL(base)
    return [ListedNotice(int(n), day, str(root.join(href))) for href, day, n in _LISTING_ROW.findall(html)]


# What a notice says, as (condition, change). Conditions:
#   lor1, lor2, lor3   an actual Lack Of Reserve at that level
#   forecast_lor3      AEMO forecasts an LOR3 (it hasn't happened)
#   load_shed          AEMO has directed load shedding
#   suspension         the market is suspended
#   apc                administered pricing: prices capped after a run of extreme ones
#   significant_event  AEMO reports a "significant power system event"

_LOR_LEVEL = re.compile(r"\bLOR\s*([123])\b|\bLevel\s+([123])\b", re.IGNORECASE)


def _has(word: str, text: str) -> bool:
    return re.search(rf"\b{word}", text, re.IGNORECASE) is not None


def classify(notice: Notice) -> tuple[str, str] | None:
    """(condition, "start" or "end") for the notices sitrep tracks, otherwise None."""
    title = notice.title
    ending = _has("cancel", title)
    if notice.type == "RESERVE NOTICE":
        match = _LOR_LEVEL.search(title)
        if not match:
            return None  # a routine publication, such as "MT PASA PUBLICATION"
        level = int(match.group(1) or match.group(2))
        if _has("actual", title):
            return f"lor{level}", "end" if ending else "start"
        if _has("forecast", title) and level == 3:
            return "forecast_lor3", "end" if ending else "start"
        return None
    if notice.type == "LOAD SHED":
        return "load_shed", "end" if ending else "start"
    if notice.type == "LOAD RESTORE":
        return "load_shed", "end"
    if notice.type == "MARKET SUSPENSION":
        if _has("resumption", title):
            return "suspension", "end"
        if _has("declaration", title) and _has("suspension", title):
            return "suspension", "start"
        return None  # updates about prices during a suspension
    if notice.type == "ADMINISTERED PRICE CAP":
        if re.search(r"\bAP STARTED\b", title, re.IGNORECASE):
            return "apc", "start"
        if re.search(r"\bAP ENDED\b", title, re.IGNORECASE):
            return "apc", "end"
        return None
    if notice.type == "POWER SYSTEM EVENTS" and _has("significant", title):
        return "significant_event", "start"
    return None


# How long a condition counts with no further notice about it, in case sitrep missed
# the notice that ended it. A significant event has no "end" notice, so it simply fades.
CONDITION_HOURS = {
    "lor1": 12, "lor2": 12, "lor3": 12, "load_shed": 12,
    "forecast_lor3": 24, "apc": 26, "suspension": 14 * 24, "significant_event": 3,
}

LABELS = {
    "lor1": ("LOR1", "reserves are lower than AEMO likes"),
    "lor2": ("LOR2", "the grid is one big failure away from load shedding"),
    "lor3": ("LOR3", "there isn't enough supply, so some customers are being cut off"),
    "load_shed": ("Load shedding", "AEMO has told the networks to cut power to some customers"),
    "suspension": ("Market suspended", "AEMO has suspended the electricity market"),
    "apc": ("Prices capped", "prices are capped after a long run of extreme ones"),
    "significant_event": ("Power system event", "AEMO reported a significant power system event"),
    "forecast_lor3": ("Forecast LOR3", "AEMO forecasts a possible shortfall; nothing has happened yet"),
}

SEVERE = {"lor3", "load_shed", "suspension"}  # these count wherever they happen


def condition_level(name: str, home: bool) -> str:
    """How much a condition matters: a Level value, or "watch"/"info" for ones that don't count."""
    if home:
        if name in SEVERE:
            return Level.CRITICAL.value
        if name in ("lor2", "apc", "significant_event"):
            return Level.ELEVATED.value
        return "watch" if name == "forecast_lor3" else "info"
    return Level.ELEVATED.value if name in SEVERE else "info"


@dataclass
class Condition:
    name: str
    region: str
    since: float
    notice: Notice | None  # the latest notice about it; None when only a market flag says so
    level: str = "info"


def active_conditions(notices: Iterable[Notice], now: float) -> list[Condition]:
    """Conditions that AEMO has declared and not yet ended, read from its notices in order."""
    state: dict[tuple[str, str], Condition] = {}
    for notice in sorted(notices, key=lambda n: n.id):
        what = classify(notice)
        if not what or notice.time > now + 600:
            continue
        name, change = what
        for region in notice.regions:
            key = (name, region)
            if change == "end":
                state.pop(key, None)
            elif key in state:
                state[key].notice = notice
            else:
                state[key] = Condition(name, region, notice.time, notice)
    return [c for c in state.values() if now - c.notice.time <= CONDITION_HOURS[c.name] * 3600]


def notice_level(notice: Notice, home: str) -> str | None:
    """How the dashboard marks a notice, or None if it's routine and not worth showing."""
    what = classify(notice)
    if what:
        name, change = what
        if home in notice.regions:
            return condition_level(name, True) if change == "start" else "info"
        if name in SEVERE and notice.regions:
            return condition_level(name, False) if change == "start" else "info"
        return None
    # Not something sitrep scores, but worth seeing when it's about your region.
    if home in notice.regions and notice.type in ("POWER SYSTEM EVENTS", "MARKET INTERVENTION", "NEM SYSTEMS"):
        return "info"
    return None


_TYPE_LABELS = {"NEM SYSTEMS": "AEMO systems", "MARKET INTERVENTION": "Intervention", "POWER SYSTEM EVENTS": "Power system"}


def notice_label(notice: Notice) -> str:
    """A word or two for the dashboard's badge on a notice."""
    what = classify(notice)
    if what:
        name, change = what
        return LABELS[name][0] if change == "start" else "Ended"
    return _TYPE_LABELS.get(notice.type, "Notice")


_BOILERPLATE = re.compile(
    r"^(?:AEMO ELECTRICITY MARKET NOTICE\.?|Manager NEM Real Time Operations|AEMO Operations"
    r"|Refer (?:to )?(?:AEMO )?(?:Electricity )?Market Notice.*)$",
    re.IGNORECASE,
)


def notice_summary(notice: Notice, limit: int = 220) -> str:
    """The gist of a notice in a sentence or two, without AEMO's header, title and signature."""
    lines = []
    for line in (raw.strip() for raw in notice.body.splitlines()):
        heading = (line.isupper() and not any(ch.isdigit() for ch in line)) \
            or notice.title.casefold().startswith(line.casefold())
        if line and not heading and not _BOILERPLATE.match(line):
            lines.append(line)
    # AEMO wraps long sentences across lines, so a line only ends a sentence when the next starts a new one.
    text = ""
    for line, following in zip(lines, lines[1:] + [""]):
        ends = line[-1] in ".!?:" or not following[:1].isupper()
        text += f"{line}{'' if ends else '.'} "
    text = text.strip()
    if text and text[-1] not in ".!?:…":
        text += "."
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


_REFERENCE = re.compile(r"\bNotice\s*(?:No\.?|ID|number)?\s*(\d{5,7})\b", re.IGNORECASE)


def worth_showing(notices: Iterable[Notice], home: str) -> list[tuple[Notice, str]]:
    """The notices the dashboard lists, oldest first, each with how it's marked.

    A notice that refers back to one already shown is shown too, so "SERVICE RESUMPTION"
    follows "SERVICE INTERRUPTION - NSW SCADA" even though it's only a general notice.
    """
    out: list[tuple[Notice, str]] = []
    shown: set[int] = set()
    for notice in sorted(notices, key=lambda n: n.id):
        mark = notice_level(notice, home)
        if not mark and any(int(ref) in shown for ref in _REFERENCE.findall(notice.body)):
            mark = "info"
        if mark:
            out.append((notice, mark))
            shown.add(notice.id)
    return out


# --- Demand and prices ---


@dataclass(frozen=True)
class RegionNow:
    id: str
    at: float  # the end of the 5-minute interval these figures are for
    demand: float | None  # MW drawn from the grid
    price: float | None  # $/MWh
    suspended: bool
    apc: bool


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_summary(data: Any) -> tuple[dict[str, RegionNow], list[Notice]]:
    try:
        rows = data["ELEC_NEM_SUMMARY"]
        raw_notices = data.get("ELEC_NEM_SUMMARY_MARKET_NOTICE") or []
    except (TypeError, KeyError) as exc:
        raise SourceError("AEMO's summary is missing its regions") from exc
    regions = {}
    for row in rows:
        try:
            region = RegionNow(
                id=row["REGIONID"],
                at=market_time(row["SETTLEMENTDATE"]),
                demand=_number(row.get("TOTALDEMAND")),
                price=_number(row.get("PRICE")),
                suspended=_number(row.get("MARKETSUSPENDEDFLAG")) == 1,
                apc=_number(row.get("APCFLAG")) == 1,
            )
        except (KeyError, TypeError, ValueError):
            continue
        regions[region.id] = region
    notices = []
    for raw in raw_notices:
        try:
            notices.append(parse_summary_notice(raw))
        except (KeyError, TypeError, ValueError):
            continue
    return regions, notices


def parse_history(data: Any, region: str) -> dict[float, tuple[float, float | None]]:
    """AEMO's 5-minute figures for one region: {interval end: (demand MW, price $/MWh)}."""
    out = {}
    for row in (data or {}).get("5MIN") or []:
        if row.get("REGIONID") != region or row.get("PERIODTYPE", "ACTUAL") != "ACTUAL":
            continue
        demand = _number(row.get("TOTALDEMAND"))
        if demand is None:
            continue
        try:
            out[market_time(row["SETTLEMENTDATE"])] = (demand, _number(row.get("RRP")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class Drop:
    at: float  # the interval it was spotted at
    before: float  # the highest demand in the half hour before, MW
    after: float  # demand over the 10 minutes since, MW (the higher of two readings)

    @property
    def share(self) -> float:
        return 1 - self.after / self.before


def find_drop(history: dict[float, tuple[float, float | None]], s: AemoSettings, now: float) -> Drop | None:
    """The biggest sudden fall in demand in the last drop_hold_minutes, if any was big enough.

    Two readings in a row (10 minutes) must both be at least demand_drop below the highest
    reading in the half hour before them, so one odd reading can't set it off. On real
    data, clouds passing over rooftop solar move NSW demand by up to about 13% this way;
    the tower collapse in Victoria on 13 February 2024 moved it by 26%.
    """
    best = None
    for at in sorted(t for t in history if now - s.drop_hold_minutes * 60 <= t <= now + STEP):
        latest = [history.get(at - k * STEP) for k in range(2)]
        before = [history.get(at - k * STEP) for k in range(2, 8)]
        if None in latest or sum(b is not None for b in before) < 4:
            continue  # not enough readings to judge
        peak = max(b[0] for b in before if b)
        after = max(r[0] for r in latest)
        if peak < s.demand_min_mw:
            continue
        drop = Drop(at, peak, after)
        if drop.share >= s.demand_drop and (best is None or drop.share > best.share):
            best = drop
    return best


def chart(history: dict[float, tuple[float, float | None]], latest: float, hours: float = 24) -> dict[str, Any] | None:
    """Demand over the last day, with the same time the day before, at 5-minute steps."""
    n = int(hours * 3600 / STEP)
    start = latest - (n - 1) * STEP
    today = [history.get(start + i * STEP) for i in range(n)]
    if sum(v is not None for v in today) < 2:
        return None
    yesterday = [history.get(start + i * STEP - 86400) for i in range(n)]
    return {
        "from": start,
        "step": STEP,
        "today": [round(v[0]) if v else None for v in today],
        "yesterday": [round(v[0]) if v else None for v in yesterday],
        "price": [round(v[1], 2) if v and v[1] is not None else None for v in today],
    }


# --- Judging ---


def mw(value: float) -> str:
    return f"{value:,.0f} MW"


def dollars(value: float) -> str:
    return f"{'−' if value < 0 else ''}${abs(value):,.0f}/MWh"


def _ago(now: float, then: float) -> str:
    return f"{format_duration(now - then)} ago"


def _places(regions: list[str]) -> str:
    names = [REGIONS[r].name for r in regions]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _describe(group: list[Condition], now: float) -> str:
    """One sentence for a condition, naming every region it applies to."""
    first = group[0]
    where = _places([c.region for c in group])
    when = f" {_ago(now, first.since)}" if first.notice else ""
    name = first.name
    if name in ("lor1", "lor2", "lor3"):
        return f"AEMO declared an actual {LABELS[name][0]} in {where}{when}: {LABELS[name][1]}"
    if name == "load_shed":
        return f"AEMO directed load shedding in {where}{when}"
    if name == "suspension":
        return f"AEMO suspended the electricity market in {where}{when}" if first.notice             else f"The electricity market is suspended in {where}"
    if name == "apc":
        return f"Prices in {where} are capped after a long run of extreme ones"
    if name == "significant_event":
        found = first.notice and re.search(r"Description of event\s*[-:]\s*(.+)", first.notice.body)
        what = f": {found.group(1).strip().rstrip('.')}" if found else ""
        return f"AEMO reported a significant power system event in {where}{when}{what}"
    return f"AEMO forecasts a possible LOR3 (a supply shortfall) in {where}; nothing has happened yet"


def _sentences(conditions: list[Condition], now: float) -> list[str]:
    groups: dict[str, list[Condition]] = {}
    for c in conditions:
        groups.setdefault(c.name, []).append(c)
    return [_describe(group, now) for group in groups.values()]


LEVEL_ORDER = {Level.CRITICAL.value: 0, Level.ELEVATED.value: 1, "watch": 2, "info": 3}


def assess(
    regions: dict[str, RegionNow],
    notices: list[Notice],
    history: dict[float, tuple[float, float | None]],
    s: AemoSettings,
    now: float,
) -> Assessment:
    home = s.region
    home_name = REGIONS[home].name
    current = regions.get(home)
    if current is None:
        raise SourceError(f"AEMO's summary has no figures for {home_name}")
    if now - current.at > s.max_age_minutes * 60:
        raise SourceError(f"AEMO's figures for {home_name} stopped updating {_ago(now, current.at)}")

    conditions = active_conditions(notices, now)
    # The market flags are AEMO's live state, so they count even without a notice.
    for region in regions.values():
        flags = [("suspension", region.suspended), ("apc", region.apc and region.id == home)]
        for name, flagged in flags:
            if flagged and region.id in REGIONS and not any(c.name == name and c.region == region.id for c in conditions):
                conditions.append(Condition(name, region.id, region.at, None))
    for c in conditions:
        c.level = condition_level(c.name, c.region == home)
    conditions.sort(key=lambda c: (LEVEL_ORDER[c.level], c.region != home, -c.since))
    counted = [c for c in conditions if c.level in (Level.CRITICAL.value, Level.ELEVATED.value)]
    drop = find_drop(history, s, now)

    reasons = _sentences(counted, now)
    if drop:
        reasons.append(
            f"{home_name} demand fell {drop.share:.0%} in half an hour ({mw(drop.before)} to {mw(drop.after)}, "
            f"{_ago(now, drop.at)}), which usually means a large area has lost power"
        )
    if any(c.level == Level.CRITICAL.value for c in counted):
        level = Level.CRITICAL
    elif reasons:
        level = Level.ELEVATED
    else:
        level = Level.NORMAL
        figures = [f"demand {mw(current.demand)}" if current.demand is not None else None,
                   f"price {dollars(current.price)}" if current.price is not None else None]
        figures = [f for f in figures if f]
        reason = f"{home_name} grid normal" + (f" ({', '.join(figures)})" if figures else "")
        watch = [c for c in conditions if c.region == home]
        reasons = [reason + ("; " + "; ".join(_sentences(watch, now)) if watch else "; no supply warnings from AEMO")]

    recent = [n for n in notices if now - n.time <= s.notice_hours * 3600 and n.time <= now + 600]
    shown = [
        {
            **asdict(n), "level": mark, "summary": notice_summary(n),
            "label": notice_label(n) if notice_level(n, home) else "Follow-up",
            "region_names": [REGIONS[r].name for r in n.regions],
        }
        for n, mark in reversed(worth_showing(recent, home))
    ]
    details = {
        "region": home,
        "region_name": home_name,
        "now": asdict(current),
        "regions": [
            {**asdict(r), "name": REGIONS[r.id].name}
            for r in sorted(regions.values(), key=lambda r: (r.id != home, r.id)) if r.id in REGIONS
        ],
        "conditions": [
            {
                "name": c.name, "label": LABELS[c.name][0], "meaning": LABELS[c.name][1],
                "region": c.region, "region_name": REGIONS[c.region].name, "home": c.region == home,
                "since": c.since if c.notice else None, "level": c.level,
                "notice_id": c.notice.id if c.notice else None,
            }
            for c in conditions
        ],
        "notices": shown[: s.show_notices],
        "notices_shown_of": len(shown),
        "notices_total": len(recent),
        "notice_hours": s.notice_hours,
        "drop": {**asdict(drop), "share": drop.share} if drop else None,
        "demand_drop": s.demand_drop,
        "chart": chart(history, current.at),
    }
    return Assessment(level, "; ".join(reasons), details)


# --- The adapter ---


class AemoAdapter(PollingAdapter):
    id = "aemo"
    name = "AEMO electricity grid"
    category = Category.OFFICIAL
    link = "https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/data-nem/data-dashboard-nem"
    Settings = AemoSettings
    settings: AemoSettings

    # Seconds to wait for the two-day history, which is large (about 700 kB).
    history_timeout_seconds = 45
    # Fetch at most this many NEMWeb notice files at once.
    backfill_concurrency = 4

    def __init__(self, ctx: AdapterContext, settings: AemoSettings) -> None:
        super().__init__(ctx, settings)
        self._notices: dict[int, Notice] = {}
        self._history: dict[float, tuple[float, float | None]] = {}
        self._need_backfill = True
        self._backfill_tried_at = float("-inf")
        self._history_tried_at = float("-inf")

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        s = self.settings
        regions, notices = parse_summary(await get_json(self.ctx.http, s.summary_url, "AEMO"))

        # The summary only carries the 10 latest notices. If sitrep knows none of them,
        # it may have missed some, so it reads the rest of the last day from NEMWeb.
        if notices and not any(n.id in self._notices for n in notices):
            self._need_backfill = True
        for n in notices:
            self._notices[n.id] = n
        if self._need_backfill and now - self._backfill_tried_at >= 600:
            self._backfill_tried_at = now
            await self._backfill_notices(now)

        current = regions.get(s.region)
        if current and current.demand is not None:
            if self._history_gap(current.at) and now - self._history_tried_at >= 3600:
                self._history_tried_at = now
                await self._fill_history()
            self._history[current.at] = (current.demand, current.price)

        self._forget(now)
        assessment = assess(regions, list(self._notices.values()), self._history, s, now)
        self._add_to_timeline(assessment, now)
        return assessment

    def _history_gap(self, latest: float) -> bool:
        """True if sitrep has no figures for the half hour before the latest interval."""
        return not any(latest - 1800 <= t < latest for t in self._history)

    async def _fill_history(self) -> None:
        try:
            response = await self.ctx.http.post(
                self.settings.history_url, json={"timeScale": ["5MIN"]},
                headers={"Accept": "application/json"}, timeout=self.history_timeout_seconds,
            )
            response.raise_for_status()
            self._history.update(parse_history(response.json(), self.settings.region))
        except Exception as exc:  # the chart can build up from the summary instead
            self.log.warning("Couldn't fetch AEMO's demand history (%s); the chart will fill in over time",
                             describe_error(exc))

    async def _backfill_notices(self, now: float) -> None:
        s = self.settings
        try:
            response = await self.ctx.http.get(s.notices_url)
            response.raise_for_status()
            first_day = market_day(now - s.notice_hours * 3600)
            wanted = [
                f for f in parse_listing(response.text, s.notices_url)
                if f.id not in self._notices and f.day >= first_day
            ]
            wanted = sorted(wanted, key=lambda f: f.id)[-s.backfill_max_notices:]
            limit = asyncio.Semaphore(self.backfill_concurrency)

            async def fetch(f: ListedNotice) -> Notice:
                async with limit:
                    r = await self.ctx.http.get(f.url)
                    r.raise_for_status()
                    return parse_notice_file(r.text)

            results = await asyncio.gather(*(fetch(f) for f in wanted), return_exceptions=True)
        except Exception as exc:
            self.log.warning("Couldn't read AEMO's notices from NEMWeb (%s); will try again in 10 minutes", exc)
            return
        failed = [r for r in results if isinstance(r, BaseException)]
        for r in results:
            if isinstance(r, Notice):
                self._notices[r.id] = r
        if failed:
            self.log.warning("Couldn't read %d of %d notices from NEMWeb; will try again in 10 minutes",
                             len(failed), len(results))
        else:
            self._need_backfill = False

    def _forget(self, now: float) -> None:
        """Keep two days of figures and the notices that can still matter."""
        keep_notices = max(max(CONDITION_HOURS.values()), self.settings.notice_hours, 7 * 24) * 3600
        self._notices = {i: n for i, n in self._notices.items() if now - n.time <= keep_notices}
        self._history = {t: v for t, v in self._history.items() if now - t <= 49 * 3600}

    def _add_to_timeline(self, assessment: Assessment, now: float) -> None:
        """Put each notice worth showing on the timeline once, at the time AEMO issued it."""
        week = [n for n in self._notices.values() if now - n.time <= 7 * 86400]
        for n, level in worth_showing(week, self.settings.region):
            self.ctx.db.add_event(
                n.time, self.id, self.category.value, level, f"AEMO: {n.title}",
                details={"reason": notice_summary(n), "notice_id": n.id},
                dedupe_key=f"aemo:{n.id}",
            )
        drop = assessment.details.get("drop")
        if drop:
            name = REGIONS[self.settings.region].name
            self.ctx.db.add_event(
                drop["at"], self.id, self.category.value, Level.ELEVATED.value,
                f"{name} electricity demand fell {drop['share']:.0%} in half an hour",
                details={"reason": f"{mw(drop['before'])} to {mw(drop['after'])}"},
                dedupe_key=f"aemo:drop:{int(drop['at'])}",
            )
