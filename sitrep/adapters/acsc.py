"""ASD's ACSC: the Australian Signals Directorate's Australian Cyber Security Centre.

Every 10 minutes sitrep checks ACSC's two RSS feeds (alerts and advisories). It
asks "has it changed?" first (ETag / Last-Modified), so most checks cost a few
hundred bytes.

How it's judged (the word lists are in config.yaml):

- elevated: an item published in the last 24 hours whose title or summary
  mentions active exploitation, critical infrastructure, telecommunications or
  widespread impact;
- never critical by itself: ACSC titles many routine items "Critical
  vulnerabilities in ...", so the word "critical" alone means little.

cyber.gov.au silently ignores requests whose User-Agent contains "bot", so this
source names itself "sitrep/<version> (...; RSS reader)" instead of "sitrep-bot".
That's still an honest description of what it is.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from typing import Any

from defusedxml import ElementTree
from pydantic import Field

from .. import __version__
from ..config import HttpSettings, Settings
from ..models import Assessment, Category, Level
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, retry_after_seconds


class Feed(Settings):
    name: str  # what one item is called: "Alert", "Advisory"
    url: str


DEFAULT_FEEDS = [
    {"name": "Alert", "url": "https://www.cyber.gov.au/rss/alerts"},
    {"name": "Advisory", "url": "https://www.cyber.gov.au/rss/advisories"},
]

# Phrases are matched case-insensitively from the start of a word, so
# "telecommunication" also matches "telecommunications".
DEFAULT_WARNING_SIGNS = {
    "active exploitation": [
        "active exploitation", "actively exploited", "actively being exploited", "being exploited",
        "exploited in the wild", "exploitation in the wild", "ongoing exploitation",
    ],
    "critical infrastructure": [
        "critical infrastructure", "operational technology", "essential service", "industrial control",
        "SCADA", "energy sector", "electricity", "water utilit",
    ],
    "telecommunications": [
        "telecommunication", "telco", "mobile network", "internet service provider", "NBN",
        "Telstra", "Optus", "TPG", "Vodafone",
    ],
    "widespread impact": [
        "widespread", "nationwide", "across Australia", "many Australian organisations",
        "multiple Australian organisations", "significant impact",
    ],
}


class AcscSettings(PollingSettings):
    interval_seconds: float = 600
    feeds: list[Feed] = Field(default_factory=lambda: [Feed(**f) for f in DEFAULT_FEEDS], min_length=1)
    # Items published this recently count towards the level.
    recent_hours: float = 24
    warning_signs: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_WARNING_SIGNS))
    # How many of the latest items the dashboard lists.
    show_items: int = Field(8, ge=1)


def reader_user_agent(http: HttpSettings) -> str:
    about = "self-hosted situational-awareness dashboard; RSS reader"
    if http.contact.strip():
        about += f"; {http.contact.strip()}"
    return f"sitrep/{__version__} ({about})"


@dataclass(frozen=True)
class Item:
    kind: str  # "Alert" or "Advisory"
    title: str
    link: str | None
    summary: str
    published: float
    guid: str
    signs: tuple[str, ...] = ()  # which warning signs it mentions


def _clean(text: str | None) -> str:
    """Plain text: tags removed, entities decoded, whitespace tidied."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def compile_signs(signs: dict[str, list[str]]) -> dict[str, re.Pattern[str]]:
    return {
        name: re.compile(r"\b(?:" + "|".join(re.escape(p) for p in phrases) + ")", re.IGNORECASE)
        for name, phrases in signs.items()
        if phrases
    }


def parse_feed(data: bytes, kind: str, signs: dict[str, re.Pattern[str]]) -> list[Item]:
    try:
        root = ElementTree.fromstring(data)
    except Exception as exc:  # defusedxml raises its own errors for unsafe XML, plus ParseError
        raise SourceError(f"ACSC's {kind.lower()} feed isn't valid RSS ({type(exc).__name__})") from exc
    channel = root.find("channel")
    if channel is None:
        raise SourceError(f"ACSC's {kind.lower()} feed has no channel in it")
    items = []
    for node in channel.findall("item"):
        title = _clean(node.findtext("title"))
        date = node.findtext("pubDate")
        if not title or not date:
            continue  # an item without a title or date can't be shown or judged
        try:
            published = parsedate_to_datetime(date.strip()).timestamp()
        except (TypeError, ValueError):
            continue
        summary = _clean(node.findtext("description"))
        link = (node.findtext("link") or "").strip() or None
        text = f"{title} {summary}"
        items.append(Item(
            kind=kind,
            title=title,
            link=link,
            summary=summary,
            published=published,
            guid=(node.findtext("guid") or link or title).strip(),
            signs=tuple(name for name, pattern in signs.items() if pattern.search(text)),
        ))
    return items


def _quoted(item: Item) -> str:
    return f"“{item.title}”"


def assess(items: list[Item], s: AcscSettings, now: float) -> Assessment:
    # Items dated in the future don't exist yet (allowing ten minutes for clocks that disagree).
    latest = sorted((i for i in items if i.published <= now + 600), key=lambda i: i.published, reverse=True)
    recent = [i for i in latest if now - i.published <= s.recent_hours * 3600]
    flagged = [i for i in recent if i.signs]
    window = format_duration(s.recent_hours * 3600)

    if flagged:
        level = Level.ELEVATED
        reason = "; ".join(
            f"New ACSC {i.kind.lower()}: {_quoted(i)} ({format_duration(now - i.published)} ago; "
            f"mentions {', '.join(i.signs)})"
            for i in flagged
        )
    elif recent:
        level = Level.NORMAL
        newest = recent[0]
        reason = (
            f"New ACSC {newest.kind.lower()} {format_duration(now - newest.published)} ago, "
            f"with no warning signs: {_quoted(newest)}"
        )
    elif latest:
        level = Level.NORMAL
        newest = latest[0]
        reason = (
            f"Nothing new in the last {window}. Latest: {_quoted(newest)} "
            f"({format_duration(now - newest.published)} ago)"
        )
    else:
        level, reason = Level.NORMAL, "ACSC's feeds are empty"

    details = {
        "items": [
            {**asdict(i), "signs": list(i.signs), "recent": now - i.published <= s.recent_hours * 3600}
            for i in latest[: s.show_items]
        ],
        "recent_hours": s.recent_hours,
        "warning_signs": list(s.warning_signs),
    }
    return Assessment(level, reason, details)


@dataclass
class _Cached:
    etag: str | None
    last_modified: str | None
    items: list[Item]


class AcscAdapter(PollingAdapter):
    id = "acsc"
    name = "ACSC alerts and advisories"
    category = Category.OFFICIAL
    link = "https://www.cyber.gov.au/about-us/view-all-content/alerts-and-advisories"
    Settings = AcscSettings
    settings: AcscSettings

    def __init__(self, ctx: AdapterContext, settings: AcscSettings) -> None:
        super().__init__(ctx, settings)
        self._signs = compile_signs(settings.warning_signs)
        self._agent = reader_user_agent(ctx.config.http)
        self._cache: dict[str, _Cached] = {}

    async def fetch(self, feed: Feed) -> list[Item]:
        cached = self._cache.get(feed.url)
        headers = {"User-Agent": self._agent}
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        response = await self.ctx.http.get(feed.url, headers=headers)
        if response.status_code == 304 and cached:
            return cached.items  # unchanged since last time
        if response.status_code == 429:
            raise SourceError("ACSC asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        response.raise_for_status()
        items = parse_feed(response.content, feed.name, self._signs)
        self._cache[feed.url] = _Cached(response.headers.get("ETag"), response.headers.get("Last-Modified"), items)
        return items

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        answers = await asyncio.gather(*(self.fetch(feed) for feed in self.settings.feeds))
        items = [item for feed_items in answers for item in feed_items]
        self._add_to_timeline(items, now)
        return assess(items, self.settings, now)

    def _add_to_timeline(self, items: list[Item], now: float) -> None:
        """Put each item from the last week on the timeline, once, at the time ACSC published it."""
        for item in items:
            if now - item.published > 7 * 86400:
                continue
            self.ctx.db.add_event(
                item.published, self.id, self.category.value,
                Level.ELEVATED.value if item.signs else "info",
                f"ACSC {item.kind.lower()}: {item.title}",
                url=item.link,
                details={"reason": item.summary, "signs": list(item.signs)},
                dedupe_key=f"acsc:{item.guid}",
            )
