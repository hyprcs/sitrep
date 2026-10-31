"""Australian ransomware victims. ransomware.live's terms don't allow republishing its data,
so every victim here is made up, in the shape its API sends."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.ransomware import (
    RansomwareAdapter, RansomwareSettings, assess, month_end, months_back, parse_victims,
)
from sitrep.config import Config
from sitrep.db import Database
from sitrep.models import Level, national
from sitrep.state import StatusBoard

NOW = datetime(2026, 10, 31, 3, 20, tzinfo=timezone.utc).timestamp()
S = RansomwareSettings()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def victim(name, hours_ago, sector="Manufacturing", group="examplegang", website="example.com.au", ident=None):
    return {"id": ident or f"id-{name}", "victim": name, "group": group, "activity": sector, "country": "AU",
            "discovered": iso(NOW - hours_ago * 3600), "attackdate": iso(NOW - hours_ago * 3600 - 86400),
            "website": website, "permalink": f"https://www.ransomware.example/id/{ident or name}",
            "post_url": "http://leaksite.example.onion/post", "screenshot": "https://images.example/x.png",
            "description": "Made up for the tests.", "press": "None"}


def doc(*victims):
    return {"client": "someone@example.com", "count": len(victims), "victims": list(victims)}


def parsed(*victims):
    return parse_victims(doc(*victims), S.telco_words)


# --- Reading and judging ---


def test_victims_keep_only_what_sitrep_needs():
    (v,) = parsed(victim("Example Pty Ltd", 2, sector="Not Found"))
    assert (v.name, v.group, v.sector, v.discovered, v.link) == (
        "Example Pty Ltd", "examplegang", "Unknown sector", NOW - 7200, "https://www.ransomware.example/id/Example Pty Ltd")
    assert not hasattr(v, "post_url") and not hasattr(v, "screenshot")
    with pytest.raises(SourceError, match="no victim list"):
        parse_victims({"client": "x"}, S.telco_words)


def test_telcos_are_found_by_name_or_website():
    found = parsed(victim("Coastal Broadband", 1, sector="Technology"),
                   victim("Example Networks", 1, sector="Technology", website="example-telecom.com.au"),
                   victim("Optusvision Fans Club", 1, sector="Other"),  # "Optus" starts a word: still counts
                   victim("Example Software", 1, sector="Technology"))
    assert [v.telco for v in found] == [True, True, True, False]


def test_an_ordinary_victim_is_info():
    result = assess(parsed(victim("Example Joinery", 3)), S, NOW)
    assert result.level is Level.NORMAL and result.signals == ()
    assert result.reason == ("1 Australian victim in the last 24 hours (usually 0.0 a day). Latest: Example Joinery "
                             "(Manufacturing), listed by examplegang, 3 hours ago")
    assert result.details["attribution"] == "Source: Ransomware.live"


def test_a_victim_in_a_critical_sector_is_elevated_for_a_day():
    result = assess(parsed(victim("Example Hospital", 2, sector="Healthcare")), S, NOW)
    assert result.level is Level.ELEVATED
    assert result.reason.startswith("New Australian ransomware victim in Healthcare: Example Hospital (Healthcare), "
                                    "listed by examplegang (2 hours ago)")
    (signal,) = result.signals
    assert signal.scopes == (national("cyber"),) and signal.key == "ransomware:id-Example Hospital"
    assert assess(parsed(victim("Example Hospital", 25, sector="Healthcare")), S, NOW).level is Level.NORMAL
    telco = assess(parsed(victim("Example Telecom", 1, sector="Technology")), S, NOW)
    assert telco.level is Level.ELEVATED and "in Telecoms" in telco.reason


def test_a_burst_is_elevated_only_when_big_and_unlikely():
    usual = [victim(f"Old {i}", 24 * (2 + i * 2)) for i in range(40)]  # about one every two days
    burst = [victim(f"New {i}", i + 1) for i in range(6)]
    result = assess(parsed(*usual, *burst), S, NOW)
    assert result.level is Level.ELEVATED and result.details["burst"]
    assert result.reason.startswith("6 Australian ransomware victims in the last 24 hours (usually 0.4 a day)")
    assert result.signals[-1].key == "ransomware:burst"
    assert not assess(parsed(*usual, *burst[:4]), S, NOW).details["burst"]  # fewer than 5
    busy = [victim(f"Busy {i}", 2 + i * 4.3) for i in range(500)]  # five or so a day, every day
    assert not assess(parsed(*busy, *burst), S, NOW).details["burst"]


def test_the_months_to_ask_for():
    assert months_back(NOW, 90) == [(2026, 10), (2026, 9), (2026, 8)]
    assert months_back(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp(), 40) == [(2026, 1), (2025, 12), (2025, 11)]
    assert month_end(2026, 2) == datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()


def test_polling_faster_than_ransomware_live_allows_is_refused():
    with pytest.raises(ValidationError):
        RansomwareSettings(interval_seconds=600)


# --- The adapter ---


def make(handler, now, key="test-key"):
    db = Database(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now[0]), http, lambda: now[0])
    adapter = RansomwareAdapter(ctx, RansomwareSettings(api_key=SecretStr(key)))
    adapter.register()
    return adapter, db, http


async def test_months_are_fetched_once_and_this_month_every_time():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == "test-key"
        params = dict(request.url.params)
        asked.append(f"{params['year']}-{params['month']}")
        assert params["country"] == "AU"
        if params["month"] == "10":
            return httpx.Response(200, json=doc(victim("Example Clinic", 1, sector="Healthcare")))
        return httpx.Response(200, json=doc())

    now = [NOW]
    adapter, db, http = make(handler, now)
    result = await adapter.poll()
    assert asked == ["2026-10", "2026-09", "2026-08"]
    assert result.level is Level.ELEVATED and result.signals[0].alertable
    asked.clear()
    now[0] += 3600
    await adapter.poll()
    assert asked == ["2026-10"]
    asked.clear()
    now[0] = datetime(2026, 11, 1, 1, tzinfo=timezone.utc).timestamp()
    await adapter.poll()
    assert asked == ["2026-11", "2026-10"]  # one last look at October now that it's over

    # Only the name, gang, sector and ransomware.live page are kept: no leak-site links.
    stored = str([dict(r) for r in db.query("SELECT * FROM items")]) + str(db.events_since(0))
    assert "Example Clinic" in stored and ".onion" not in stored and "someone@example.com" not in stored
    await http.aclose()
    db.close()


async def test_a_missing_or_refused_key_says_what_to_do():
    adapter, db, http = make(lambda r: httpx.Response(200, json=doc()), [NOW], key="")
    with pytest.raises(SourceError, match="add RANSOMWARE_LIVE_API_KEY to .env"):
        await adapter.poll()
    await http.aclose()
    db.close()
    adapter, db, http = make(lambda r: httpx.Response(401), [NOW])
    with pytest.raises(SourceError, match="didn't accept the key"):
        await adapter.poll()
    await http.aclose()
    db.close()
