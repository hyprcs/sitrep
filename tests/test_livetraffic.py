"""Live Traffic NSW: reading hazards, which ones show, and talking to the Open Data Hub."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.livetraffic import LiveTrafficAdapter, LiveTrafficSettings, _point, assess, parse
from sitrep.config import Config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.state import StatusBoard

FIXTURES = Path(__file__).parent / "fixtures" / "livetraffic"  # real feeds, © Transport for NSW, CC BY 4.0
HOME = (-33.77878, 151.12425)
BBOX = [149.97, -34.34, 151.64, -32.99]
S = LiveTrafficSettings(api_key=SecretStr("test-token"))


def load(kind: str) -> dict:
    return json.loads((FIXTURES / f"{kind}_open_20261031.json").read_text(encoding="utf-8"))


def hazards():
    return parse(load("incident"), "incident") + parse(load("flood"), "flood") + parse(load("majorevent"), "majorevent")


NOW = max(h.updated for h in hazards() if h.kind == "incident") + 60


def crash_near_home() -> dict:
    data = load("incident")
    crash = copy.deepcopy(data["features"][0])
    crash["id"] = 999001
    crash["geometry"]["coordinates"] = [151.130, -33.785]  # the M2 at Macquarie Park
    crash["properties"].update({
        "mainCategory": "CRASH", "displayName": "CRASH Multi-vehicle", "isMajor": True, "incidentKind": "Unplanned",
        "ended": False, "created": int(NOW * 1000) - 600_000, "lastUpdated": int(NOW * 1000) - 120_000,
        "roads": [{"mainStreet": "M2 Motorway", "suburb": "Macquarie Park"}],
        "adviceA": "Reduce speed", "adviceB": "Expect delays",
    })
    data["features"].append(crash)
    return data


# --- Reading ---


def test_the_real_feeds_parse():
    incidents = parse(load("incident"), "incident")
    assert len(incidents) == 45
    first = incidents[0]
    assert first.id.startswith("incident:") and first.updated is not None and first.updated < 1e11  # seconds, not ms
    assert -38 < first.lat < -28 and 140 < first.lon < 154  # NSW
    assert parse(load("fire"), "fire") == []


def test_coordinates_in_either_order():
    assert _point({"type": "Point", "coordinates": [151.13, -33.78]}) == (-33.78, 151.13)
    assert _point({"type": "Point", "coordinates": [-33.78, 151.13]}) == (-33.78, 151.13)  # as the guide says
    assert _point({"type": "LineString", "coordinates": []}) is None


def test_titles_say_what_and_where():
    (crash,) = [h for h in parse(crash_near_home(), "incident") if h.id == "incident:999001"]
    assert crash.title == "Crash (multi-vehicle): M2 Motorway, Macquarie Park"
    assert (crash.major, crash.planned, crash.advice) == (True, False, "Reduce speed; Expect delays")


def gas_leak():
    # The real gas leak in the CBD on 25 Nov 2026, from that day's feed.
    found = parse(json.loads((FIXTURES / "incident_open_20261125.json").read_text(encoding="utf-8")), "incident")
    (leak,) = [h for h in found if h.road == "Elizabeth Street"]
    return leak


def test_titles_say_exactly_what_the_feed_says_it_is():
    leak = gas_leak()
    assert (leak.category, leak.what) == ("HAZARD", "Gas leak")  # not just "Hazard"
    assert leak.title == "Gas leak: Elizabeth Street, Sydney"
    assert leak.location == "just past King Street"
    assert leak.attending == ("Emergency services", "Transport for NSW", "Utility Company")
    assert leak.detail == ("Just past King Street. Attending: Emergency services, Transport for NSW, Utility "
                           "Company. Prepare to merge; Exercise caution")


@pytest.mark.parametrize(("category", "sub", "display", "what"), [
    ("CRASH", "Truck, Car", "CRASH Truck, Car", "Crash (truck, car)"),
    ("BREAKDOWN", "Car towing caravan", "BREAKDOWN Car towing caravan", "Breakdown (car towing caravan)"),
    ("ADVERSE WEATHER", "Water over road", "ADVERSE WEATHER Water over road", "Water over road"),
    ("EMERGENCY ROADWORK", "null", "EMERGENCY ROADWORK Road damage", "Emergency roadwork (road damage)"),
    ("FIRE", "Grass fire", "", "Grass fire"),
    ("SPECIAL EVENT", "", "West Head Road Cyclist Race", "Special event (West Head Road Cyclist Race)"),
    ("CHANGED TRAFFIC CONDITIONS", "", "SPECIAL EVENT CLEARWAYS Sydney Trains trackwork",
     "Changed traffic conditions (Sydney Trains trackwork)"),
    ("CHANGED TRAFFIC CONDITIONS", "", "CHANGED TRAFFIC CONDITIONS Snowy 2.0", "Changed traffic conditions (Snowy 2.0)"),
    ("OPEN TO  4WD ONLY", "", "OPEN TO  4WD ONLY", "Open to 4wd only"),
    ("HAZARD", "", "HAZARD", "Hazard"),  # when that's all the feed says
])
def test_what_a_hazard_is(category, sub, display, what):
    from sitrep.adapters.livetraffic import _text, what_of
    assert what_of(category, _text(sub), display) == what


def test_where_on_the_road():
    from sitrep.adapters.livetraffic import location_of
    assert location_of({"locationQualifier": "near", "crossStreet": "Poveys Road"}) == "near Poveys Road"
    assert location_of({"locationQualifier": "between", "crossStreet": "Day Street",
                        "secondLocation": "Brown Street"}) == "between Day Street and Brown Street"
    assert location_of({"locationQualifier": "-", "crossStreet": "just south of Gulgan Road overpass"}) == (
        "just south of Gulgan Road overpass")
    assert location_of({"locationQualifier": "at", "crossStreet": ""}) == ""


def test_the_cross_street_stays_out_of_the_title():
    # Story clusters match titles word by word: "King" and "Street" would tie unrelated headlines to it.
    from sitrep.stories import words
    assert words(gas_leak().title, ["Sydney"]) == {"gas", "leak", "elizabeth", "street"}


def test_ended_hazards_are_dropped():
    data = crash_near_home()
    data["features"][-1]["properties"]["ended"] = True
    assert "incident:999001" not in {h.id for h in parse(data, "incident")}


def test_something_else_is_an_error():
    with pytest.raises(SourceError, match="isn't the GeoJSON"):
        parse([], "incident")


# --- Judging ---


def test_never_raises_the_level_and_leaves_out_long_term_works():
    result = assess(hazards(), HOME, BBOX, S, NOW)
    assert result.level == Level.NORMAL and result.signals == ()
    titles = [p["title"] for p in result.details["map"]]
    assert not any("Sydney Harbour Bridge" in t for t in titles)  # planned works running for years
    assert result.content_at == NOW - 60


def test_a_major_crash_near_home_is_listed_and_mapped():
    data = crash_near_home()
    found = parse(data, "incident") + parse(load("flood"), "flood")
    result = assess(found, HOME, BBOX, S, NOW)
    assert result.level == Level.NORMAL  # still: it only feeds the map and, later, clusters
    assert "Nearest: Crash (multi-vehicle): M2 Motorway, Macquarie Park, 1 km" in result.reason
    assert "Major: Crash (multi-vehicle): M2 Motorway, Macquarie Park" in result.reason
    first = result.details["near"][0]
    assert (first["title"], first["mark"]) == ("Crash (multi-vehicle): M2 Motorway, Macquarie Park", "watch")
    point = next(p for p in result.details["map"] if p["id"] == "incident:999001")
    assert (point["kind"], point["label"], point["mark"]) == ("traffic", "Major hazard", "watch")


# --- The adapter ---


def make(handler, now=NOW, token="test-token"):
    db = Database(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now), http, lambda: now)
    adapter = LiveTrafficAdapter(ctx, LiveTrafficSettings(api_key=SecretStr(token)))
    adapter.register()
    return adapter, db, http


async def test_poll_reads_each_feed_with_the_token_and_events_less_often():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        kind = request.url.path.split("/")[-2]
        asked.append((kind, request.headers.get("Authorization")))
        body = crash_near_home() if kind == "incident" else load(kind)
        return httpx.Response(200, json=body)

    adapter, db, http = make(handler)
    first = await adapter.poll()
    assert sorted(k for k, _ in asked) == ["fire", "flood", "incident", "majorevent"]
    assert {auth for _, auth in asked} == {"apikey test-token"}
    asked.clear()
    second = await adapter.poll()
    assert sorted(k for k, _ in asked) == ["fire", "flood", "incident"]  # major events every 15 minutes
    assert first.details["sydney"] == second.details["sydney"]  # the events it already had still count
    item = db.query("SELECT level, details FROM items WHERE item_id = 'incident:999001'")[0]
    assert item["level"] == "watch" and json.loads(item["details"])["suburb"] == "Macquarie Park"
    assert json.loads(item["details"])["what"] == "Crash (multi-vehicle)"  # for alerts to say what it is
    assert "Live Traffic major hazard: Crash (multi-vehicle): M2 Motorway, Macquarie Park" in [e["title"] for e in db.events_since(0)]
    await http.aclose()
    db.close()


async def test_a_missing_or_wrong_token_says_what_to_do():
    adapter, db, http = make(lambda r: httpx.Response(200, json=load("fire")), token="")
    with pytest.raises(SourceError, match="add TFNSW_API_KEY to .env"):
        await adapter.poll()
    await http.aclose()
    db.close()
    adapter, db, http = make(lambda r: httpx.Response(401, json={"ErrorDetails": {"Message": "unauthenticated"}}))
    with pytest.raises(SourceError, match="didn't accept the token"):
        await adapter.poll()
    await http.aclose()
    db.close()


async def test_unchanged_feeds_are_reused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=load(request.url.path.split("/")[-2]), headers={"ETag": '"v1"'})

    adapter, db, http = make(handler)
    first = await adapter.poll()
    second = await adapter.poll()
    assert first.details["map"] == second.details["map"]
    await http.aclose()
    db.close()
