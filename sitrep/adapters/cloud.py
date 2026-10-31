"""Cloud and CDN status for Sydney: AWS, Azure, Google Cloud and Cloudflare.

Every 5 minutes sitrep checks each provider's public status feed for an incident in its
Sydney region:

- AWS ap-southeast-2: the Sydney RSS feeds of a few core services (EC2, S3, Lambda,
  DynamoDB). AWS's site terms rule out "robots" on its website, so sitrep reads only
  these feeds, which exist to be polled, and never the status page itself. A feed's
  newest update counts until it says the service is operating normally again (or for
  12 hours, if it never does);
- Azure Australia East: Azure's status RSS feed, which lists only widespread incidents
  (problems in one region need an Azure login to see). An item counts if it names
  Australia East;
- Google Cloud australia-southeast1: incidents.json. An incident counts while it has no
  end and lists australia-southeast1 among the locations it currently affects;
- Cloudflare's Sydney data centre (SYD): Cloudflare's status API. An unresolved incident
  counts if it includes the Sydney location or names Sydney. Cloudflare's per-location
  statuses change all the time without an incident, so they're ignored; maintenance in
  Sydney is shown, but doesn't count.

Any active incident in those regions is elevated, and the reason names it. It's never
critical by itself. It's about the internet in Australia, so it backs up other signs of
internet trouble: the case this covers is many sites broken while your line and the
telcos are fine.

Every request asks "has it changed?" first, so most checks cost a few hundred bytes.
The feeds are quiet by nature (weeks can pass without an incident), so the source is
never "frozen"; it only shows a problem when fetching fails.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from defusedxml import ElementTree
from pydantic import Field

from ..models import Assessment, Category, Level, Signal, national
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, describe_error, retry_after_seconds

AWS, AZURE, GCP, CLOUDFLARE = "AWS", "Azure", "Google Cloud", "Cloudflare"
PAGES = {
    AWS: "https://health.aws.amazon.com/health/status",
    AZURE: "https://azure.status.microsoft/en-us/status",
    GCP: "https://status.cloud.google.com/",
    CLOUDFLARE: "https://www.cloudflarestatus.com/",
}
# An AWS update that means its event is over.
AWS_RESOLVED = re.compile(r"^Service is operating normally|\[RESOLVED\]", re.IGNORECASE)


class CloudSettings(PollingSettings):
    interval_seconds: float = 300
    # AWS: which services' Sydney feeds to read. Each is one small request per check.
    aws_services: list[str] = Field(default_factory=lambda: ["ec2", "s3", "lambda", "dynamodb"])
    aws_region: str = "ap-southeast-2"
    aws_url: str = "https://status.aws.amazon.com/rss/{service}-{region}.rss"
    # An AWS update that never says the service is back is taken to be over after this long.
    aws_stale_hours: float = Field(12, gt=0)
    # Azure: an item counts when it names one of these regions.
    azure_regions: list[str] = Field(default_factory=lambda: ["Australia East"])
    azure_url: str = "https://rssfeed.azure.status.microsoft/en-us/status/feed/"
    # Google Cloud: an incident counts when it currently affects one of these locations.
    gcp_locations: list[str] = Field(default_factory=lambda: ["australia-southeast1"])
    gcp_url: str = "https://status.cloud.google.com/incidents.json"
    # Cloudflare: an incident counts when its locations or name contain one of these words.
    cloudflare_words: list[str] = Field(default_factory=lambda: ["SYD", "Sydney"])
    cloudflare_url: str = "https://www.cloudflarestatus.com/api/v2"


@dataclass(frozen=True)
class Incident:
    provider: str
    id: str
    title: str
    started: float | None  # when the provider first posted it
    updated: float | None
    link: str | None
    status: str = ""  # the provider's own word for it: "Service disruption", "investigating", ...

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"

    def describe(self, now: float) -> str:
        since = f" (posted {format_duration(max(0.0, now - self.started))} ago)" if self.started else ""
        return f"{self.provider}: {self.title}{since}"


@dataclass(frozen=True)
class Maintenance:
    name: str
    starts: float | None
    ends: float | None


@dataclass
class Checked:
    """What one provider's feeds said."""

    provider: str
    region: str  # what was checked, in words
    incidents: list[Incident] = field(default_factory=list)
    elsewhere: int = 0  # active incidents that don't touch Sydney
    maintenance: list[Maintenance] = field(default_factory=list)
    feed_at: float | None = None


# --- Reading the feeds ---


def _clean(text: str | None) -> str:
    """Plain text: tags removed, entities decoded, whitespace tidied."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _rss_date(text: str | None) -> float | None:
    try:
        return parsedate_to_datetime((text or "").strip()).timestamp()  # AWS's "PDT" is understood
    except (TypeError, ValueError):
        return None


def _iso(text: Any) -> float | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def _channel(data: bytes, name: str) -> Any:
    try:
        root = ElementTree.fromstring(data)
    except Exception as exc:  # defusedxml raises its own errors for unsafe XML, plus ParseError
        raise SourceError(f"{name}'s feed isn't valid RSS ({type(exc).__name__})") from exc
    channel = root.find("channel")
    if channel is None:
        raise SourceError(f"{name}'s feed has no channel in it")
    return channel


def parse_aws(data: bytes, service: str, now: float, stale_hours: float) -> tuple[list[Incident], float | None]:
    """The service's current event in Sydney, if its newest update doesn't say it's over."""
    channel = _channel(data, f"AWS {service}")
    name = _clean(channel.findtext("title")).removesuffix(" Service Status") or service
    updates = []
    for node in channel.findall("item"):
        title, at = _clean(node.findtext("title")), _rss_date(node.findtext("pubDate"))
        if title and at is not None:
            updates.append((at, title, (node.findtext("guid") or "").strip()))
    updates.sort(reverse=True)
    feed_at = _rss_date(channel.findtext("lastBuildDate"))
    if not updates or AWS_RESOLVED.search(updates[0][1]) or now - updates[0][0] > stale_hours * 3600:
        return [], feed_at
    # The event's updates: the newest ones, back to the last time it said all was well.
    event = []
    for update in updates:
        if AWS_RESOLVED.search(update[1]):
            break
        event.append(update)
    first, newest = event[-1], event[0]
    status = newest[1].partition(":")[0] if ":" in newest[1] else ""
    return [Incident(AWS, first[2] or f"{service}:{int(first[0])}", f"{name}: {newest[1]}", first[0], newest[0],
                     PAGES[AWS], status)], feed_at


def parse_azure(data: bytes, regions: list[str]) -> Checked:
    channel = _channel(data, AZURE)
    wanted = re.compile("|".join(re.escape(r) for r in regions), re.IGNORECASE) if regions else None
    checked = Checked(AZURE, " or ".join(regions), feed_at=_rss_date(channel.findtext("lastBuildDate")))
    for node in channel.findall("item"):
        title, text = _clean(node.findtext("title")), _clean(node.findtext("description"))
        if not title:
            continue
        if wanted and wanted.search(f"{title} {text}"):
            at = _rss_date(node.findtext("pubDate"))
            ident = (node.findtext("guid") or node.findtext("link") or title).strip()
            checked.incidents.append(Incident(AZURE, ident, title, at, at, (node.findtext("link") or "").strip()
                                              or PAGES[AZURE]))
        else:
            checked.elsewhere += 1
    return checked


def parse_gcp(data: Any, locations: list[str]) -> Checked:
    if not isinstance(data, list):
        raise SourceError("Google Cloud's incidents aren't a list")
    checked = Checked(GCP, ", ".join(locations))
    checked.feed_at = max(filter(None, (_iso(i.get("modified")) for i in data if isinstance(i, dict))), default=None)
    for i in data:
        if not isinstance(i, dict) or i.get("end"):
            continue
        here = {loc.get("id") for loc in i.get("currently_affected_locations") or [] if isinstance(loc, dict)}
        if not here & set(locations):
            checked.elsewhere += 1
            continue
        uri = str(i.get("uri") or "")
        checked.incidents.append(Incident(
            GCP, str(i.get("id") or i.get("number")), _clean(i.get("external_desc")) or "An incident",
            _iso(i.get("created")) or _iso(i.get("begin")), _iso(i.get("modified")),
            PAGES[GCP] + uri.lstrip("/") if uri else PAGES[GCP], str(i.get("status_impact") or "").replace("_", " ").lower(),
        ))
    return checked


def parse_cloudflare(incidents: Any, maintenances: Any, words: list[str]) -> Checked:
    if not isinstance(incidents, dict) or not isinstance(incidents.get("incidents"), list):
        raise SourceError("Cloudflare's status API sent no incident list")
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b") if words else None

    def ours(thing: dict[str, Any]) -> bool:
        names = [thing.get("name") or ""] + [c.get("name") or "" for c in thing.get("components") or []]
        return bool(pattern and any(pattern.search(n) for n in names))

    checked = Checked(CLOUDFLARE, " or ".join(words), feed_at=_iso((incidents.get("page") or {}).get("updated_at")))
    for i in incidents["incidents"]:
        if not isinstance(i, dict) or i.get("resolved_at"):
            continue
        if not ours(i):
            checked.elsewhere += 1
            continue
        checked.incidents.append(Incident(
            CLOUDFLARE, str(i.get("id")), _clean(i.get("name")), _iso(i.get("created_at")), _iso(i.get("updated_at")),
            i.get("shortlink") or PAGES[CLOUDFLARE], f"{i.get('status', '')}, {i.get('impact', '')} impact",
        ))
    for m in (maintenances or {}).get("scheduled_maintenances") or []:
        if isinstance(m, dict) and m.get("status") == "in_progress" and ours(m):
            checked.maintenance.append(Maintenance(_clean(m.get("name")), _iso(m.get("scheduled_for")),
                                                   _iso(m.get("scheduled_until"))))
    return checked


# --- Judging ---

REGIONS = {AWS: "Sydney (ap-southeast-2)", AZURE: "Australia East", GCP: "Sydney (australia-southeast1)",
           CLOUDFLARE: "Sydney data centre (SYD)"}


def assess(checked: list[Checked], now: float, alertable: dict[str, bool] | None = None) -> Assessment:
    incidents = [i for c in checked for i in c.incidents]
    maintenance = [m for c in checked for m in c.maintenance]
    if incidents:
        level = Level.ELEVATED
        reason = "; ".join(i.describe(now) for i in incidents)
    else:
        level = Level.NORMAL
        names = [f"{c.provider} {REGIONS.get(c.provider, c.region)}" for c in checked]
        reason = "No incidents for " + (", ".join(names[:-1]) + " or " + names[-1] if len(names) > 1 else names[0])
        if maintenance:
            reason += ". Cloudflare: " + "; ".join(m.name for m in maintenance) + " (scheduled maintenance)"
    signals = tuple(
        Signal(f"cloud:{i.key}", Level.ELEVATED, (national("internet"),), title=i.describe(now),
               alertable=(alertable or {}).get(i.key, True), at=i.started)
        for i in incidents
    )
    details = {
        "providers": [
            {"name": c.provider, "region": REGIONS.get(c.provider, c.region), "link": PAGES.get(c.provider),
             "incidents": [asdict(i) for i in c.incidents], "elsewhere": c.elsewhere,
             "maintenance": [asdict(m) for m in c.maintenance]}
            for c in checked
        ],
    }
    updated = [i.updated for i in incidents if i.updated]
    return Assessment(level, reason, details, content_at=max(updated, default=None), signals=signals)


@dataclass
class _Cached:
    etag: str | None
    modified: str | None
    body: bytes


class CloudAdapter(PollingAdapter):
    id = "cloud"
    name = "Cloud and CDN status"
    category = Category.NETWORK
    link = PAGES[AWS]
    Settings = CloudSettings
    scope = national("internet")
    settings: CloudSettings

    def __init__(self, ctx: AdapterContext, settings: CloudSettings) -> None:
        super().__init__(ctx, settings)
        self._cache: dict[str, _Cached] = {}
        self._last: dict[str, Checked] = {}  # each provider's last good answer
        self._active: dict[str, Incident] = {}

    async def get(self, url: str, name: str) -> bytes:
        """GET, asking "has it changed?" first. Returns the body (the saved one if unchanged)."""
        cached = self._cache.get(url)
        headers = {}
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached and cached.modified:
            headers["If-Modified-Since"] = cached.modified
        response = await self.ctx.http.get(url, headers=headers)
        if response.status_code == 304 and cached:
            return cached.body
        if response.status_code == 429:
            raise SourceError(f"{name} asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 400:
            raise SourceError(f"{name} answered HTTP {response.status_code}")
        self._cache[url] = _Cached(response.headers.get("ETag"), response.headers.get("Last-Modified"),
                                   response.content)
        return response.content

    async def _json(self, url: str, name: str) -> Any:
        try:
            return json.loads(await self.get(url, name))
        except ValueError as exc:
            raise SourceError(f"{name} sent something that isn't JSON") from exc

    async def check_aws(self, now: float) -> Checked:
        s = self.settings
        checked = Checked(AWS, s.aws_region)
        bodies = await asyncio.gather(*(
            self.get(s.aws_url.format(service=svc, region=s.aws_region), f"AWS {svc}") for svc in s.aws_services
        ))
        for service, body in zip(s.aws_services, bodies):
            found, feed_at = parse_aws(body, service, now, s.aws_stale_hours)
            checked.incidents += found
            checked.feed_at = max(filter(None, (checked.feed_at, feed_at)), default=None)
        return checked

    async def check_azure(self, now: float) -> Checked:
        return parse_azure(await self.get(self.settings.azure_url, AZURE), self.settings.azure_regions)

    async def check_gcp(self, now: float) -> Checked:
        return parse_gcp(await self._json(self.settings.gcp_url, GCP), self.settings.gcp_locations)

    async def check_cloudflare(self, now: float) -> Checked:
        base = self.settings.cloudflare_url.rstrip("/")
        incidents, maintenance = await asyncio.gather(
            self._json(f"{base}/incidents/unresolved.json", CLOUDFLARE),
            self._json(f"{base}/scheduled-maintenances/active.json", CLOUDFLARE),
        )
        return parse_cloudflare(incidents, maintenance, self.settings.cloudflare_words)

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        checks = {AWS: self.check_aws, AZURE: self.check_azure, GCP: self.check_gcp, CLOUDFLARE: self.check_cloudflare}
        answers = await asyncio.gather(*(check(now) for check in checks.values()), return_exceptions=True)
        problems, checked = [], []
        for provider, answer in zip(checks, answers):
            if isinstance(answer, asyncio.CancelledError):
                raise answer
            if isinstance(answer, BaseException):
                problems.append(f"{provider}: {describe_error(answer)}")
                if provider in self._last:
                    checked.append(self._last[provider])  # keep showing what it last said
                continue
            self._last[provider] = answer
            checked.append(answer)
        if len(problems) == len(checks):
            raise SourceError("No cloud status feed could be read (" + "; ".join(problems)[:200] + ")")

        alertable = {}
        for incident in (i for c in checked for i in c.incidents):
            alertable[incident.key] = self.see_item(
                incident.key, incident.started, title=incident.describe(now), level=Level.ELEVATED.value,
                url=incident.link, now=now,
            ).alertable
        self._timeline(checked, now)
        result = assess(checked, now, alertable)
        reason = result.reason
        if problems:
            reason += ". Couldn't read " + "; ".join(problems)
        result.details["feeds_failing"] = problems
        feeds = [c.feed_at for c in checked if c.feed_at]
        return Assessment(result.level, reason, result.details, content_at=result.content_at,
                          feed_at=max(feeds, default=None), signals=result.signals)

    def _timeline(self, checked: list[Checked], now: float) -> None:
        """Each incident goes on the timeline when it's first posted, and again when it's over."""
        current = {i.key: i for c in checked for i in c.incidents}
        for key, incident in current.items():
            self.ctx.db.add_event(
                incident.started or now, self.id, self.category.value, Level.ELEVATED.value,
                incident.describe(now).rsplit(" (posted", 1)[0], url=incident.link,
                details={"reason": incident.status}, dedupe_key=f"cloud:{key}",
            )
        for key, incident in self._active.items():
            if key not in current and any(c.provider == incident.provider for c in checked):
                self.ctx.db.add_event(now, self.id, self.category.value, "info",
                                      f"Over: {incident.provider}: {incident.title}", url=incident.link,
                                      dedupe_key=f"cloud:{key}:over")
        self._active = current
