"""NSW RFS incidents: reading the feed, distances from home, and which incidents count."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import httpx

from sitrep.adapters.base import AdapterContext
from sitrep.adapters.rfs import (
    RfsAdapter,
    RfsSettings,
    assess,
    description_fields,
    feed_time,
    parse_feed,
)
from sitrep.config import Config
from sitrep.db import Database
from sitrep.geo import cell_of, km_between
from sitrep.models import Level
from sitrep.state import StatusBoard
from sitrep.util import sydney_timestamp

FIXTURE = Path(__file__).parent / "fixtures" / "rfs" / "majorIncidents_20261030.json"  # real feed, CC BY 4.0
HOME = (-33.77878, 151.12425)  # Macquarie Park
S = RfsSettings()


def feed() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def incidents():
    return parse_feed(feed())


def with_incident(level: str, lat: float, lon: float, title="LANE COVE NATIONAL PARK FIRE") -> dict:
    """The real feed plus one made-up incident at a chosen level and place."""
    data = feed()
    extra = copy.deepcopy(data["features"][0])
    extra["properties"].update({
        "title": title, "category": level, "guid": "https://incidents.rfs.nsw.gov.au/api/v1/incidents/900001",
        "pubDate": "30/10/2026 5:50:00 AM",
        "description": f"ALERT LEVEL: {level} <br />COUNCIL AREA: Ryde <br />STATUS: Out of control",
    })
    extra["geometry"] = {"type": "Point", "coordinates": [lon, lat]}
    data["features"].append(extra)
    return data


NOW = feed_time("30/10/2026 6:00:00 AM")


# --- Reading the feed ---


def test_times_are_utc():
    # The description of that incident says "UPDATED: 30 Oct 2026 16:47", Sydney time.
    assert feed_time("30/10/2026 5:47:00 AM") == sydney_timestamp(datetime(2026, 10, 30, 16, 47))
    assert feed_time("30/10/2026 12:05:00 PM") - feed_time("30/10/2026 12:05:00 AM") == 12 * 3600
    assert feed_time("yesterday") is None


def test_the_real_feed_parses():
    found = incidents()
    assert len(found) == 34
    first = found[0]
    assert (first.id, first.level, first.council, first.status, first.kind, first.size) == (
        "683618", "Advice", "Singleton", "Under control", "Bush Fire", "19 ha")
    assert first.lat is not None and first.lon is not None  # from inside a nested GeometryCollection
    assert {i.level for i in found} == {"Advice", "Planned burn", "Not applicable"}


def test_description_fields():
    fields = description_fields("ALERT LEVEL: Watch and Act <br />LOCATION: Smith &amp; Co Rd <br/>SIZE: 5 ha")
    assert fields == {"ALERT LEVEL": "Watch and Act", "LOCATION": "Smith & Co Rd", "SIZE": "5 ha"}


def test_something_that_isnt_geojson_is_an_error():
    import pytest

    from sitrep.adapters.base import SourceError

    with pytest.raises(SourceError, match="isn't the GeoJSON"):
        parse_feed({"items": []})


# --- Judging by distance ---


def test_a_quiet_day_names_the_nearest_incident():
    result = assess(incidents(), HOME, "Macquarie Park", S, NOW)
    assert result.level == Level.NORMAL
    assert result.reason.startswith("No Emergency Warning within 50 km of Macquarie Park")
    assert "Across NSW: 34 incidents (0 Emergency Warnings, 0 at Watch and Act, 9 at Advice)" in result.reason
    assert result.content_at == feed_time("30/10/2026 5:47:00 AM")
    assert result.signals == ()


def test_an_emergency_warning_near_home_is_critical():
    lat, lon = -33.785, 151.155  # Lane Cove National Park, about 3 km away
    result = assess(parse_feed(with_incident("Emergency Warning", lat, lon)), HOME, "Macquarie Park", S, NOW)
    assert result.level == Level.CRITICAL
    assert result.reason.startswith("Emergency Warning: LANE COVE NATIONAL PARK FIRE (3 km away, Ryde)")
    (signal,) = result.signals
    assert (signal.key, signal.level, signal.emergency) == ("rfs:900001", Level.CRITICAL, True)
    assert signal.scopes == (f"area:{cell_of(lat, lon)[0]},{cell_of(lat, lon)[1]}",)


def test_further_away_counts_for_less():
    blue_mountains = (-33.71, 150.31)  # about 76 km away
    assert km_between(*HOME, *blue_mountains) > 50
    wahroonga = (-33.70, 151.07)  # about 10 km away
    hornsby_heights = (-33.63, 151.10)  # about 17 km away
    cases = [
        ("Emergency Warning", hornsby_heights, Level.ELEVATED),
        ("Emergency Warning", blue_mountains, Level.NORMAL),
        ("Watch and Act", wahroonga, Level.ELEVATED),
        ("Watch and Act", hornsby_heights, Level.NORMAL),
        ("Advice", wahroonga, Level.NORMAL),
    ]
    for level, (lat, lon), expected in cases:
        result = assess(parse_feed(with_incident(level, lat, lon)), HOME, "Macquarie Park", S, NOW)
        assert result.level == expected, (level, lat, lon)
    far = assess(parse_feed(with_incident("Emergency Warning", *blue_mountains)), HOME, "Macquarie Park", S, NOW)
    assert far.details["statewide"]["Emergency Warning"] == 1  # still counted and shown, just not raised


def test_dashboard_lists_what_is_near_home_worst_first():
    data = with_incident("Watch and Act", -33.70, 151.07)
    result = assess(parse_feed(data), HOME, "Macquarie Park", S, NOW)
    near = result.details["near"]
    assert near[0]["level"] == "Watch and Act" and near[0]["mark"] == "elevated"
    assert all(n["km"] <= 50 for n in near)
    assert len(result.details["map"]) == 35  # everything with a location, for the map


# --- The adapter ---


def run_poll(handler, now: float):
    db = Database(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now), http, lambda: now)
    adapter = RfsAdapter(ctx, RfsSettings())
    adapter.register()
    return adapter, db, http


async def test_poll_asks_whether_the_feed_changed_and_remembers_items():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("If-None-Match"))
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=with_incident("Emergency Warning", -33.785, 151.155),
                              headers={"ETag": '"v1"', "Last-Modified": "Fri, 30 Oct 2026 05:52:41 GMT"})

    adapter, db, http = run_poll(handler, NOW)
    first = await adapter.poll()
    second = await adapter.poll()
    assert calls == [None, '"v1"']
    assert first.level == second.level == Level.CRITICAL
    assert first.feed_at == feed_time("30/10/2026 5:52:41 AM")
    (signal,) = first.signals
    assert signal.alertable  # updated 10 minutes before sitrep saw it: within the 15-minute limit
    rows = db.query("SELECT item_id, level FROM items WHERE item_id = '900001'")
    assert dict(rows[0]) == {"item_id": "900001", "level": "critical"}
    timeline = db.events_since(0)
    assert [e["title"] for e in timeline] == ["RFS Emergency Warning: LANE COVE NATIONAL PARK FIRE"]
    await http.aclose()
    db.close()


async def test_an_old_warning_seen_after_an_outage_cant_alert():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=with_incident("Emergency Warning", -33.785, 151.155))

    adapter, db, http = run_poll(handler, NOW + 3 * 3600)  # sitrep comes back 3 hours later
    result = await adapter.poll()
    assert result.level == Level.CRITICAL  # the dashboard still shows it
    assert not result.signals[0].alertable  # but it can't set off a message
    await http.aclose()
    db.close()
