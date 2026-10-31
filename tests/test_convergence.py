"""Geographic convergence: several kinds of event in one small area."""

from __future__ import annotations

import json

from sitrep.config import ConvergenceSettings, ScoringSettings
from sitrep.convergence import Convergence, Event, _events_from_items, find_hotspots
from sitrep.db import Database
from sitrep.geo import cell_of
from sitrep.models import Category, Level, Overall
from sitrep.scoring import score_overall

S = ConvergenceSettings()
NOW = 1_793_412_000.0
KELLYVILLE = (-33.7080, 150.9510)
NEARBY = (-33.7150, 150.9600)  # about 1 km away, the next square over
FAR = (-33.8688, 151.2093)  # the CBD, ~30 km away


def ev(kind, where=KELLYVILLE, *, who="", emergency=False, alertable=True, title="something"):
    return Event(kind, cell_of(*where), where[0], where[1], NOW - 600, title, place="Kellyville",
                 alertable=alertable, emergency=emergency, who=who)


def posts(n, where=KELLYVILLE, **kw):
    return [ev("social", where, who=f"person{i}", title=f"smoke over Kellyville {i}", **kw) for i in range(n)]


# --- Finding areas ---


def test_three_kinds_in_neighbouring_squares_converge():
    (spot,) = find_hotspots([ev("fire"), ev("road", NEARBY), *posts(2)], S)
    assert spot.kinds(S) == {"fire", "road", "social"} and spot.where == "Kellyville"
    assert spot.level(S) is Level.ELEVATED


def test_two_kinds_or_far_apart_dont():
    assert find_hotspots([ev("fire"), ev("road")], S) == []
    assert find_hotspots([ev("fire"), ev("road"), ev("news", FAR)], S) == []


def test_posts_need_two_different_people():
    assert find_hotspots([ev("fire"), ev("road"), *posts(1)], S) == []
    same_person = [ev("social", who="person0"), ev("social", who="person0")]
    assert find_hotspots([ev("fire"), ev("road"), *same_person], S) == []


def test_touching_busy_squares_make_one_area_and_separate_ones_two():
    here = [ev("fire"), ev("road"), ev("news")]
    there = [ev("fire", FAR), ev("road", FAR), ev("news", FAR)]
    assert len(find_hotspots(here + [ev("news", NEARBY)], S)) == 1
    assert len(find_hotspots(here + there, S)) == 2


# --- When it's critical ---


def test_critical_needs_an_emergency_or_keywords_inside_sydney():
    (fire,) = find_hotspots([ev("fire", emergency=True), ev("road"), ev("news")], S)
    assert fire.level(S) is Level.CRITICAL
    (plain,) = find_hotspots([ev("fire"), ev("road"), ev("news")], S)
    assert plain.level(S) is Level.ELEVATED
    plain.keywords = ("sydney_incident",)
    assert plain.level(S) is Level.CRITICAL
    newcastle = (-32.93, 151.78)  # outside Greater Sydney
    (far,) = find_hotspots([ev("fire", newcastle, emergency=True), ev("road", newcastle), ev("news", newcastle)], S)
    assert far.level(S) is Level.ELEVATED


def test_backlog_cant_raise_an_alert():
    (spot,) = find_hotspots([ev("fire", alertable=False), ev("road"), ev("news")], S)
    assert spot.level(S) is Level.ELEVATED and spot.level(S, alertable_only=True) is Level.NORMAL


# --- Reading items ---


def item(source, title, details, *, item_id="1"):
    return {"source_id": source, "item_id": item_id, "ts": NOW - 600, "version_seen": NOW - 600, "alertable": 1,
            "title": title, "details": details}


def test_which_items_count_as_events():
    rows = [
        item("rfs", "RFS Planned burn: X", {"lat": KELLYVILLE[0], "lon": KELLYVILLE[1], "level": "Planned burn"}),
        item("rfs", "RFS Advice: GRASS FIRE", {"lat": KELLYVILLE[0], "lon": KELLYVILLE[1], "level": "Advice"}),
        item("livetraffic", "Live Traffic: Roadwork", {"lat": KELLYVILLE[0], "lon": KELLYVILLE[1], "kind": "incident",
                                                      "planned": True, "major": False}),
        item("livetraffic", "Live Traffic: Special event", {"lat": KELLYVILLE[0], "lon": KELLYVILLE[1],
                                                           "kind": "majorevent", "planned": True}),
        item("livetraffic", "Live Traffic: Crash", {"lat": KELLYVILLE[0], "lon": KELLYVILLE[1], "kind": "incident",
                                                   "planned": False}),
        item("transit", "T9 Northern: Trains are not running between Hornsby and Strathfield", {}),
    ]
    kinds = [(e.kind, e.place) for e in _events_from_items(rows, S)]
    assert kinds == [("fire", None), ("road", None), ("trains", "Hornsby"), ("trains", "Strathfield")]


def test_the_engine_reads_stored_items_and_posts_and_keeps_post_text_private():
    db = Database(":memory:")
    for i, (source, title, details) in enumerate([
        ("rfs", "RFS Advice: GRASS FIRE, KELLYVILLE", {"lat": KELLYVILLE[0], "lon": KELLYVILLE[1], "level": "Advice"}),
        ("livetraffic", "Live Traffic: Hazard: Old Windsor Road, Kellyville",
         {"lat": KELLYVILLE[0], "lon": KELLYVILLE[1], "suburb": "Kellyville", "kind": "incident", "planned": False}),
    ]):
        db.see_item(source, str(i), ts=NOW - 600, now=NOW - 600, fresh=True, source_type="official", title=title,
                    details=details)

    class Post:
        def __init__(self, n):
            self.cell, self.lat, self.lon = cell_of(*KELLYVILLE), *KELLYVILLE
            self.at, self.title, self.place, self.alertable = NOW - 300, f"smoke over Kellyville ({n})", "Kellyville", True

    engine = Convergence(db, S, lambda: NOW, posts=lambda since: [("a", Post(1)), ("b", Post(2))])
    (spot,) = engine.update(NOW)
    assert spot.describe(S) == "Convergence at Kellyville: fire, road hazard, posts"
    views = engine.views(NOW)
    assert {v.category for v in views} == {Category.OFFICIAL, Category.SOCIAL} and all(v.derived for v in views)
    assert views[0].signals[0].scopes == (f"area:{cell_of(*KELLYVILLE)[0]},{cell_of(*KELLYVILLE)[1]}",)
    tile = engine.assessment(NOW)
    shown = [e["title"] for e in tile.details["hotspots"][0]["events"] if e["kind"] == "social"]
    assert shown == ["A Bluesky post", "A Bluesky post"]
    assert json.dumps(tile.details).count("smoke over") == 0
    assert db.events_since(0)[0]["title"] == "Convergence at Kellyville: fire, road hazard, posts"
    # Official and social, both about Kellyville: enough to make the overall status Elevated.
    assert score_overall(views, ScoringSettings())[0].level is Overall.ELEVATED
    db.close()
