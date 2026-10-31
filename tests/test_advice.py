"""Foreign travel advice. The FCDO page is real, trimmed (GOV.UK, Open Government Licence
v3.0); the US advisories are made up, in the shape cadataapi.state.gov sends."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from sitrep.adapters.advice import (
    UK_NONE, UK_PAGE, AdviceAdapter, AdviceSettings, compile_words, parse_uk, parse_us, uk_label,
)
from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.config import Config
from sitrep.db import Database
from sitrep.geo import cell_of
from sitrep.models import Category, Level, area, national
from sitrep.places import gazetteer
from sitrep.state import StatusBoard
from sitrep.stories import member_from_item

FIXTURES = Path(__file__).parent / "fixtures" / "advice"
NOW = 1_793_412_000.0
WORDS = compile_words(AdviceSettings().warning_words)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def us_doc(level=1, label="Exercise Normal Precautions", updated="2026-08-17T20:00:00-04:00"):
    docs = json.loads((FIXTURES / "us_advisories.json").read_text(encoding="utf-8"))
    for d in docs:
        if d["Category"] == ["AS"]:
            d["Title"], d["Updated"] = f"Australia - Level {level}: {label}", updated
    return docs


def uk_doc(*notes: tuple[str, float], statuses=()):
    doc = json.loads((FIXTURES / "fcdo_australia.json").read_text(encoding="utf-8"))
    doc["details"]["alert_status"] = list(statuses)
    history = [{"note": note, "public_timestamp": iso(ts)} for note, ts in notes]
    doc["details"]["change_history"] = history + doc["details"]["change_history"]
    return doc


# --- Reading the advice ---


def test_the_us_level_comes_from_australias_entry():
    us = parse_us(us_doc(), "AS")
    assert (us.level, us.label, us.issued, us.link) == (
        1, "Exercise Normal Precautions", "17 Aug 2026", "https://travel.example/australia.html")
    assert us.feed_at == datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()  # the newest, for any country


def test_us_problems_are_errors():
    with pytest.raises(SourceError, match="no entry for Australia"):
        parse_us([d for d in us_doc() if d["Category"] != ["AS"]], "AS")
    broken = us_doc()
    broken[1]["Title"] = "Australia travel advisory"
    with pytest.raises(SourceError, match="Couldn't find a level"):
        parse_us(broken, "AS")


def test_the_real_fcdo_page():
    uk = parse_uk(uk_doc(), WORDS)
    assert (uk.rank, uk.label, len(uk.updates)) == (0, UK_NONE, 12)
    assert uk.updates[0].note.startswith("Updated information about global travel impacts")
    flagged = [u for u in uk.updates if u.warnings]
    assert [u.warnings for u in flagged] == [("terrorist", "attack"), ("terrorist", "attack"), ("shooting",),
                                              ("shooting",)]
    # The December 2025 attack is placed at Bondi Beach; the FCDO's "Safety and security" page isn't news.
    assert all(u.suburb == "Bondi Beach" and u.scope == area(*cell_of(u.lat, u.lon)) for u in flagged)
    cyclone = next(u for u in uk.updates if "cyclone Alfred ('Safety" in u.note)
    assert cyclone.warnings == ()


def test_uk_statuses_are_ranked():
    assert uk_label(()) == UK_NONE
    uk = parse_uk(uk_doc(statuses=["avoid_all_travel_to_parts", "avoid_all_but_essential_travel_to_whole_country"]),
                  WORDS)
    assert uk.rank == 3 and uk.label == "advises against all travel to parts of Australia"
    with pytest.raises(SourceError, match="no alert status"):
        parse_uk({"details": {}}, WORDS)


# --- The adapter ---


class Server:
    """Answers like the two governments' sites, with GOV.UK's "has it changed?" check."""

    def __init__(self):
        self.us, self.uk = us_doc(), uk_doc()
        self.us_status = self.uk_status = 200
        self.asked: list[tuple[str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "gov.uk" in request.url.host:
            body = json.dumps(self.uk).encode()
            etag = f'W/"{hashlib.md5(body).hexdigest()}"'
            self.asked.append(("uk", request.headers.get("If-None-Match")))
            if self.uk_status != 200:
                return httpx.Response(self.uk_status)
            if request.headers.get("If-None-Match") == etag:
                return httpx.Response(304)
            return httpx.Response(200, content=body, headers={"ETag": etag})
        self.asked.append(("us", None))
        return httpx.Response(self.us_status, json=self.us) if self.us_status == 200 else httpx.Response(self.us_status)


def make(server, now, db=None):
    db = db or Database(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now[0]), http, lambda: now[0])
    adapter = AdviceAdapter(ctx, AdviceSettings())
    adapter.register()
    return adapter, db, http


async def check(adapter, now, hours=1):
    """One hourly check, as the adapter's own loop would run it."""
    now[0] += hours * 3600
    result = await adapter.poll()
    adapter.report(result)
    return result


async def test_a_raised_us_level_is_critical_for_a_day_then_lowering_it_is_info():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    first = await check(adapter, now, 0)
    assert first.level is Level.NORMAL
    assert first.reason.startswith("US: Level 1, exercise normal precautions (issued 17 Aug 2026); UK: " + UK_NONE)

    server.us = us_doc(2, "Exercise Increased Caution")
    raised = await check(adapter, now)
    assert raised.level is Level.CRITICAL
    assert raised.reason.startswith("The US raised its travel advisory for Australia from Level 1 to Level 2: "
                                    "Exercise Increased Caution")
    (signal,) = raised.signals
    assert (signal.key, signal.scopes, signal.alertable) == ("advice:us:level2", (national("security"),), True)
    assert db.events_since(0)[0]["level"] == "critical"

    assert (await check(adapter, now, 23)).level is Level.CRITICAL
    later = await check(adapter, now, 2)  # more than 24 hours since sitrep noticed
    assert later.level is Level.NORMAL and later.reason.startswith("US: Level 2, exercise increased caution")

    server.us = us_doc(1)
    lowered = await check(adapter, now)
    assert lowered.level is Level.NORMAL
    assert lowered.reason.startswith("The US lowered its travel advisory for Australia from Level 2 to Level 1")
    assert [e["level"] for e in db.events_since(0)] == ["info", "critical"]  # newest first
    await http.aclose()
    db.close()


async def test_a_change_found_after_a_restart_shows_but_cant_alert():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    await check(adapter, now, 0)
    await http.aclose()

    server.us = us_doc(3, "Reconsider Travel")
    restarted, _, http = make(server, now, db)  # a new process: it hasn't looked at the source yet
    result = await check(restarted, now, 5)
    assert result.level is Level.CRITICAL and not result.signals[0].alertable
    await http.aclose()
    db.close()


async def test_a_reissue_at_the_same_level_is_only_on_the_timeline():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    await check(adapter, now, 0)
    server.us = us_doc(updated="2026-10-30T20:00:00-04:00")
    result = await check(adapter, now)
    assert result.level is Level.NORMAL and "issued 30 Oct 2026" in result.reason
    (event,) = db.events_since(0)
    assert (event["level"], event["title"]) == (
        "info", "The US reissued its travel advisory for Australia, still Level 1: Exercise Normal Precautions")
    await http.aclose()
    db.close()


async def test_an_fcdo_update_about_security_is_elevated_and_about_its_place():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    await check(adapter, now, 0)
    note = "Information about a shooting in Bondi Beach, Sydney ('Warnings and insurance' page)."
    server.uk = uk_doc((note, NOW + 3000))
    result = await check(adapter, now)
    assert result.level is Level.ELEVATED and "mentions shooting" in result.reason
    (signal,) = result.signals
    bondi = gazetteer().get("Bondi Beach")
    assert signal.scopes == (area(*cell_of(bondi.lat, bondi.lon)),) and signal.alertable
    # Stored with its place, so it can join a story about Bondi as an official source.
    row = dict(db.query("SELECT * FROM items WHERE source_id = 'advice' AND title LIKE '%Bondi%'")[0])
    member = member_from_item({**row, "details": json.loads(row["details"])},
                              {"advice": ("Foreign travel advice", Category.OFFICIAL)})
    assert member.place == "Bondi Beach" and member.notable and member.source_type == "official"

    routine = "New information about the bushfire season ('Safety and security' page)."
    server.uk = uk_doc((routine, now[0] + 1800))
    quiet = await check(adapter, now)
    assert quiet.level is Level.NORMAL and quiet.details["updates"][0]["warnings"] == []
    await http.aclose()
    db.close()


async def test_the_uk_advising_against_travel_is_critical():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    await check(adapter, now, 0)
    server.uk = uk_doc(statuses=["avoid_all_but_essential_travel_to_parts"])
    result = await check(adapter, now)
    assert result.level is Level.CRITICAL
    assert result.signals[0].title == "The UK FCDO now advises against all but essential travel to parts of Australia"
    await http.aclose()
    db.close()


async def test_one_government_failing_is_named_and_both_failing_is_an_error():
    server, now = Server(), [NOW]
    server.us_status = 403
    adapter, db, http = make(server, now)
    result = await check(adapter, now, 0)
    assert result.details["feeds_failing"] == ["US State Department: it answered HTTP 403"]
    assert result.reason.startswith("UK: ") and "Couldn't read US State Department" in result.reason
    server.uk_status = 503
    with pytest.raises(SourceError, match="Couldn't read either government's advice"):
        await adapter.poll()
    await http.aclose()
    db.close()


async def test_gov_uk_is_asked_whether_it_changed():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    await check(adapter, now, 0)
    second = await check(adapter, now)
    uk_asks = [etag for who, etag in server.asked if who == "uk"]
    assert uk_asks[0] is None and uk_asks[1].startswith('W/"')  # the second answer was 304, with no body
    assert second.details["uk"]["link"] == UK_PAGE and second.details["uk"]["label"] == UK_NONE
    await http.aclose()
    db.close()
