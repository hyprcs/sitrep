"""Foreign governments' travel advice about Australia.

Every hour sitrep reads two governments' advice for their citizens travelling here:

- the US State Department's travel advisory for Australia, from Level 1 ("exercise
  normal precautions") to Level 4 ("do not travel"), from cadataapi.state.gov;
- the UK FCDO's travel advice for Australia, from GOV.UK's content API (Open Government
  Licence v3.0). It says whether the FCDO advises against travel to part or all of
  Australia, and lists every update to the advice with a short note.

Foreign governments rarely change their advice about Australia, so a change means a lot:

- critical: either one raises its level (the US from Level 1 to Level 2, or the FCDO
  starting to advise against travel to part of Australia), for 24 hours after sitrep
  notices;
- elevated: an FCDO update from the last 24 hours whose note mentions terrorism,
  security, safety, civil unrest or the like. The FCDO updates its pages often for small
  reasons (insurance wording, the bushfire season), and those are info. The note's
  mention of its own "Safety and security" page doesn't count. A note that names a
  Sydney place is about that place, so it can back up, or join a story about, the same
  place;
- info: a lowered level, a reissue at the same level, and every other FCDO update.

The US Embassy in Canberra's security alerts are only a link on the tile: its website
refuses automated requests (HTTP 403) and has no feed.

Both are quiet by nature, so the source is never "frozen"; it only shows a problem
when fetching fails.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from ..geo import cell_of
from ..models import SYDNEY, Assessment, Category, Level, Signal, area, national
from ..places import gazetteer
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, describe_error, retry_after_seconds

US_NAME = "US State Department"
UK_NAME = "UK FCDO"
UK_PAGE = "https://www.gov.uk/foreign-travel-advice/australia"
EMBASSY = "https://au.usembassy.gov/"

# The FCDO's alert statuses, from least to most serious.
UK_STATUSES = {
    "avoid_all_but_essential_travel_to_parts": (1, "advises against all but essential travel to parts of Australia"),
    "avoid_all_but_essential_travel_to_whole_country": (2, "advises against all but essential travel to Australia"),
    "avoid_all_travel_to_parts": (3, "advises against all travel to parts of Australia"),
    "avoid_all_travel_to_whole_country": (4, "advises against all travel to Australia"),
}
UK_NONE = "doesn't advise against travel to any part of Australia"

# Words that make an FCDO update worth a look. They match the start of a word, ignoring
# case, so "terror" also matches "terrorist" and "terrorism".
DEFAULT_WARNING_WORDS = [
    "terror", "security", "safety", "civil unrest", "unrest", "riot", "protest", "shooting",
    "attack", "explosion", "hostage",
]

LEVEL = re.compile(r"Level (\d)\s*:\s*(.+)$")


class AdviceSettings(PollingSettings):
    interval_seconds: float = 3600
    us_url: str = "https://cadataapi.state.gov/api/TravelAdvisories"
    # The State Department's code for Australia. It's "AS", not "AU".
    us_country: str = "AS"
    uk_url: str = "https://www.gov.uk/api/content/foreign-travel-advice/australia"
    # A raised level is critical, and a worrying FCDO update elevated, for this many hours.
    recent_hours: float = Field(24, gt=0)
    warning_words: list[str] = Field(default_factory=lambda: list(DEFAULT_WARNING_WORDS))
    # How many of the FCDO's latest updates the dashboard lists.
    show_updates: int = Field(5, ge=1)


@dataclass(frozen=True)
class UsAdvisory:
    level: int
    label: str  # "Exercise Normal Precautions"
    link: str | None
    updated: float | None
    issued: str | None  # its date as the State Department gives it: "17 Aug 2026"
    feed_at: float | None  # the newest advisory for any country, which shows the feed is alive


@dataclass(frozen=True)
class UkUpdate:
    note: str
    at: float
    warnings: tuple[str, ...] = ()  # the warning words it mentions
    places: tuple[str, ...] = ()
    lat: float | None = None
    lon: float | None = None
    suburb: str | None = None
    sydney_wide: bool = False

    @property
    def scope(self) -> str:
        if self.lat is not None and self.lon is not None:
            return area(*cell_of(self.lat, self.lon))
        return SYDNEY if self.sydney_wide else national("security")


@dataclass(frozen=True)
class UkAdvice:
    statuses: tuple[str, ...]
    rank: int  # 0 (no advice against travel) to 4 (against all travel to Australia)
    updated: float | None  # the FCDO's own "latest update" time
    feed_at: float | None  # when GOV.UK last republished the page, for any reason
    updates: tuple[UkUpdate, ...]  # newest first

    @property
    def label(self) -> str:
        return uk_label(self.statuses)


@dataclass(frozen=True)
class Change:
    """A government moving its level, as sitrep noticed it."""

    who: str  # "us" or "uk"
    old: int
    new: int
    at: float  # when sitrep noticed
    label: str  # what the new level says

    @property
    def raised(self) -> bool:
        return self.new > self.old

    @property
    def item_id(self) -> str:
        return f"{self.who}:level:{self.old}-{self.new}:{int(self.at)}"

    def describe(self) -> str:
        if self.who == "us":
            verb = "raised" if self.raised else "lowered"
            return (f"The US {verb} its travel advisory for Australia from Level {self.old} to "
                    f"Level {self.new}: {self.label}")
        if self.raised:
            return f"The UK FCDO now {self.label}"
        return f"The UK FCDO eased its advice: it now {self.label}"


def uk_label(statuses: tuple[str, ...]) -> str:
    if not statuses:
        return UK_NONE
    worst = max(statuses, key=lambda s: UK_STATUSES.get(s, (1, s))[0])
    return UK_STATUSES.get(worst, (1, f"has an alert status sitrep doesn't know: {worst}"))[1]


def _clean(text: str | None) -> str:
    """Plain text: tags removed, entities decoded, whitespace tidied."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _time(text: Any) -> float | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def _date(text: Any) -> str | None:
    """The date in a timestamp, in the timestamp's own time zone: "17 Aug 2026"."""
    try:
        when = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return f"{when.day} {when:%b %Y}"


def compile_words(words: list[str]) -> re.Pattern[str] | None:
    words = [w for w in words if w.strip()]
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(w.strip()) for w in words) + r")\w*", re.IGNORECASE)


def parse_us(data: Any, country: str) -> UsAdvisory:
    if not isinstance(data, list):
        raise SourceError("The State Department's advisories aren't a list")
    ours = next((x for x in data if isinstance(x, dict) and country in (x.get("Category") or [])), None)
    if ours is None:
        raise SourceError(f"The State Department's advisories have no entry for Australia ({country})")
    match = LEVEL.search(_clean(ours.get("Title")))
    if not match:
        raise SourceError(f"Couldn't find a level in the US advisory “{_clean(ours.get('Title'))}”")
    newest = max(filter(None, (_time(x.get("Updated")) for x in data if isinstance(x, dict))), default=None)
    return UsAdvisory(int(match.group(1)), match.group(2).strip(), ours.get("Link") or None,
                      _time(ours.get("Updated")), _date(ours.get("Updated")), newest)


def uk_update(note: str, at: float, pages: list[str], warnings: re.Pattern[str] | None) -> UkUpdate:
    # The FCDO names the page it changed ("'Safety and security' page"): that isn't news.
    plain = note
    for page in pages:
        plain = re.sub(re.escape(page), " ", plain, flags=re.IGNORECASE)
    found = tuple(dict.fromkeys(m.group(0).lower() for m in warnings.finditer(plain))) if warnings else ()
    places = gazetteer()
    named = places.find(note)
    local = next((p for p in named if p.sydney), None)
    return UkUpdate(
        note, at, found, tuple(p.name for p in named), local.lat if local else None, local.lon if local else None,
        local.name if local else None, places.sydney_wide(note),
    )


def parse_uk(doc: Any, warnings: re.Pattern[str] | None) -> UkAdvice:
    details = doc.get("details") if isinstance(doc, dict) else None
    if not isinstance(details, dict) or "alert_status" not in details:
        raise SourceError("GOV.UK's travel advice for Australia has no alert status in it")
    statuses = tuple(sorted({str(s) for s in details.get("alert_status") or []}))
    rank = max((UK_STATUSES.get(s, (1, s))[0] for s in statuses), default=0)
    pages = [p["title"] for p in details.get("parts") or [] if isinstance(p, dict) and p.get("title")]
    pages.append("Summary")
    updates = []
    for entry in details.get("change_history") or []:
        if not isinstance(entry, dict):
            continue
        at, note = _time(entry.get("public_timestamp")), _clean(entry.get("note"))
        if at is not None and note:
            updates.append(uk_update(note, at, pages, warnings))
    return UkAdvice(statuses, rank, _time(doc.get("public_updated_at")), _time(doc.get("updated_at")),
                    tuple(sorted(updates, key=lambda u: u.at, reverse=True)))


def _when(ts: float, now: float) -> str:
    """'3 hours ago' for the last week, then the date: 'on 19 Mar 2026'."""
    if now - ts <= 7 * 86400:
        return f"{format_duration(max(0.0, now - ts))} ago"
    when = datetime.fromtimestamp(ts, timezone.utc)
    return f"on {when.day} {when:%b %Y}"


def _quoted(update: UkUpdate) -> str:
    return f"“{update.note}”"


def assess(
    us: UsAdvisory | None, uk: UkAdvice | None, changes: list[Change], s: AdviceSettings, now: float,
    alertable: dict[str, bool] | None = None,
) -> Assessment:
    alertable = alertable or {}
    window = s.recent_hours * 3600
    raised = [c for c in changes if c.raised and now - c.at <= window]
    eased = [c for c in changes if not c.raised and now - c.at <= window]
    worrying = [u for u in (uk.updates if uk else ()) if u.warnings and -600 <= now - u.at <= window]

    signals = tuple(
        Signal(f"advice:{c.who}:level{c.new}", Level.CRITICAL, (national("security"),), title=c.describe(),
               alertable=alertable.get(c.item_id, True), at=c.at)
        for c in raised
    ) + tuple(
        Signal(f"advice:uk:{int(u.at)}", Level.ELEVATED, (u.scope,), title=f"UK travel advice update: {_quoted(u)}",
               alertable=alertable.get(f"uk:{int(u.at)}", True), at=u.at)
        for u in worrying
    )

    standing = []
    if us:
        standing.append(f"US: Level {us.level}, {us.label.lower()}" + (f" (issued {us.issued})" if us.issued else ""))
    if uk:
        standing.append(f"UK: {uk.label}")
    if raised:
        level = Level.CRITICAL
        reason = "; ".join(f"{c.describe()} ({format_duration(now - c.at)} ago)" for c in raised)
    elif worrying:
        level = Level.ELEVATED
        reason = "; ".join(
            f"New UK travel advice update ({format_duration(max(0, now - u.at))} ago; mentions "
            f"{', '.join(u.warnings)}): {_quoted(u)}"
            for u in worrying
        )
    else:
        level = Level.NORMAL
        reason = ". ".join(
            [f"{c.describe()} ({format_duration(now - c.at)} ago)" for c in eased] + ["; ".join(standing)]
        ) or "No advice read yet"
        if uk and uk.updates and not eased:
            reason += f". Latest UK update {_when(uk.updates[0].at, now)}"

    details = {
        "us": {**asdict(us), "label": us.label} if us else None,
        "uk": {"rank": uk.rank, "statuses": list(uk.statuses), "label": uk.label, "updated": uk.updated,
               "link": UK_PAGE} if uk else None,
        "changes": [{**asdict(c), "text": c.describe(), "raised": c.raised, "counts": now - c.at <= window}
                    for c in changes],
        "updates": [{"note": u.note, "at": u.at, "warnings": list(u.warnings), "places": list(u.places),
                     "recent": now - u.at <= window}
                    for u in (uk.updates[: s.show_updates] if uk else ())],
        "embassy": EMBASSY,
        "recent_hours": s.recent_hours,
    }
    dates = [t for t in ((us.updated if us else None), (uk.updated if uk else None)) if t is not None]
    return Assessment(level, reason, details, content_at=max(dates, default=None), signals=signals)


class AdviceAdapter(PollingAdapter):
    id = "advice"
    name = "Foreign travel advice"
    category = Category.OFFICIAL
    link = UK_PAGE
    Settings = AdviceSettings
    scope = national("security")
    # GOV.UK can take a few minutes to show an update after its own time.
    publish_delay_minutes = 15
    settings: AdviceSettings

    def __init__(self, ctx: AdapterContext, settings: AdviceSettings) -> None:
        super().__init__(ctx, settings)
        self._warnings = compile_words(settings.warning_words)
        self._us: UsAdvisory | None = None
        self._uk: UkAdvice | None = None
        self._uk_etag: str | None = None

    async def fetch_us(self) -> UsAdvisory:
        response = await self.ctx.http.get(self.settings.us_url)
        if response.status_code == 429:
            raise SourceError("it asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 400:
            raise SourceError(f"it answered HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError("it sent something that isn't JSON") from exc
        return parse_us(data, self.settings.us_country)

    async def fetch_uk(self) -> UkAdvice:
        headers = {"If-None-Match": self._uk_etag} if self._uk_etag and self._uk else {}
        response = await self.ctx.http.get(self.settings.uk_url, headers=headers)
        if response.status_code == 304 and self._uk:
            return self._uk  # unchanged since last time
        if response.status_code == 429:
            raise SourceError("it asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 400:
            raise SourceError(f"it answered HTTP {response.status_code}")
        try:
            doc = response.json()
        except ValueError as exc:
            raise SourceError("it sent something that isn't JSON") from exc
        advice = parse_uk(doc, self._warnings)
        self._uk_etag = response.headers.get("ETag")
        return advice

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        us, uk = await asyncio.gather(self.fetch_us(), self.fetch_uk(), return_exceptions=True)
        problems = []
        for name, answer in ((US_NAME, us), (UK_NAME, uk)):
            if isinstance(answer, asyncio.CancelledError):
                raise answer
            if isinstance(answer, BaseException):
                problems.append(f"{name}: {describe_error(answer)}")
        if len(problems) == 2:
            raise SourceError("Couldn't read either government's advice (" + "; ".join(problems)[:200] + ")")

        changes = []
        if isinstance(us, UsAdvisory):
            self._us = us
            changes.append(self._track("us", us.level, us.label, now, link=us.link, updated=us.updated))
        else:
            changes.append(self._stored_change("us"))
        if isinstance(uk, UkAdvice):
            self._uk = uk
            changes.append(self._track("uk", uk.rank, uk.label, now, link=UK_PAGE))
            self._see_updates(uk, now)
        else:
            changes.append(self._stored_change("uk"))
        changes = [c for c in changes if c is not None]

        alertable = {}
        for c in changes:
            if now - c.at <= self.settings.recent_hours * 3600:
                alertable[c.item_id] = self.see_item(
                    c.item_id, None, title=c.describe(), level=Level.CRITICAL.value if c.raised else "info",
                    url=self._us.link if c.who == "us" and self._us else UK_PAGE, now=now,
                ).alertable
        if self._uk:
            for u in self._uk.updates:
                if u.warnings and now - u.at <= self.settings.recent_hours * 3600:
                    alertable[f"uk:{int(u.at)}"] = self._seen_update(u, now)

        result = assess(self._us, self._uk, changes, self.settings, now, alertable)
        reason = result.reason
        if problems:
            reason += ". Couldn't read " + "; ".join(problems)
        result.details["feeds_failing"] = problems
        feeds = [t for t in ((self._us.feed_at if self._us else None), (self._uk.feed_at if self._uk else None)) if t]
        return Assessment(result.level, reason, result.details, content_at=result.content_at,
                          feed_at=max(feeds, default=None), signals=result.signals)

    # --- What sitrep remembers between checks (and restarts) ---

    def _stored(self, who: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.ctx.db.kv_get(f"advice:{who}") or "null")
        except ValueError:
            return None

    def _stored_change(self, who: str) -> Change | None:
        stored = self._stored(who) or {}
        return Change(**stored["change"]) if stored.get("change") else None

    def _track(self, who: str, level: int, label: str, now: float, *, link: str | None,
               updated: float | None = None) -> Change | None:
        """Compare this level with the last one seen, and remember it. Returns the latest change, if any."""
        stored = self._stored(who)
        change = Change(**stored["change"]) if stored and stored.get("change") else None
        if stored is not None and stored.get("level") != level:
            change = Change(who, int(stored["level"]), level, now, label)
            self.ctx.db.add_event(
                now, self.id, self.category.value, Level.CRITICAL.value if change.raised else "info",
                change.describe(), url=link, dedupe_key=f"advice:{change.item_id}",
            )
        elif stored is not None and updated is not None and stored.get("updated") not in (None, updated):
            self.ctx.db.add_event(
                now, self.id, self.category.value, "info",
                f"The US reissued its travel advisory for Australia, still Level {level}: {label}",
                url=link, dedupe_key=f"advice:{who}:reissued:{int(updated)}",
            )
        self.ctx.db.kv_set(f"advice:{who}", json.dumps({
            "level": level, "label": label, "updated": updated, "change": asdict(change) if change else None,
        }))
        return change

    def _see_updates(self, uk: UkAdvice, now: float) -> None:
        """Remember the FCDO's updates from the last week, and put each on the timeline once."""
        for u in uk.updates:
            if now - u.at > 7 * 86400:
                break
            self._seen_update(u, now)
            self.ctx.db.add_event(
                u.at, self.id, self.category.value, Level.ELEVATED.value if u.warnings else "info",
                f"UK travel advice: {u.note}", url=UK_PAGE,
                details={"reason": f"Mentions {', '.join(u.warnings)}" if u.warnings else "A routine update"},
                dedupe_key=f"advice:uk:{int(u.at)}",
            )

    def _seen_update(self, u: UkUpdate, now: float) -> bool:
        cell = cell_of(u.lat, u.lon) if u.lat is not None and u.lon is not None else None
        return self.see_item(
            f"uk:{int(u.at)}", u.at, title=f"UK travel advice: {u.note}",
            level=Level.ELEVATED.value if u.warnings else "info", url=UK_PAGE, now=now,
            details={"places": list(u.places), "lat": u.lat, "lon": u.lon, "suburb": u.suburb,
                     "cell": list(cell) if cell else None, "sydney_wide": u.sydney_wide},
        ).alertable
