"""Headlines: alerts and the dashboard lead with what's happening and where."""

from __future__ import annotations

from datetime import datetime

from sitrep.alerts import AlertPolicy
from sitrep.config import AlertsSettings, ConvergenceSettings, ScoringSettings
from sitrep.convergence import Convergence
from sitrep.db import Database
from sitrep.geo import cell_of
from sitrep.models import Category, Level, Overall, Signal
from sitrep.scoring import score_overall
from sitrep.state import SourceView
from sitrep.summary import summarise
from sitrep.telegram import format_alert
from sitrep.util import sydney_timestamp

E, C = Level.ELEVATED, Level.CRITICAL


def sig(key, level=E, headline="", title="", lines=(), emergency=False):
    return Signal(key, level, ("sydney",), title=title or key, headline=headline, lines=lines, emergency=emergency)


# --- Which signal leads ---


def test_the_most_telling_signal_leads():
    convergence = sig("convergence:29,60", headline="Gas leak, Sydney CBD", lines=("Gas leak on Elizabeth Street",))
    story = sig("cluster:abc", headline="Truck rollover closes M2 at Macquarie Park")
    term = sig("term:lamarre", headline='"lamarre" is busy')
    spike = sig("bluesky:spike", title="Spike in posts about an incident")
    assert summarise([term, spike, convergence]).headline == "Gas leak, Sydney CBD"  # not busy words or a spike
    assert summarise([convergence, story]).headline == story.headline  # a story has a real headline
    assert summarise([convergence]).lines == ("Gas leak on Elizabeth Street",)
    # Worse beats better, and an emergency beats the rest.
    rfs = sig("rfs:1", C, title="RFS Emergency Warning: Bush fire at Arcadia", emergency=True)
    assert summarise([story, rfs]).headline == rfs.title
    assert summarise([sig("aemo:lor2", title="Actual LOR2"), sig("rfs:2", title="RFS Watch and Act", emergency=True)]
                     ).headline == "RFS Watch and Act"


TOPIC = 'busy talk about "beau lamarre condon"'
SAMPLE = 'ABC News: "Beau Lamarre-Condon found guilty"'


def test_the_rest_go_on_one_line_busy_words_as_topics():
    lead = sig("convergence:1", headline="Gas leak, Sydney CBD")
    court = [sig(f"term:{w}", headline=TOPIC, lines=(SAMPLE,)) for w in ("lamarre condon", "lamarre", "guilty")]
    filler = [sig("term:north"), sig("term:making")]
    s = summarise([lead, lead, *court, *filler, sig("aemo:lor2", title="Actual LOR2 in NSW")])  # lead in 2 categories
    assert s.also == f"Actual LOR2 in NSW; {TOPIC} ({SAMPLE}); 2 other busy words"
    assert summarise([lead, *filler]).also == 'busy words "north", "making"'  # no topics: name them


def test_what_didnt_cause_the_level_is_not_linked():
    lead = sig("convergence:1", headline="Gas leak, Sydney CBD")
    court = [sig(f"term:{w}", headline=TOPIC, lines=(SAMPLE,)) for w in ("lamarre condon", "guilty")]
    s = summarise([lead, *court, sig("term:north")], linked={"convergence:1"})
    assert (s.headline, s.also, s.unlinked) == ("Gas leak, Sydney CBD", "", f"{TOPIC} ({SAMPLE}); 1 other busy word")


def test_only_busy_words():
    court = [sig(f"term:{w}", headline=TOPIC, lines=(SAMPLE,)) for w in ("lamarre condon", "guilty")]
    s = summarise([*court, sig("term:north")])
    assert (s.headline, s.lines, s.also) == ('Busy talk about "beau lamarre condon"', (SAMPLE,), 'busy words "north"')
    assert summarise([sig("term:north"), sig("term:making")]).headline == 'Busy words "north", "making"'


def test_nothing_raised_nothing_to_say():
    assert summarise([]).headline == ""
    assert score_overall([], ScoringSettings())[0].summary.headline == ""


# --- Today's alert, again ---


def at(hour, minute):
    return sydney_timestamp(datetime(2026, 11, 25, hour, minute))


NOW = at(12, 57)
CBD = (-33.8688825, 151.2104516)  # Elizabeth Street just past King Street
WOOLLOOMOOLOO = (-33.8700, 151.2220)


class Post:
    def __init__(self, text, place, when):
        self.cell, self.lat, self.lon = cell_of(*CBD), *CBD
        self.at, self.title, self.place, self.alertable, self.notable = when, text, place, True, True


COURT = [("Guardian Australia", "Beau Lamarre-Condon found guilty of murdering Jesse Baird and Luke Davies"),
         ("Sydney Morning Herald", "Lamarre-Condon found guilty of murdering Sydney couple")]


def busy_words_that_day(db):
    """The court case busy in headlines and posts, and an everyday word one account kept posting."""
    from sitrep.config import TermsSettings
    from sitrep.terms import Terms, blocklist_of, record
    block = blocklist_of(TermsSettings())
    for i, (outlet, title) in enumerate(COURT):
        db.see_item("news", f"https://example.com/{i}", ts=at(12, 30), now=at(12, 30), fresh=True,
                    source_type="mainstream", title=title, url=f"https://example.com/{i}",
                    details={"outlet": outlet, "places": [], "lat": None, "lon": None, "sydney_wide": True})
        record(db, title, at=at(12, 30), kind="news", who=outlet, fresh=True, blocklist=block)
    chatter = [Post(f"beau lamarre-condon found guilty ({i})", "", at(12, 40)) for i in range(10)]
    for i, p in enumerate(chatter):
        record(db, p.title, at=p.at, kind="social", who=f"person{i}", fresh=True, blocklist=block)
    for i in range(8):
        record(db, "making", at=at(12, 45), kind="social", who=f"bot{i % 2}", fresh=True, blocklist=block)
    db.kv_set("terms:first_seen", str(NOW - 8 * 86400))
    terms = Terms(db, TermsSettings(), lambda: NOW, posts=lambda since: chatter)
    terms.update(NOW)
    return terms


def test_the_gas_leak_on_25_november():
    """A gas leak in the CBD, a helicopter circling nearby and two people posting made a
    convergence at 12:57. The alert said "road hazard, posts, aircraft circling", followed by
    30 busy words about a court case. It now leads with what it was."""
    db = Database(":memory:")
    db.see_item("livetraffic", "incident:295635", ts=at(12, 18), now=at(10, 15), fresh=True, source_type="official",
                title="Live Traffic: Gas leak: Elizabeth Street, Sydney",
                details={"lat": CBD[0], "lon": CBD[1], "suburb": "Sydney", "road": "Elizabeth Street",
                         "kind": "incident", "planned": False, "major": False, "what": "Gas leak",
                         "location": "just past King Street",
                         "attending": ["Emergency services", "Transport for NSW", "Utility Company"]})
    db.see_item("adsb", "7c1234", ts=at(12, 19), now=at(12, 19), fresh=True, source_type="sensor",
                title="Aircraft MDIA28 (AS50) circling near Rushcutters Bay for 39 minutes",
                details={"lat": WOOLLOOMOOLOO[0], "lon": WOOLLOOMOOLOO[1], "orbit": True, "helicopter": False})
    posts = [("a", Post("what's going on at moore park", "Moore Park", at(12, 8))),
             ("b", Post("smell of gas all down elizabeth st", "Sydney", at(12, 57)))]
    engine = Convergence(db, ConvergenceSettings(), lambda: NOW, posts=lambda since: posts)
    engine.update(NOW)
    terms = busy_words_that_day(db)
    views = engine.views(NOW) + terms.views(NOW)
    result, _ = score_overall(views, ScoringSettings())
    assert result.level is Overall.ELEVATED
    assert result.summary.headline == "Gas leak, Sydney CBD"

    alert = AlertPolicy(AlertsSettings()).update(result.level, result.reason, NOW, drivers=result.drivers,
                                                 summary=result.summary)
    text = format_alert(alert, AlertsSettings())
    # The court case didn't cause it (busy talk about Sydney as a whole doesn't back up one
    # CBD square), so it's on its own line, as one topic with its headline; "making" (one
    # account, over and over) is only counted.
    assert text.splitlines() == [
        "⚠️ <b>Elevated: Gas leak, Sydney CBD</b>",
        "Gas leak on Elizabeth Street just past King Street (attending: Emergency services, Transport for NSW, "
        "Utility Company), since 10:15 am.",
        "Also nearby: 2 people posting about Sydney, Moore Park; aircraft MDIA28 (AS50) circling near "
        "Rushcutters Bay for 39 minutes.",
        'Not linked: busy talk about "lamarre condon found guilty" (Guardian Australia: "Beau Lamarre-Condon '
        'found guilty of murdering Jesse Baird and Luke Davies"); 1 other busy word.',
        "Since 12:57 pm.",
    ]
    assert "smell of gas" not in text and "moore park" not in text and "(3)" not in text  # never a post's text
    db.close()


# --- Convergence wording ---

KELLYVILLE = (-33.7080, 150.9510)


def row(source, title, details, *, item_id="1", first_seen=None):
    return {"source_id": source, "item_id": item_id, "ts": NOW - 600, "first_seen": first_seen, "version_seen": NOW - 600,
            "alertable": 1, "title": title, "details": {"lat": KELLYVILLE[0], "lon": KELLYVILLE[1], **details}}


def spot_of(rows, people=2):
    from sitrep.convergence import Event, _events_from_items, find_hotspots
    s = ConvergenceSettings()
    events = _events_from_items(rows, s) + [
        Event("social", cell_of(*KELLYVILLE), *KELLYVILLE, NOW - 60, f"post {i}", place="Kellyville", who=str(i))
        for i in range(people)]
    (spot,) = find_hotspots(events, s)
    return spot, s


def test_a_vague_hazard_gives_way_to_a_headline():
    spot, s = spot_of([
        row("livetraffic", "Live Traffic: Hazard: Old Windsor Road, Kellyville",  # saved before "what" was
            {"suburb": "Kellyville", "road": "Old Windsor Road", "kind": "incident", "planned": False}),
        row("news", "Factory fire sends smoke over Kellyville", {"places": ["Kellyville"], "outlet": "ABC News"},
            item_id="2"),
    ])
    assert spot.headline(s) == "Factory fire sends smoke over Kellyville"  # names the place: no ", Kellyville"
    assert spot.lines(s, NOW) == ("Headline from ABC News",
                                  "Also nearby: hazard on Old Windsor Road; 2 people posting about Kellyville")


def test_a_fire_says_what_kind_and_where():
    spot, s = spot_of([
        row("rfs", "RFS Advice: OLD WINDSOR RD, KELLYVILLE", {"level": "Advice", "kind": "Grass Fire"},
            first_seen=at(12, 40)),
        row("livetraffic", "Live Traffic: Smoke: Old Windsor Road, Kellyville",
            {"what": "Smoke", "road": "Old Windsor Road", "kind": "incident", "planned": False}, item_id="2"),
    ])
    assert spot.headline(s) == "Grass fire, Kellyville"
    assert spot.lines(s, NOW) == ("Grass fire at Old Windsor Rd, Kellyville (RFS Advice), since 12:40 pm",
                                  "Also nearby: smoke on Old Windsor Road; 2 people posting about Kellyville")


# --- Stories ---


def test_a_story_leads_with_its_headline_and_says_who_reports_it():
    from sitrep.stories import Member, Story, words
    def m(key, kind, title, outlet=""):
        return Member(key=key, source=key.split(":")[0], source_name="News", source_type=kind,
                      category=Category.NEWS, title=title, at=NOW - 600, url=None, words=words(title), place="Macquarie Park",
                      cell=cell_of(-33.7785, 151.1235), sydney_wide=False, notable=True, emergency=False,
                      alertable=True, outlet=outlet)
    story = Story("s1", [m("news:1", "mainstream", "Truck rollover crash closes M2", "ABC News"),
                         m("news:2", "mainstream", "M2 crash: long delays", "Sydney Morning Herald"),
                         m("livetraffic:1", "official", "Live Traffic: Crash (truck): M2 Motorway, Macquarie Park"),
                         m("bluesky:1", "social", "stuck on the m2"), m("bluesky:2", "social", "m2 is a car park")])
    assert story.headline == "Truck rollover crash closes M2, Macquarie Park"
    assert story.lines() == ("Headlines from ABC News, Sydney Morning Herald",
                             "Live Traffic: Crash (truck): M2 Motorway, Macquarie Park", "2 posts on Bluesky")


# --- The other messages ---


def test_the_all_clear_says_what_it_was():
    p = AlertPolicy(AlertsSettings(all_clear_after_minutes=30))
    leak = sig("convergence:29,60", headline="Gas leak, Sydney CBD", lines=("Gas leak on Elizabeth Street",))
    p.update(Overall.NORMAL, "calm", NOW - 3600)
    raised = p.update(Overall.ELEVATED, "Convergence: ...", NOW, drivers=(leak,))
    assert raised.summary.headline == "Gas leak, Sydney CBD"
    p.update(Overall.NORMAL, "Nothing unusual from any source", NOW + 20 * 60)
    clear = p.update(Overall.NORMAL, "Nothing unusual from any source", NOW + 50 * 60)
    assert format_alert(clear, AlertsSettings()).splitlines() == [
        "✅ <b>sitrep: All clear</b>",
        "Nothing unusual from any source.",
        "It was Elevated (Gas leak, Sydney CBD) from 12:57 pm until 1:17 pm.",
    ]


def test_what_you_were_told_is_remembered_across_a_restart(tmp_path):
    from sitrep.alerts import Alerts, Notifier

    class Quiet(Notifier):
        name = "Quiet"
        async def check(self): ...
        async def send(self, alert): ...

    db = Database(tmp_path / "a.db")
    alerts = Alerts(AlertsSettings(), db, [Quiet(db, lambda: NOW)])
    alerts.update(Overall.NORMAL, "calm", NOW - 60)
    alerts.update(Overall.ELEVATED, "...", NOW, drivers=(sig("convergence:1", headline="Gas leak, Sydney CBD"),))
    again = Alerts(AlertsSettings(), db, [Quiet(db, lambda: NOW)])
    assert (again.policy.told, again.policy.told_headline) == (Overall.ELEVATED, "Gas leak, Sydney CBD")
    db.close()


def test_another_critical_event_leads_with_its_own_headline():
    p = AlertPolicy(AlertsSettings())
    first = sig("rfs:1", C, title="RFS Emergency Warning: Bush fire at Arcadia", emergency=True)
    second = sig("convergence:2", C, headline="Explosion, Parramatta", lines=("Building fire at Church Street",))
    p.update(Overall.CRITICAL, "...", NOW, drivers=(first,))
    p.update(Overall.CRITICAL, "...", NOW + 30, drivers=(first, second))
    another = p.update(Overall.CRITICAL, "...", NOW + 60, drivers=(first, second))
    assert format_alert(another, AlertsSettings()).splitlines() == [
        "\U0001f6d1 <b>Critical, another event: Explosion, Parramatta</b>",
        "Building fire at Church Street.",
        "Still critical since 12:57 pm.",
    ]
