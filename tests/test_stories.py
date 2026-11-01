"""Story clusters: which items group together, what counts as corroboration, and privacy."""

from __future__ import annotations

import json

import pytest

from sitrep.adapters.base import AdapterContext
from sitrep.adapters.bluesky import BlueskyAdapter, BlueskySettings, Post
from sitrep.config import Config
from sitrep.db import Database
from sitrep.geo import cell_of
from sitrep.models import Category, Level
from sitrep.state import StatusBoard
from sitrep.stories import Member, Stories, group, words

NOW = 1_793_412_000.0
M2 = (-33.7785, 151.1235)
CBD = (-33.8688, 151.2093)
FAR = (-33.6203, 151.0436)  # Arcadia, ~20 km away


def member(key, kind, title, where=M2, *, at=NOW - 600, notable=True, emergency=False, alertable=True, place=None,
           category=None):
    category = category or {"official": Category.OFFICIAL, "mainstream": Category.NEWS, "social": Category.SOCIAL}[kind]
    cell = cell_of(*where) if where else None
    return Member(key=key, source=key.split(":")[0], source_name=key.split(":")[0], source_type=kind, category=category,
                  title=title, at=at, url=None, words=words(title, [place] if place else []), place=place, cell=cell,
                  sydney_wide=where is None, notable=notable, emergency=emergency, alertable=alertable,
                  lat=where[0] if where else None, lon=where[1] if where else None)


def stories_of(*members, keywords=None):
    s = Stories(Database(":memory:"), lambda: NOW, keyword_groups=keywords or (lambda text: []),
                social=lambda since: list(members))
    s.update(NOW)
    return s


# --- Words ---


def test_words_fold_events_and_drop_places_and_filler():
    assert words("Two injured as bushfire threatens homes at Lane Cove", ["Lane Cove"]) == {"injured", "fire", "threaten", "home"}
    assert words("Truck rollover crash closes M2") == {"truck", "crash", "close", "m2"}
    assert "police" in words("Police hunt gunman") and "shooting" in words("Police hunt gunman")


# --- Grouping ---


def test_same_place_and_an_event_word_makes_one_story():
    (story,) = group([
        member("livetraffic:1", "official", "Live Traffic: Crash: M2 Motorway, Macquarie Park"),
        member("news:a", "mainstream", "Truck rollover crash closes M2 at Macquarie Park", place="Macquarie Park"),
    ])
    assert len(story.members) == 2


def test_different_places_or_unrelated_words_stay_apart():
    stories = group([
        member("livetraffic:1", "official", "Live Traffic: Crash: M2 Motorway, Macquarie Park"),
        member("news:a", "mainstream", "Crash closes road at Arcadia", where=FAR),  # same event word, 20 km away
        member("news:b", "mainstream", "New cafe opens at Macquarie Park", place="Macquarie Park"),  # same place, no overlap
    ])
    assert len(stories) == 3


def test_sydney_wide_items_group_with_each_other_only():
    stories = group([
        member("transit:1", "official", "the network: Trains are not running due to a power supply issue", where=None),
        member("news:a", "mainstream", "Sydney trains suspended after power failure", where=None),
        member("news:b", "mainstream", "Trains crash at Macquarie Park"),
    ])
    assert [len(s.members) for s in stories] == [2, 1]


def test_posts_are_matched_against_the_headline_not_other_posts():
    # From 1 Nov 2026: a thunderstorm post joined a story about police rifles because it shared
    # "eastern" and "far" with a post that had joined through the headline.
    stories = group([
        member("news:a", "mainstream", "NSW police will be armed with military-style rifles from Sunday",
               where=None, at=NOW - 900),
        member("bluesky:1", "social", "Minns has given machine guns and military rifles to police in the far eastern suburbs",
               where=None, at=NOW - 800),
        member("bluesky:2", "social", "Thunderstorms stretched far and wide across eastern Australia", where=None, at=NOW - 700),
    ])
    assert [[m.key for m in s.members] for s in stories] == [["news:a", "bluesky:1"], ["bluesky:2"]]


def test_a_story_of_posts_only_is_matched_against_its_first_post():
    stories = group([
        member("bluesky:1", "social", "smoke and fire near the station", CBD, at=NOW - 900),
        member("bluesky:2", "social", "fire trucks lined up everywhere", CBD, at=NOW - 800),
        member("bluesky:3", "social", "food trucks lined up for the festival", CBD, at=NOW - 700),
        member("news:a", "mainstream", "Fire breaks out near Town Hall station", CBD, at=NOW - 600),
    ])
    assert [[m.key for m in s.members] for s in stories] == [["bluesky:1", "bluesky:2", "news:a"], ["bluesky:3"]]


def test_police_alone_only_ties_together_items_pinned_to_a_place():
    sydney_wide = group([
        member("news:a", "mainstream", "NSW police will be armed with military-style rifles from Sunday", where=None),
        member("news:b", "mainstream", "Police car pursuit ends on golf course in Sydney's east", where=None),
    ])
    assert len(sydney_wide) == 2
    (placed,) = group([
        member("news:a", "mainstream", "Heavy police presence at a CBD shopping centre", CBD),
        member("bluesky:1", "social", "loads of officers near Westfield right now", CBD),
    ])
    assert len(placed.members) == 2


def test_a_story_keeps_its_id_as_it_grows():
    first = group([member("livetraffic:1", "official", "Crash: M2 Motorway")])[0].id
    grown = group([member("livetraffic:1", "official", "Crash: M2 Motorway"),
                   member("news:a", "mainstream", "M2 crash causes delays", at=NOW)])[0]
    assert grown.id == first


# --- Corroboration and levels ---


def test_kinds_not_outlets_and_routine_official_items_dont_count():
    s = stories_of(
        member("news:a", "mainstream", "M2 crash at Macquarie Park"),
        member("news:b", "mainstream", "Crash on the M2 at Macquarie Park"),
        member("livetraffic:9", "official", "Crash: M2 Motorway, Macquarie Park", notable=False),
    )
    (story,) = s.stories
    assert story.kinds() == {"mainstream"} and story.level() is Level.NORMAL
    assert s.views(NOW) == []


def test_two_kinds_are_elevated_in_both_categories_with_the_story_as_scope():
    s = stories_of(member("livetraffic:1", "official", "Crash: M2 Motorway, Macquarie Park"),
                   member("news:a", "mainstream", "M2 crash at Macquarie Park"))
    views = {v.category: v for v in s.views(NOW)}
    assert set(views) == {Category.OFFICIAL, Category.NEWS}
    (signal,) = views[Category.NEWS].signals
    story = s.stories[0]
    assert signal.key == f"cluster:{story.id}" and signal.scopes == (f"cluster:{story.id}", f"area:{cell_of(*M2)[0]},{cell_of(*M2)[1]}")
    assert signal.level is Level.ELEVATED


def test_triangulated_isnt_critical_without_an_emergency_or_keywords():
    s = stories_of(member("livetraffic:1", "official", "Crash: M2 Motorway, Macquarie Park"),
                   member("news:a", "mainstream", "M2 crash at Macquarie Park"),
                   member("bluesky:x", "social", "huge crash on the M2"))
    (story,) = s.stories
    assert story.triangulated and story.level() is Level.ELEVATED


def test_triangulated_with_an_emergency_item_or_keywords_is_critical():
    fire = stories_of(member("rfs:1", "official", "RFS Emergency Warning: ARCADIA FIRE", FAR, emergency=True),
                      member("news:a", "mainstream", "Bushfire threatens homes at Arcadia", FAR),
                      member("bluesky:x", "social", "fire right behind the houses", FAR))
    assert fire.stories[0].level() is Level.CRITICAL
    lockdown = stories_of(member("livetraffic:1", "official", "Police operation: George Street", CBD),
                          member("news:a", "mainstream", "Shots fired as Sydney CBD goes into lockdown", CBD),
                          member("bluesky:x", "social", "in lockdown, police everywhere", CBD),
                          keywords=lambda text: ["sydney_incident"] if "lockdown" in text else [])
    assert lockdown.stories[0].level() is Level.CRITICAL and lockdown.stories[0].keywords == ("sydney_incident",)


def test_old_stories_stop_counting_but_stay_on_show():
    s = stories_of(member("livetraffic:1", "official", "Crash: M2 Motorway", at=NOW - 3 * 3600),
                   member("news:a", "mainstream", "M2 crash at Macquarie Park", at=NOW - 3 * 3600 + 60))
    assert s.views(NOW) == []
    assert s.assessment(NOW).details["stories"][0]["active"] is False


def test_posts_sharing_a_link_join_but_dont_count_as_social():
    # From 1 Nov 2026: a bot reposting a headline with a link card made a "news + social" story.
    s = stories_of(member("news:a", "mainstream", "Police car pursuit ends on golf course in Sydney's east", where=None),
                   member("bluesky:bot", "social", "Two charged after police chase ends on golf course in Sydney's east",
                          where=None, notable=False))
    (story,) = s.stories
    assert len(story.members) == 2 and story.kinds() == {"mainstream"}
    assert s.views(NOW) == []


def test_backlog_cant_raise_an_alert():
    s = stories_of(member("livetraffic:1", "official", "Crash: M2 Motorway", alertable=False),
                   member("news:a", "mainstream", "M2 crash at Macquarie Park"))
    assert [v.level for v in s.views(NOW)] == [Level.ELEVATED, Level.ELEVATED]  # the dashboard sees it
    assert s.views(NOW, alertable_only=True) == []  # alerts don't


# --- Saving, privacy and the timeline ---


def test_saved_stories_never_keep_a_posts_text():
    db = Database(":memory:")
    s = Stories(db, lambda: NOW, social=lambda since: [member("bluesky:at://did:plc:abc/post/1", "social",
                                                                "my neighbour's car got hit in the M2 crash")])
    db.see_item("news", "https://abc.example/1", ts=NOW - 300, now=NOW, fresh=True, source_type="mainstream",
                title="M2 crash at Macquarie Park",
                details={"lat": M2[0], "lon": M2[1], "places": ["Macquarie Park"], "outlet": "ABC News"})
    s.update(NOW)
    saved = json.loads(db.query("SELECT members FROM clusters")[0]["members"])
    social = next(m for m in saved if m["type"] == "social")
    assert social["title"] == "A Bluesky post" and social["url"] is None and "did:plc" not in json.dumps(saved)
    titles = [e["title"] for e in db.events_since(0)]
    assert titles == ["Story: M2 crash at Macquarie Park"]
    db.close()


def test_bluesky_keeps_sydney_posts_in_memory_and_forgets_deleted_ones():
    db = Database(":memory:")
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: NOW), None, lambda: NOW)
    adapter = BlueskyAdapter(ctx, BlueskySettings())
    for i, (text, link) in enumerate([("huge crash on the M2 at Macquarie Park", False), ("lovely day in Perth", False),
                                      ("storms hitting Sydney now", False), ("NSW police to carry rifles", False),
                                      ("Storm damage across Sydney (news link)", True)]):
        adapter._remember_for_stories(Post(uri=f"at://did:plc:u{i}/app.bsky.feed.post/{i}", author=f"did:plc:u{i}",
                                           time=NOW - 60, text=text, match_text=text, url=f"https://bsky.app/{i}",
                                           shares_link=link))
    kept = adapter.recent_posts(NOW - 3600)
    # Perth isn't Sydney, and naming only NSW isn't naming Sydney. The link post is kept, but doesn't count.
    assert [(m.place, m.sydney_wide, m.notable) for m in kept] == [
        ("Macquarie Park", False, True), (None, True, True), (None, True, False)]
    adapter._forget_for_stories(lambda author, m: author == "did:plc:u0")
    assert [m.title for m in adapter.recent_posts(NOW - 3600)] == [
        "storms hitting Sydney now", "Storm damage across Sydney (news link)"]
    db.close()


@pytest.mark.parametrize("title", ["Story in Macquarie Park (official + news): M2 crash at Macquarie Park"])
def test_descriptions(title):
    s = stories_of(member("livetraffic:1", "official", "Crash: M2 Motorway, Macquarie Park", place="Macquarie Park"),
                   member("news:a", "mainstream", "M2 crash at Macquarie Park", place="Macquarie Park"))
    assert s.stories[0].describe() == title
