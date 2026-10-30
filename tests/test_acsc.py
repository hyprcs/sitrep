"""ACSC: reading the RSS feeds, spotting warning signs, and the adapter end to end.

The fixtures are ACSC's real feeds as of 29 October 2026 (tests/fixtures/acsc):
10 alerts and 6 advisories going back to August.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from sitrep.adapters.acsc import (
    AcscAdapter,
    AcscSettings,
    assess,
    compile_signs,
    parse_feed,
    reader_user_agent,
)
from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.config import Config, HttpSettings, load_config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.runtime import build_adapter_specs
from sitrep.state import StatusBoard

FIXTURES = Path(__file__).parent / "fixtures" / "acsc"
ROOT = Path(__file__).parent.parent
SETTINGS = AcscSettings()
SIGNS = compile_signs(SETTINGS.warning_signs)


def feed(name: str) -> bytes:
    return (FIXTURES / f"{name}.xml").read_bytes()


def all_items():
    return parse_feed(feed("alerts"), "Alert", SIGNS) + parse_feed(feed("advisories"), "Advisory", SIGNS)


def item(title_start: str):
    return next(i for i in all_items() if i.title.startswith(title_start))


# --- Reading the feeds ---


def test_real_feeds_parse():
    alerts = parse_feed(feed("alerts"), "Alert", SIGNS)
    advisories = parse_feed(feed("advisories"), "Advisory", SIGNS)
    assert (len(alerts), len(advisories)) == (10, 6)
    fortinet = alerts[0]
    assert fortinet.title == "Critical vulnerabilities in Fortinet products"
    assert fortinet.link == "https://www.cyber.gov.au/alert/critical-vulnerabilities-in-fortinet-products"
    assert fortinet.summary.startswith("Fortinet have announced critical vulnerabilities")
    assert fortinet.published == 1792732307  # Fri, 23 Oct 2026 16:11:47 +1100
    assert "’" in item("Protect your organisations").title  # curly quotes survive


def test_warning_signs_in_real_items():
    assert item("Critical vulnerabilities in Fortinet").signs == ()  # "critical" alone isn't a sign
    assert item("Protecting operational technology").signs == ("critical infrastructure",)
    assert item("Active exploitation of Adobe Commerce").signs == ("active exploitation",)
    flagged = [i for i in all_items() if i.signs]
    assert len(flagged) == 6


def test_phrases_match_from_the_start_of_a_word():
    signs = compile_signs({"telecommunications": ["telecommunication", "TPG", "NBN"]})
    rss = (
        b"<rss><channel>"
        b"<item><title>A</title><description>Telecommunications providers</description><pubDate>Thu, 29 Oct 2026 10:00:00 +1100</pubDate></item>"
        b"<item><title>B</title><description>TPG's customers</description><pubDate>Thu, 29 Oct 2026 10:00:00 +1100</pubDate></item>"
        b"<item><title>C</title><description>ATPG and unbnd tools</description><pubDate>Thu, 29 Oct 2026 10:00:00 +1100</pubDate></item>"
        b"</channel></rss>"
    )
    assert [i.signs for i in parse_feed(rss, "Alert", signs)] == [("telecommunications",), ("telecommunications",), ()]


def test_html_in_summaries_becomes_plain_text():
    rss = (
        b"<rss><channel><item><title>T</title>"
        b"<description>&lt;p&gt;Patch &amp;amp; update&lt;/p&gt;</description>"
        b"<pubDate>Thu, 29 Oct 2026 10:00:00 +1100</pubDate></item></channel></rss>"
    )
    assert parse_feed(rss, "Alert", SIGNS)[0].summary == "Patch & update"


def test_unsafe_xml_is_refused():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
        b"<rss><channel><item><title>&b;</title></item></channel></rss>"
    )
    with pytest.raises(SourceError, match="isn't valid RSS"):
        parse_feed(bomb, "Alert", SIGNS)


def test_broken_xml_is_a_source_error():
    with pytest.raises(SourceError, match="isn't valid RSS"):
        parse_feed(b"<html>Service unavailable", "Alert", SIGNS)


# --- Judging ---


def test_quiet_day_names_the_latest_item():
    now = item("Critical vulnerabilities in Fortinet").published + 5.9 * 86400
    result = assess(all_items(), SETTINGS, now)
    assert result.level == Level.NORMAL
    assert result.reason == (
        "Nothing new in the last 24 hours. Latest: “Critical vulnerabilities in Fortinet products” (5.9 days ago)"
    )
    assert len(result.details["items"]) == 8


def test_new_item_with_a_warning_sign_is_elevated():
    adobe = item("Active exploitation of Adobe Commerce")
    result = assess(all_items(), SETTINGS, adobe.published + 3 * 3600)
    assert result.level == Level.ELEVATED
    assert result.reason == (
        "New ACSC alert: “Active exploitation of Adobe Commerce and Magento Open Source vulnerability” "
        "(3 hours ago; mentions active exploitation)"
    )


def test_new_item_without_warning_signs_is_normal():
    fortinet = item("Critical vulnerabilities in Fortinet")
    result = assess(all_items(), SETTINGS, fortinet.published + 2 * 3600)
    assert result.level == Level.NORMAL
    assert result.reason.startswith("New ACSC alert 2 hours ago, with no warning signs")


def test_a_warning_sign_counts_for_24_hours_only():
    adobe = item("Active exploitation of Adobe Commerce")
    assert assess(all_items(), SETTINGS, adobe.published + 25 * 3600).level == Level.NORMAL


def test_acsc_is_never_critical():
    for i in all_items():
        assert assess(all_items(), SETTINGS, i.published + 60).level != Level.CRITICAL


def test_shipped_config_is_valid():
    cfg = load_config(ROOT / "config.yaml", env_file=None)
    acsc = next(s for cls, s in build_adapter_specs(cfg) if cls.id == "acsc")
    assert len(acsc.feeds) == 2 and "telecommunications" in acsc.warning_signs


def test_reader_name_has_no_bot_in_it():
    assert reader_user_agent(HttpSettings()) == "sitrep/0.1.0 (self-hosted situational-awareness dashboard; RSS reader)"
    assert "bot" not in reader_user_agent(HttpSettings(contact="me@example.com")).lower()


# --- The adapter against a fake cyber.gov.au ---


class FakeAcsc:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        etag = f'"{name}-v1"'
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, content=feed(name), headers={"ETag": etag, "Content-Type": "application/rss+xml"})


@pytest.fixture
def make_adapter():
    made = []

    def make(handler, clock):
        db = Database(":memory:")
        made.append(db)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return AcscAdapter(AdapterContext(Config(), db, StatusBoard(db, clock), http, clock), SETTINGS), db

    yield make
    for db in made:
        db.close()


async def test_poll_reads_both_feeds_as_an_rss_reader(make_adapter):
    fake = FakeAcsc()
    now = item("Critical vulnerabilities in Fortinet").published + 3600
    adapter, _ = make_adapter(fake, lambda: now)
    result = await adapter.poll()
    assert result.level == Level.NORMAL
    assert sorted(r.url.path for r in fake.requests) == ["/rss/advisories", "/rss/alerts"]
    assert all(r.headers["User-Agent"].startswith("sitrep/0.1.0 (") for r in fake.requests)
    assert all("bot" not in r.headers["User-Agent"].lower() for r in fake.requests)


async def test_unchanged_feeds_are_not_downloaded_again(make_adapter):
    fake = FakeAcsc()
    now = item("Active exploitation of Adobe Commerce").published + 3600
    adapter, _ = make_adapter(fake, lambda: now)
    first = await adapter.poll()
    second = await adapter.poll()
    asked = sorted(r.headers.get("If-None-Match") for r in fake.requests[2:])
    assert asked == ['"advisories-v1"', '"alerts-v1"']  # the second poll only asks "has it changed?"
    assert second.reason == first.reason and second.level == Level.ELEVATED


async def test_items_from_the_last_week_go_on_the_timeline_once(make_adapter):
    fortinet = item("Critical vulnerabilities in Fortinet")
    now = fortinet.published + 3600
    adapter, db = make_adapter(FakeAcsc(), lambda: now)
    await adapter.poll()
    await adapter.poll()
    events = db.events_since(now - 7 * 86400)
    titles = [e["title"] for e in events]
    assert titles.count("ACSC alert: Critical vulnerabilities in Fortinet products") == 1
    assert all(now - e["ts"] <= 7 * 86400 for e in events)
    php = next(e for e in events if "PHP" in e["title"])
    assert php["level"] == "info" and php["ts"] == item("Critical vulnerabilities in PHP").published


async def test_slow_down_carries_retry_after(make_adapter):
    adapter, _ = make_adapter(lambda r: httpx.Response(429, headers={"Retry-After": "300"}), lambda: 1_800_000_000)
    with pytest.raises(SourceError, match="slow down") as caught:
        await adapter.poll()
    assert caught.value.retry_after == 300


async def test_server_error_fails_the_poll(make_adapter):
    adapter, _ = make_adapter(lambda r: httpx.Response(503), lambda: 1_800_000_000)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.poll()
