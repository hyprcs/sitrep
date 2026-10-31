"""Aircraft over Sydney. Made-up aircraft, in the shape adsb.lol and adsb.fi send."""

from __future__ import annotations

import json
import math
from datetime import datetime

import httpx

from sitrep.adapters.adsb import (
    AdsbAdapter, AdsbSettings, Check, Plane, Tracks, airborne, assess, circling, may_circle, parse_aircraft,
    usual_count,
)
from sitrep.adapters.base import AdapterContext
from sitrep.config import ConvergenceSettings, Config
from sitrep.convergence import _events_from_items
from sitrep.db import Database
from sitrep.geo import km_between
from sitrep.models import SYDNEY, Level
from sitrep.state import StatusBoard
from sitrep.util import sydney_timestamp

S = AdsbSettings()
CBD = (S.centre_lat, S.centre_lon)
NOW = sydney_timestamp(datetime(2026, 10, 31, 15, 0))  # a Saturday afternoon
PARRAMATTA = (-33.8150, 151.0011)


def ac(hex_, lat, lon, alt=1500, category="A1", flight="TEST1", type_="C172", seen_pos=1.0):
    return {"hex": hex_, "flight": f"{flight:<8}", "t": type_, "category": category, "lat": lat, "lon": lon,
            "alt_baro": alt, "seen_pos": seen_pos, "gs": 90}


def answer(*aircraft, now=NOW):
    return {"ac": list(aircraft), "now": now * 1000, "total": len(aircraft), "msg": "No error", "ctime": 0, "ptime": 0}


def plane(hex_, lat, lon, at, alt=1500.0, category="A7"):
    return Plane(hex_, "POL21", "AS50", category, lat, lon, alt, at)


def on_circle(centre, km, angle):
    """A point km from centre, at angle (radians)."""
    dlat = km / 111.2 * math.cos(angle)
    dlon = km / (111.2 * math.cos(math.radians(centre[0]))) * math.sin(angle)
    return centre[0] + dlat, centre[1] + dlon


# --- Reading and counting ---


def test_counts_are_aircraft_in_the_air_within_the_circle():
    planes, at = parse_aircraft(answer(
        ac("a1", *CBD), ac("a2", *CBD, alt="ground"), ac("a3", *CBD, category="C2", alt=0),
        ac("a4", -33.0, 151.2),  # Newcastle-ish: outside 40 km
    ), CBD, 40)
    assert at == NOW and [p.hex for p in planes] == ["a1", "a2", "a3"]
    assert airborne(planes) == 1 and planes[1].alt is None


def test_what_may_be_circling():
    assert may_circle(plane("h", *PARRAMATTA, 0, alt=5000), S)  # a helicopter, at any height
    assert not may_circle(plane("f", *PARRAMATTA, 0, alt=5000, category="A3"), S)  # a jet up high
    assert may_circle(plane("f", *PARRAMATTA, 0, alt=2000, category="A1"), S)  # a light aircraft down low
    assert not may_circle(plane("b", *PARRAMATTA, 0, alt=1000, category="B2"), S)  # a balloon
    assert not may_circle(plane("h", *PARRAMATTA, 0, alt=150), S)  # landing or landed
    assert not may_circle(plane("h", -33.94, 151.17, 0), S)  # at Sydney Airport
    assert not may_circle(plane("h", -33.92, 150.99, 0), S)  # Bankstown circuits


def test_circling_needs_ten_minutes_within_two_km():
    orbit = [plane("h", *on_circle(PARRAMATTA, 0.8, i / 2), NOW + i * 60) for i in range(11)]
    centre = circling(orbit, S)
    assert centre and km_between(*centre, *PARRAMATTA) < 0.5
    assert circling(orbit[:7], S) is None  # only 6 minutes so far
    straight = [plane("h", PARRAMATTA[0], PARRAMATTA[1] + i * 0.01, NOW + i * 60) for i in range(11)]
    assert circling(straight, S) is None  # about 1 km a minute: passing through
    gappy = orbit[:3] + orbit[8:]
    assert circling(gappy, S) is None  # 5 minutes unseen in the middle


def test_tracks_keep_one_orbit_going_and_forget_it_when_it_stops():
    tracks = Tracks()
    found = []
    for i in range(14):
        found = tracks.update([plane("h", *on_circle(PARRAMATTA, 0.8, i / 2), NOW + i * 60)], NOW + i * 60, S)
        if i == 10:
            (first,) = found
    (later,) = found
    assert later.id == first.id and later.place == "Parramatta" and round(later.minutes(NOW + 13 * 60)) == 13
    assert later.describe() == "Helicopter POL21 (AS50) circling near Parramatta for 13 minutes"
    gone = tracks.update([plane("h", PARRAMATTA[0], PARRAMATTA[1] + 0.2, NOW + 14 * 60)], NOW + 14 * 60, S)
    assert gone == []  # it flew off


# --- What's usual, and judging ---


def test_usual_is_this_hour_of_the_week_or_of_the_day_while_learning():
    hour = int(NOW // 3600 * 3600)
    weeks = {hour - w * 7 * 86400: 14 + w for w in (1, 2)}
    assert usual_count(weeks, NOW) == (15.5, "this hour of the week")
    days = {hour - d * 86400: 10 + d for d in (1, 2, 3)}
    assert usual_count(days, NOW) == (12, "this hour of the day")
    assert usual_count({}, NOW) == (None, "")


def test_far_fewer_aircraft_counts_only_when_adsb_fi_agrees():
    agreed = assess([], 2, 15, "this hour of the week", 3, Check(NOW, 3), [], S, NOW, NOW)
    assert agreed.level is Level.ELEVATED and agreed.signals[0].scopes == (SYDNEY,)
    assert agreed.reason.startswith("Far fewer aircraft over Sydney than usual: 2 in the air within 40 km of the CBD")
    disagreed = assess([], 2, 15, "this hour of the week", 3, Check(NOW, 13), [], S, NOW, NOW)
    assert disagreed.level is Level.NORMAL and "adsb.fi sees 13: probably an adsb.lol problem" in disagreed.reason
    unchecked = assess([], 2, 15, "this hour of the week", 3, Check(NOW, None, "timed out"), [], S, NOW, NOW)
    assert unchecked.level is Level.NORMAL and "couldn't check with adsb.fi (timed out)" in unchecked.reason
    night = assess([], 0, 3, "this hour of the week", 9, Check(NOW, 0), [], S, NOW, NOW)
    assert night.level is Level.NORMAL  # too few usually fly to tell
    assert assess([], 12, None, "", 0, None, [], S, NOW, NOW).level is Level.LEARNING


# --- The adapter ---


class Server:
    def __init__(self):
        self.lol = answer()
        self.fi = answer()
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        name = "adsb.fi" if "adsb.fi" in request.url.host else "adsb.lol"
        self.asked.append(name)
        return httpx.Response(200, content=json.dumps(self.fi if name == "adsb.fi" else self.lol).encode())


def make(server, now):
    db = Database(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now[0]), http, lambda: now[0])
    adapter = AdsbAdapter(ctx, AdsbSettings())
    adapter.register()
    return adapter, db, http


async def test_a_fall_is_double_checked_once_with_adsb_fi():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    for w in (1, 2):  # this hour, the last two weeks: 15 in the air
        db.set_gauge("adsb", "airborne", NOW - w * 7 * 86400, 15)
    server.lol = answer(ac("a1", *CBD), now=now[0])
    server.fi = answer(*(ac(f"f{i}", *CBD) for i in range(14)))
    results = []
    for i in range(4):
        now[0] = NOW + i * 60
        server.lol["now"] = now[0] * 1000
        results.append(await adapter.poll())
    assert [r.level for r in results] == [Level.NORMAL] * 4
    assert server.asked == ["adsb.lol"] * 3 + ["adsb.fi", "adsb.lol"]  # asked once, then remembered for 5 minutes
    assert "adsb.fi sees 14: probably an adsb.lol problem" in results[-1].reason
    assert db.gauges_since("adsb", "airborne", NOW - 3600) == {int(NOW // 3600 * 3600): 1}  # the hour's average
    await http.aclose()
    db.close()


async def test_circling_is_saved_for_the_map_and_convergence():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    result = None
    for i in range(12):
        now[0] = NOW + i * 60
        server.lol = answer(ac("7c1234", *on_circle(PARRAMATTA, 0.8, i / 2), alt=1200, category="A7",
                               flight="POL21", type_="AS50"), now=now[0])
        result = await adapter.poll()
    (orbit,) = result.details["orbits"]
    assert orbit["place"] == "Parramatta" and result.details["map"][0]["kind"] == "aircraft"
    assert "Helicopter POL21 (AS50) circling near Parramatta" in result.reason
    rows = [{**dict(r), "details": json.loads(r["details"])} for r in db.query("SELECT * FROM items")]
    (event,) = _events_from_items(rows, ConvergenceSettings())
    assert event.kind == "aircraft" and km_between(event.lat, event.lon, *PARRAMATTA) < 0.5
    assert db.events_since(0)[0]["title"] == "Helicopter POL21 (AS50) circling near Parramatta"
    assert "lat" not in str(db.gauges_since("adsb", "airborne", 0))  # no positions are saved, only counts
    await http.aclose()
    db.close()
