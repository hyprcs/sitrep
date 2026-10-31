"""News headlines and place names. The outlets don't allow republishing their feeds, so the
feeds here are made up, in the same RSS shape the real ones use."""

from __future__ import annotations

from email.utils import format_datetime
from datetime import datetime, timezone

import httpx
import pytest

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.news import NewsAdapter, NewsFeed, NewsSettings, assess, canonical, parse_feed
from sitrep.config import Config, StorageSettings
from sitrep.db import Database
from sitrep.models import Assessment, Category, Level
from sitrep.places import gazetteer
from sitrep.runtime import Runtime
from sitrep.state import StatusBoard

NOW = 1_793_412_000.0
HOME = (-33.77878, 151.12425)
ABC = NewsFeed(name="ABC News", url="https://abc.example/justin.xml", every_minutes=10)
ABC_NSW = NewsFeed(name="ABC News NSW", url="https://abc.example/nsw.xml", nsw=True)
SMH = NewsFeed(name="Sydney Morning Herald", url="https://smh.example/nsw.xml", nsw=True, no_ai=True)


def rss(*items: tuple[str, str, float]) -> bytes:
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>{format_datetime(datetime.fromtimestamp(ts, timezone.utc))}</pubDate>"
        f"<description>Not stored.</description></item>"
        for title, link, ts in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>{body}</channel></rss>'.encode()


# --- Place names ---


@pytest.mark.parametrize("text, nsw, expected", [
    ("Two injured in M2 crash at Macquarie Park", False, ["Macquarie Park"]),
    ("Bondi Junction Westfield evacuated after stabbing", False, ["Bondi Junction"]),
    ("Liverpool beat Chelsea at Anfield", False, []),  # an English city, not the Sydney suburb
    ("Man charged after stabbing in Liverpool, NSW Police say", False, ["Liverpool"]),
    ("Epping residents told to stay indoors", True, ["Epping"]),  # a NSW-only feed is context enough
    ("Kristen Bell joins cast", False, []),  # a surname, not the NSW locality of Bell
    ("Shell shocked on the Sunshine Coast", True, []),  # Queensland, not the NSW locality of Sunshine
    ("Penrith Panthers win again", True, []),  # a football team
    ("Crash on Parramatta Road closes lanes", True, []),  # a road named after a place
    ("Sydney weather: storms on the way", False, []),  # Sydney as a whole isn't a point
    ("Flood warning near Maitland and Paterson, NSW", False, ["Maitland", "Paterson"]),
    ("The ruse that fooled the council", True, []),  # Ruse needs its capital
])
def test_place_names(text, nsw, expected):
    assert [p.name for p in gazetteer().find(text, nsw_feed=nsw)] == expected


def test_posts_need_a_sydney_clue_for_one_word_names():
    g = gazetteer()
    assert g.find("Stephen Miller says", strict=True) == []
    assert [p.name for p in g.find("Fire near Miller, Sydney", strict=True)] == ["Miller"]
    assert [p.name for p in g.find("crash at Macquarie Park", strict=True)] == ["Macquarie Park"]


def test_places_know_whether_they_are_in_greater_sydney():
    g = gazetteer()
    assert g.get("Macquarie Park").sydney and not g.get("Maitland").sydney
    assert g.get("Epping").ambiguous  # there's an Epping in Victoria too
    assert g.sydney_wide("Sydney weather: storms") and not g.sydney_wide("Storms in Brisbane")


# --- Reading feeds ---


def test_links_lose_their_tracking():
    assert canonical("https://www.smh.com.au/national/nsw/story-20261031-p5abc.html?ref=rss&utm_medium=rss#top") == (
        "https://www.smh.com.au/national/nsw/story-20261031-p5abc.html")
    assert canonical("https://example.com/a?id=7&utm_source=x") == "https://example.com/a?id=7"


def test_a_feed_is_read_with_places_and_nothing_else():
    data = rss(("Two injured in M2 crash at Macquarie Park", "https://abc.example/news/1?utm_source=rss", NOW - 600),
               ("Federal budget explained", "https://abc.example/news/2", NOW - 1200))
    crash, budget = parse_feed(data, ABC)
    assert (crash.outlet, crash.link, crash.places) == ("ABC News", "https://abc.example/news/1", ["Macquarie Park"])
    assert crash.lat == gazetteer().get("Macquarie Park").lat
    assert budget.places == [] and budget.lat is None and not budget.sydney_wide
    assert not hasattr(crash, "description")  # article text is never kept


def test_broken_feeds_are_errors():
    with pytest.raises(SourceError, match="isn't valid RSS"):
        parse_feed(b"<html>Service unavailable", ABC)


def test_assess_counts_and_maps_placed_headlines():
    headlines = parse_feed(rss(
        ("Two injured in M2 crash at Macquarie Park", "https://abc.example/1", NOW - 600),
        ("Sydney weather: storms on the way", "https://abc.example/2", NOW - 1800),
        ("Old story about Ryde", "https://abc.example/3", NOW - 3 * 86400),
    ), ABC_NSW)
    result = assess(headlines, HOME, NewsSettings(), NOW)
    assert result.level == Level.NORMAL and result.signals == ()
    assert result.reason == ("2 headlines in the last hour (ABC News NSW 2); "
                             "1 headline in the last 24 hours names a Sydney suburb")
    (point,) = result.details["map"]
    assert (point["kind"], point["title"], point["km"]) == ("news", "Two injured in M2 crash at Macquarie Park", 0.0)
    assert result.content_at == NOW - 600


# --- The adapter ---


def make(handler, now):
    db = Database(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now[0]), http, lambda: now[0])
    adapter = NewsAdapter(ctx, NewsSettings(feeds=[ABC, ABC_NSW, SMH]))
    adapter.register()
    return adapter, db, http


async def test_feeds_are_read_when_due_and_a_story_in_two_feeds_counts_once():
    asked = []
    story = ("Two injured in M2 crash at Macquarie Park", "https://abc.example/news/1", NOW - 300)
    feeds = {ABC.url: rss(story), ABC_NSW.url: rss(story), SMH.url: rss(
        ("Train delays across Sydney", "https://smh.example/story?ref=rss", NOW - 400))}

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, content=feeds[str(request.url)])

    now = [NOW]
    adapter, db, http = make(handler, now)
    first = await adapter.poll()
    assert first.details["counts"]["hour"] == 2
    items = db.query("SELECT item_id, source_type FROM items ORDER BY item_id")
    assert [dict(r) for r in items] == [
        {"item_id": "https://abc.example/news/1", "source_type": "mainstream"},
        {"item_id": "https://smh.example/story", "source_type": "mainstream"},
    ]
    asked.clear()
    now[0] += 600  # 10 minutes on: only the 10-minute feed is due
    await adapter.poll()
    assert asked == [ABC.url]
    await http.aclose()
    db.close()


async def test_one_feed_failing_is_named_and_all_failing_is_an_error():
    def one_down(request: httpx.Request) -> httpx.Response:
        if "smh" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, content=rss(("Quiet day", "https://abc.example/q", NOW - 60)))

    now = [NOW]
    adapter, db, http = make(one_down, now)
    result = await adapter.poll()
    assert result.details["feeds_failing"] == ["Sydney Morning Herald: Sydney Morning Herald's feed answered HTTP 503"]
    assert "Couldn't read Sydney Morning Herald" in result.reason
    await http.aclose()
    db.close()

    adapter, db, http = make(lambda r: httpx.Response(503), [NOW])
    with pytest.raises(SourceError, match="No news feed could be read"):
        await adapter.poll()
    await http.aclose()
    db.close()


def test_a_failing_feed_shows_in_the_gaps_panel(tmp_path):
    runtime = Runtime(Config(storage=StorageSettings(db_path=str(tmp_path / "s.db"))))
    runtime.setup()
    runtime.board.register("news", "News headlines", Category.NEWS, None, 600)
    runtime.board.report("news", Assessment(Level.NORMAL, "ok", {"feeds_failing": ["Guardian Australia: HTTP 503"]},
                                            content_at=runtime.clock()))
    gaps = [g for g in runtime.state_payload()["gaps"] if g["id"] == "news"]
    assert [g["text"] for g in gaps] == ["News headlines: Guardian Australia: HTTP 503"]
    runtime.db.close()
