"""Cloud and CDN status for Sydney. None of the providers' terms clearly allow sharing their
feeds, so every response here is made up, in the shape each provider sends."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.cloud import (
    AWS, CLOUDFLARE, GCP, Checked, CloudAdapter, CloudSettings, assess, parse_aws, parse_azure, parse_cloudflare,
    parse_gcp,
)
from sitrep.config import Config
from sitrep.db import Database
from sitrep.models import Level, national
from sitrep.state import StatusBoard

NOW = 1_793_412_000.0  # 2026-10-31 02:00 UTC
PDT = timezone(timedelta(hours=-7))


def rfc822(ts: float, zone=PDT, name="PDT") -> str:
    return datetime.fromtimestamp(ts, zone).strftime(f"%a, %d %b %Y %H:%M:%S {name}")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def aws_rss(*updates: tuple[str, float], service="Amazon Elastic Compute Cloud") -> bytes:
    items = "".join(
        f"<item><title><![CDATA[{title}]]></title><link>https://status.aws.amazon.com/</link>"
        f"<pubDate>{rfc822(ts)}</pubDate><guid isPermaLink=\"false\">https://status.aws.amazon.com/#ec2-ap-southeast-2_"
        f"{int(ts)}</guid><description><![CDATA[Made up for the tests.]]></description></item>"
        for title, ts in updates
    )
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            f"<title><![CDATA[{service} (Sydney) Service Status]]></title><link>https://status.aws.amazon.com/</link>"
            f"<lastBuildDate>{rfc822(NOW)}</lastBuildDate><ttl>5</ttl>{items}</channel></rss>").encode()


def azure_rss(*items: tuple[str, str]) -> bytes:
    body = "".join(f"<item><guid>{t}</guid><title>{t}</title><description>{d}</description>"
                   f"<pubDate>{rfc822(NOW - 600, timezone.utc, 'Z')}</pubDate></item>" for t, d in items)
    return (f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>Azure Status</title>'
            f"<lastBuildDate>{rfc822(NOW, timezone.utc, 'Z')}</lastBuildDate>{body}</channel></rss>").encode()


def gcp_incident(ident, desc, *locations, ended=False, created=NOW - 900):
    return {"id": ident, "number": "123", "external_desc": desc, "begin": iso(created - 1800), "created": iso(created),
            "modified": iso(created + 60), "end": iso(NOW - 60) if ended else None, "uri": f"incidents/{ident}",
            "status_impact": "SERVICE_DISRUPTION", "severity": "medium",
            "currently_affected_locations": [{"title": loc, "id": loc} for loc in locations]}


def cf_incident(ident, name, *components, resolved=False):
    return {"id": ident, "name": name, "status": "investigating", "impact": "minor",
            "created_at": iso(NOW - 900), "updated_at": iso(NOW - 300), "resolved_at": iso(NOW) if resolved else None,
            "shortlink": f"https://stspg.example/{ident}", "components": [{"name": c} for c in components]}


def cf(*incidents, maintenance=()):
    return ({"page": {"updated_at": iso(NOW - 60)}, "incidents": list(incidents)},
            {"scheduled_maintenances": list(maintenance)})


# --- Reading each provider ---


def test_aws_counts_the_newest_update_until_the_service_is_back():
    assert parse_aws(aws_rss(), "ec2", NOW, 12)[0] == []
    ongoing = aws_rss(("Increased API Error Rates", NOW - 1200),
                      ("Informational message: Increased API Error Rates", NOW - 600),
                      ("Service is operating normally: [RESOLVED] Earlier problem", NOW - 86400))
    (incident,) = parse_aws(ongoing, "ec2", NOW, 12)[0]
    assert incident.title == "Amazon Elastic Compute Cloud (Sydney): Informational message: Increased API Error Rates"
    assert incident.started == NOW - 1200 and incident.status == "Informational message"  # the event's first update
    assert incident.id.endswith(f"_{int(NOW - 1200)}")  # the same id for every update of one event
    over = aws_rss(("Increased API Error Rates", NOW - 1200),
                   ("Service is operating normally: [RESOLVED] Increased API Error Rates", NOW - 60))
    assert parse_aws(over, "ec2", NOW, 12)[0] == []
    assert parse_aws(aws_rss(("Performance issues: Slow", NOW - 13 * 3600)), "ec2", NOW, 12)[0] == []


def test_aws_dates_in_pdt_are_understood():
    (incident,) = parse_aws(aws_rss(("Service disruption: Instances unreachable", NOW - 300)), "ec2", NOW, 12)[0]
    assert incident.started == NOW - 300


def test_azure_items_count_when_they_name_australia_east():
    checked = parse_azure(azure_rss(
        ("Azure Front Door - Australia East - Mitigated", "Customers in Australia East may see errors"),
        ("Virtual Machines - West Europe", "Some customers may see errors"),
    ), ["Australia East"])
    assert [i.title for i in checked.incidents] == ["Azure Front Door - Australia East - Mitigated"]
    assert checked.elsewhere == 1


def test_gcp_counts_active_incidents_in_sydney():
    checked = parse_gcp([
        gcp_incident("a", "Cloud SQL errors in Sydney", "australia-southeast1"),
        gcp_incident("b", "Old Sydney problem", "australia-southeast1", ended=True),
        gcp_incident("c", "Vertex AI errors", "us-central1", "global"),
    ], ["australia-southeast1"])
    (incident,) = checked.incidents
    assert (incident.title, incident.started, incident.link, incident.status) == (
        "Cloud SQL errors in Sydney", NOW - 900, "https://status.cloud.google.com/incidents/a", "service disruption")
    assert checked.elsewhere == 1


def test_cloudflare_counts_unresolved_incidents_touching_sydney():
    incidents, maintenance = cf(
        cf_incident("1", "Elevated latency", "Sydney, Australia - (SYD)"),
        cf_incident("2", "Network issues in Sydney"),
        cf_incident("3", "Errors in Canberra", "Canberra, Australia - (CBR)"),
        cf_incident("4", "Old SYD problem", "Sydney, Australia - (SYD)", resolved=True),
        maintenance=[{"name": "SYD (Sydney) on 2026-10-31", "status": "in_progress",
                      "scheduled_for": iso(NOW - 600), "scheduled_until": iso(NOW + 3000),
                      "components": [{"name": "Sydney, Australia - (SYD)"}]},
                     {"name": "FRA (Frankfurt) on 2026-10-31", "status": "in_progress", "components": []}],
    )
    checked = parse_cloudflare(incidents, maintenance, ["SYD", "Sydney"])
    assert [i.id for i in checked.incidents] == ["1", "2"] and checked.elsewhere == 1
    assert [m.name for m in checked.maintenance] == ["SYD (Sydney) on 2026-10-31"]
    with pytest.raises(SourceError, match="no incident list"):
        parse_cloudflare({}, {}, ["SYD"])


def test_any_incident_is_elevated_and_about_the_internet():
    (incident,) = parse_gcp([gcp_incident("a", "Cloud SQL errors in Sydney", "australia-southeast1")],
                            ["australia-southeast1"]).incidents
    result = assess([Checked(AWS, "x"), Checked(GCP, "y", [incident])], NOW)
    assert result.level is Level.ELEVATED
    assert result.reason == "Google Cloud: Cloud SQL errors in Sydney (posted 15 minutes ago)"
    (signal,) = result.signals
    assert signal.scopes == (national("internet"),) and signal.level is Level.ELEVATED
    quiet = assess([Checked(AWS, "x"), Checked(CLOUDFLARE, "y")], NOW)
    assert quiet.level is Level.NORMAL
    assert quiet.reason == "No incidents for AWS Sydney (ap-southeast-2) or Cloudflare Sydney data centre (SYD)"


# --- The adapter ---


class Server:
    def __init__(self):
        self.aws, self.azure = aws_rss(), azure_rss()
        self.gcp: list = []
        self.cf = cf()
        self.down: set[str] = set()
        self.conditional: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host
        if host in self.down:
            return httpx.Response(503)
        if "aws" in host:
            body = self.aws
        elif "azure" in host:
            body = self.azure
        elif "cloud.google" in host:
            body = json.dumps(self.gcp).encode()
        else:
            body = json.dumps(self.cf[0] if "incidents" in url else self.cf[1]).encode()
        etag = f'"{hash(body)}"'
        if request.headers.get("If-None-Match") == etag:
            self.conditional.append(host)
            return httpx.Response(304)
        return httpx.Response(200, content=body, headers={"ETag": etag})


def make(server, now):
    db = Database(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now[0]), http, lambda: now[0])
    adapter = CloudAdapter(ctx, CloudSettings(aws_services=["ec2"]))
    adapter.register()
    return adapter, db, http


async def test_an_incident_is_elevated_put_on_the_timeline_and_ended():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    assert (await adapter.poll()).level is Level.NORMAL
    server.cf = cf(cf_incident("1", "Elevated errors", "Sydney, Australia - (SYD)"))
    result = await adapter.poll()
    assert result.level is Level.ELEVATED and result.reason.startswith("Cloudflare: Elevated errors")
    assert result.signals[0].alertable  # posted 15 minutes ago, seen within the recency limit
    server.cf = cf()
    now[0] += 300
    assert (await adapter.poll()).level is Level.NORMAL
    assert [e["title"] for e in db.events_since(0)] == ["Over: Cloudflare: Elevated errors", "Cloudflare: Elevated errors"]
    await http.aclose()
    db.close()


async def test_unchanged_feeds_are_not_downloaded_again():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    await adapter.poll()
    await adapter.poll()
    assert len(server.conditional) == 5  # AWS, Azure, Google Cloud and Cloudflare's two
    await http.aclose()
    db.close()


async def test_a_failing_provider_is_named_and_keeps_its_last_incidents():
    server, now = Server(), [NOW]
    server.gcp = [gcp_incident("a", "Cloud SQL errors in Sydney", "australia-southeast1")]
    adapter, db, http = make(server, now)
    await adapter.poll()
    server.down = {"status.cloud.google.com"}
    result = await adapter.poll()
    assert result.details["feeds_failing"] == ["Google Cloud: Google Cloud answered HTTP 503"]
    assert result.level is Level.ELEVATED and "Couldn't read Google Cloud" in result.reason
    server.down = {"status.aws.amazon.com", "rssfeed.azure.status.microsoft", "status.cloud.google.com",
                   "www.cloudflarestatus.com"}
    with pytest.raises(SourceError, match="No cloud status feed could be read"):
        await adapter.poll()
    await http.aclose()
    db.close()
