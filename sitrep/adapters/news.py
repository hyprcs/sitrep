"""Australian news: headlines from major outlets' RSS feeds, with the places they name.

sitrep reads ABC News (Just In, and NSW), the Sydney Morning Herald (NSW) and Guardian
Australia, each every 10 or 15 minutes, asking "has it changed?" first where the feed
allows. From each item it keeps only the headline, the link, the outlet, the time and any
NSW place the headline names (see places.py). Never the article text: sitrep doesn't
fetch articles at all. Headlines are kept for 7 days.

The outlets' terms allow personal, non-commercial use of their feeds, with a link back,
which the dashboard gives. The Guardian and Nine (SMH) also ban AI and machine-learning
use of their content, so place-finding here is plain rules, and their headlines must be
left out of anything a language model reads (the planned Ollama brief).

News on its own never raises the level. Headlines are the raw material for story
clusters, which group news, official and social items about one event, and for the map.

Items carry their own publication time, so a headline first seen long after it was
published (after an outage) can't set anything off. The Guardian's time is when the story
was last updated, so a story that keeps being updated counts as new again. If no feed has
published anything for 4 hours, they've probably stopped moving on, and the source shows
as frozen.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from defusedxml import ElementTree
from pydantic import Field

from ..config import Settings
from ..geo import cell_of, km_between
from ..models import Assessment, Category, Level
from ..places import gazetteer
from ..terms import blocklist_of, record
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, retry_after_seconds


class NewsFeed(Settings):
    name: str  # the outlet, as the dashboard shows it
    url: str
    every_minutes: float = 15
    # True for a feed that's only about NSW, so a place name in it needs no other clue.
    nsw: bool = False
    # Its terms ban AI use of its content, so a language model must never see its headlines.
    no_ai: bool = False


DEFAULT_FEEDS = [
    {"name": "ABC News", "url": "https://www.abc.net.au/news/feed/10719976/rss.xml", "every_minutes": 10},
    {"name": "ABC News NSW", "url": "https://www.abc.net.au/news/feed/5555986/rss.xml", "nsw": True},
    {"name": "Sydney Morning Herald", "url": "https://www.smh.com.au/rss/national/nsw.xml", "nsw": True, "no_ai": True},
    {"name": "Guardian Australia", "url": "https://www.theguardian.com/australia-news/rss", "no_ai": True},
]
# Tracking bits sites add to links, removed so the same story has the same link everywhere.
TRACKING = re.compile(r"^(utm_\w+|ref|cid|sp)$", re.IGNORECASE)


class NewsSettings(PollingSettings):
    interval_seconds: float = 300  # how often to see which feeds are due
    feeds: list[NewsFeed] = Field(default_factory=lambda: [NewsFeed(**f) for f in DEFAULT_FEEDS], min_length=1)
    # Headlines from this many hours back are shown and counted.
    recent_hours: float = Field(24, gt=0)
    show_headlines: int = Field(12, ge=1)


@dataclass
class Headline:
    outlet: str
    title: str
    link: str
    published: float | None
    places: list[str]  # NSW places it names
    lat: float | None = None  # the first Greater Sydney place it names, if any
    lon: float | None = None
    sydney_wide: bool = False  # it's about Sydney or NSW as a whole
    no_ai: bool = False


def canonical(link: str) -> str:
    """The link without tracking parameters or a fragment."""
    parts = urlsplit(link.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()


def parse_feed(data: bytes, feed: NewsFeed) -> list[Headline]:
    try:
        root = ElementTree.fromstring(data)
    except Exception as exc:  # defusedxml raises its own errors for unsafe XML, plus ParseError
        raise SourceError(f"{feed.name}'s feed isn't valid RSS ({type(exc).__name__})") from exc
    channel = root.find("channel")
    if channel is None:
        raise SourceError(f"{feed.name}'s feed has no channel in it")
    places = gazetteer()
    headlines = []
    for node in channel.findall("item"):
        title, link = _clean(node.findtext("title")), (node.findtext("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        try:
            published = parsedate_to_datetime((node.findtext("pubDate") or "").strip()).timestamp()
        except (TypeError, ValueError):
            published = None
        found = places.find(title, nsw_feed=feed.nsw)
        local = next((p for p in found if p.sydney), None)
        headlines.append(Headline(
            outlet=feed.name, title=title, link=canonical(link), published=published,
            places=[p.name for p in found], lat=local.lat if local else None, lon=local.lon if local else None,
            sydney_wide=places.sydney_wide(title), no_ai=feed.no_ai,
        ))
    return headlines


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def assess(headlines: list[Headline], home: tuple[float, float], s: NewsSettings, now: float) -> Assessment:
    recent = sorted((h for h in headlines if h.published is not None and now - s.recent_hours * 3600 <= h.published
                     <= now + 600), key=lambda h: h.published or 0, reverse=True)
    hour = [h for h in recent if now - (h.published or 0) <= 3600]
    placed = [h for h in recent if h.lat is not None]
    by_outlet: dict[str, int] = {}
    for h in hour:
        by_outlet[h.outlet] = by_outlet.get(h.outlet, 0) + 1
    reason = f"{_plural(len(hour), 'headline')} in the last hour"
    if by_outlet:
        reason += " (" + ", ".join(f"{name} {n}" for name, n in by_outlet.items()) + ")"
    verb = "names" if len(placed) == 1 else "name"
    reason += f"; {_plural(len(placed), 'headline')} in the last {s.recent_hours:g} hours {verb} a Sydney suburb"
    details = {
        "recent_hours": s.recent_hours,
        "headlines": [{**asdict(h), "km": round(km_between(*home, h.lat, h.lon), 1) if h.lat is not None else None}
                      for h in recent[: s.show_headlines]],
        "counts": {"hour": len(hour), "recent": len(recent), "placed": len(placed)},
        "map": [
            {"kind": "news", "id": h.link, "title": h.title, "label": h.outlet, "mark": "info", "lat": h.lat,
             "lon": h.lon, "km": round(km_between(*home, h.lat, h.lon), 1), "at": h.published,
             "detail": ", ".join(h.places)}
            for h in placed if now - (h.published or 0) <= 6 * 3600
        ],
    }
    times = [h.published for h in headlines if h.published is not None and h.published <= now + 600]
    return Assessment(Level.NORMAL, reason, details, content_at=max(times, default=None))


@dataclass
class _Feed:
    etag: str | None = None
    modified: str | None = None
    fetched_at: float = float("-inf")
    headlines: list[Headline] | None = None


class NewsAdapter(PollingAdapter):
    id = "news"
    name = "News headlines"
    category = Category.NEWS
    Settings = NewsSettings
    source_type = "mainstream"
    # Something is published every few minutes in the day; 4 quiet hours means the feeds are stuck.
    content_budget_minutes = 240
    # Feeds are read every 10 to 15 minutes, and can lag a story's time by a few more.
    publish_delay_minutes = 10
    settings: NewsSettings

    def __init__(self, ctx: AdapterContext, settings: NewsSettings) -> None:
        super().__init__(ctx, settings)
        self._feeds = {f.url: _Feed() for f in settings.feeds}

    @property
    def poll_gap(self) -> float:
        return max(f.every_minutes for f in self.settings.feeds) * 60

    async def fetch(self, feed: NewsFeed, now: float) -> list[Headline]:
        state = self._feeds[feed.url]
        if state.headlines is not None and now - state.fetched_at < feed.every_minutes * 60 - 5:
            return state.headlines  # not due yet
        headers = {}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.modified:
            headers["If-Modified-Since"] = state.modified
        response = await self.ctx.http.get(feed.url, headers=headers)
        state.fetched_at = now
        if response.status_code == 304 and state.headlines is not None:
            return state.headlines
        if response.status_code == 429:
            raise SourceError(f"{feed.name} asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 400:
            raise SourceError(f"{feed.name}'s feed answered HTTP {response.status_code}")
        state.headlines = parse_feed(response.content, feed)
        state.etag, state.modified = response.headers.get("ETag"), response.headers.get("Last-Modified")
        return state.headlines

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        answers = await asyncio.gather(*(self.fetch(f, now) for f in self.settings.feeds), return_exceptions=True)
        problems = [f"{f.name}: {a}" for f, a in zip(self.settings.feeds, answers) if isinstance(a, BaseException)]
        if len(problems) == len(answers):
            raise SourceError("No news feed could be read (" + "; ".join(problems)[:200] + ")")
        seen: dict[str, Headline] = {}
        for answer in answers:
            if isinstance(answer, BaseException):
                if isinstance(answer, asyncio.CancelledError):
                    raise answer
                continue
            for h in answer:
                seen.setdefault(h.link, h)  # the same story in two ABC feeds counts once
        headlines = list(seen.values())
        terms = self.ctx.config.terms
        blocklist = blocklist_of(terms)
        for h in headlines:
            if h.published is None or now - h.published > 7 * 86400:
                continue
            item = self.see_item(
                h.link, h.published, title=h.title, url=h.link, now=now,
                details={"outlet": h.outlet, "places": h.places, "lat": h.lat, "lon": h.lon,
                         "cell": list(cell_of(h.lat, h.lon)) if h.lat is not None else None,
                         "sydney_wide": h.sydney_wide, "no_ai": h.no_ai},
            )
            if item.new and terms.enabled and (h.sydney_wide or h.places):
                # Each headline about Sydney or NSW is counted once, for emerging terms.
                record(self.ctx.db, h.title, at=h.published, kind="news", who=h.outlet, fresh=item.alertable,
                       blocklist=blocklist)
        loc = self.ctx.config.home.location
        result = assess(headlines, (loc.lat, loc.lon), self.settings, now)
        reason = result.reason
        if problems:
            reason += ". Couldn't read " + "; ".join(problems)
        result.details["feeds_failing"] = problems
        return Assessment(result.level, reason, result.details, content_at=result.content_at)
