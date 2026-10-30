"""Bluesky: keyword matching, Jetstream messages, the spike test, and the adapter end to end.

Every post here is made up. The message shapes copy what Jetstream v2 sent on
29 October 2026 (an envelope with "$type": "message" and a "payload"), but no real
person's post or account is stored in the tests.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.server import serve

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.bluesky import (
    BlueskyAdapter,
    BlueskySettings,
    GroupView,
    KeywordGroup,
    assess,
    compile_groups,
    judge_group,
    matching_groups,
    poisson_tail,
    read_message,
    usual_rate,
)
from sitrep.config import Config, load_config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.runtime import build_adapter_specs
from sitrep.state import StatusBoard

SETTINGS = BlueskySettings()
GROUPS = compile_groups(SETTINGS.groups)
T0 = 1793250000.0  # 2026-10-29 05:00 UTC, on a minute (and hour) boundary


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def commit(seq: int, ts: float, did: str, rkey: str, text: str = "", op: str = "create", title: str | None = None) -> str:
    payload = {"$type": "network.bsky.jetstream.subscribeEvents#commit", "collection": "app.bsky.feed.post",
               "did": did, "operation": op, "rev": "3abc", "rkey": rkey, "seq": seq, "time": iso(ts)}
    if op == "create":
        record = {"$type": "app.bsky.feed.post", "createdAt": iso(ts), "langs": ["en"], "text": text}
        if title:
            record["embed"] = {"$type": "app.bsky.embed.external", "external": {"title": title, "uri": "https://example.com/"}}
        payload |= {"cid": "bafyfake", "record": record}
    return json.dumps({"$type": "message", "payload": payload})


def account(seq: int, ts: float, did: str, active: bool, status: str | None = None) -> str:
    acct = {"active": active, "did": did, "seq": 1, "time": iso(ts)} | ({"status": status} if status else {})
    return json.dumps({"$type": "message", "payload": {"$type": "network.bsky.jetstream.subscribeEvents#account",
                                                       "account": acct, "did": did, "seq": seq, "time": iso(ts)}})


# --- Matching ---


@pytest.mark.parametrize("text, groups", [
    ("Telstra is down again, anyone else?", ["telco_down"]),
    ("Telstra download speeds are great", []),  # "down" is a whole word, not "download"
    ("#Sydney evacuation underway near Central", ["sydney_incident"]),
    ("Massive NBN outage in Sydney right now", ["telco_down"]),
    ("My internet is down. Sydney, are you OK?", ["internet_down"]),
    ("internet   down    across Australia", ["internet_down"]),
    ("Ransomware attack on an Australian hospital", ["cyber_attack"]),
    ("Ransomware attack on a hospital in Ohio", []),
    ("Lovely day in Sydney", []),
])
def test_keyword_groups(text, groups):
    assert matching_groups(text, GROUPS) == groups


def test_star_matches_the_start_of_a_word():
    compiled = compile_groups([KeywordGroup(id="g", name="G", all_of=[["evacuat*"], ["Sydney"]])])
    assert matching_groups("Sydney: people evacuating now", compiled) == ["g"]
    assert matching_groups("Sydney: reevacuated", compiled) == []  # still from the start of a word


# --- Reading Jetstream ---


def test_reading_messages():
    created = read_message(commit(10, T0, "did:plc:a", "r1", "Optus down", title="Optus outage hits millions"))
    assert created.kind == "post" and created.seq == 10 and created.time == T0
    assert created.post.uri == "at://did:plc:a/app.bsky.feed.post/r1"
    assert created.post.url == "https://bsky.app/profile/did:plc:a/post/r1"
    assert "Optus outage hits millions" in created.post.match_text and created.post.text == "Optus down"

    deleted = read_message(commit(11, T0, "did:plc:a", "r1", op="delete"))
    assert (deleted.kind, deleted.uri) == ("delete", "at://did:plc:a/app.bsky.feed.post/r1")

    assert read_message(account(12, T0, "did:plc:a", False, "deleted")).kind == "gone"
    assert read_message(account(13, T0, "did:plc:a", False, "takendown")).author == "did:plc:a"
    assert read_message(account(14, T0, "did:plc:a", False, "throttled")).kind == "other"  # not gone, just slowed
    assert read_message(account(15, T0, "did:plc:a", True)).kind == "other"

    info = read_message(json.dumps({"$type": "message", "payload": {
        "$type": "network.bsky.jetstream.subscribeEvents#info", "name": "FutureCursor", "message": "starting at the live tip"}}))
    assert info.kind == "info" and info.info == "FutureCursor: starting at the live tip"
    assert read_message("not json").kind == "other"


# --- The spike test ---


def test_poisson_tail_matches_the_formula():
    def exact(k, lam):
        return 1 - sum(math.exp(-lam) * lam**i / math.factorial(i) for i in range(k))
    for k, lam in [(1, 0.5), (5, 0.5), (3, 2.0), (20, 10.0), (8, 12.0)]:
        assert poisson_tail(k, lam) == pytest.approx(exact(k, lam), rel=1e-9, abs=1e-15)
    assert poisson_tail(0, 3) == 1.0
    assert poisson_tail(40, 0.5) < 1e-50


def test_usual_rate_uses_the_same_hour_in_earlier_weeks():
    hour = int(T0 // 3600 * 3600)
    week = 7 * 86400
    counts, minutes = {}, {}
    for w in (1, 2):
        for offset in (-3600, 0, 3600):
            minutes[hour - w * week + offset] = 60
            counts[hour - w * week + offset] = 12  # 12 people an hour, 0.2 a minute
    minutes[hour - 3600] = 60  # an hour earlier today: not the same hour of the week
    counts[hour - 3600] = 600
    assert usual_rate(counts, minutes, T0 + 120, weeks=4) == pytest.approx(0.2)


def test_usual_rate_falls_back_to_last_week_then_to_nothing():
    hour = int(T0 // 3600 * 3600)
    minutes = {hour - 2 * 3600: 60, hour - 3 * 3600: 30}
    counts = {hour - 2 * 3600: 9, hour - 3 * 3600: 0}
    assert usual_rate(counts, minutes, T0, weeks=4) == pytest.approx(0.1)
    assert usual_rate({}, {hour - 3600: 30}, T0, weeks=4) is None


def test_a_spike_needs_long_odds_and_enough_people():
    group = SETTINGS.groups[1]
    assert not judge_group(group, 5, 0.0, SETTINGS).spike  # 5 people when 0.5 is usual: odds 1 in 6,000
    eight = judge_group(group, 8, 0.0, SETTINGS)
    assert eight.spike and eight.usual == 0.5  # 8 people: odds 1 in 100 million
    busy = judge_group(group, 30, 4.0, SETTINGS)  # 20 usual per 5 minutes
    assert not busy.spike
    assert judge_group(group, 60, 4.0, SETTINGS).spike


def view(name="Telco down", people=0, usual=0.5, spike=False):
    return GroupView(name.lower().replace(" ", "_"), name, people, usual, 0.5, spike)


def test_reasons():
    calm = assess([view(people=1), view("Internet down in Australia", people=0)], SETTINGS,
                  learning_left=0, behind=60, posts_per_second=25)
    assert calm.level == Level.NORMAL
    assert calm.reason == "No spikes. Busiest: telco down, 1 person in the last 5 minutes (usually about 0.5)"
    spike = assess([view(people=34, usual=2.0, spike=True)], SETTINGS, learning_left=0, behind=60, posts_per_second=25)
    assert spike.level == Level.ELEVATED
    assert spike.reason == "Spike in posts about telco down: 34 people in 5 minutes, usually about 2.0"
    learning = assess([view(people=3)], SETTINGS, learning_left=2.5 * 86400, behind=60, posts_per_second=25)
    assert learning.level == Level.LEARNING and learning.reason.startswith("Learning what's usual: 2.5 days to go.")
    behind = assess([view()], SETTINGS, learning_left=0, behind=1800, posts_per_second=25)
    assert behind.reason.startswith("Catching up after a break (30 minutes behind).")


def test_shipped_config_is_valid():
    cfg = load_config(Path(__file__).parent.parent / "config.yaml", env_file=None)
    bluesky = next(s for cls, s in build_adapter_specs(cfg) if cls.id == "bluesky")
    assert [g.id for g in bluesky.groups] == ["sydney_incident", "telco_down", "internet_down", "cyber_attack"]
    assert bluesky.spike_probability == 1e-6


# --- The adapter ---


@pytest.fixture
def make_adapter():
    made = []

    def make(clock, settings=SETTINGS):
        db = Database(":memory:")
        made.append(db)
        ctx = AdapterContext(Config(), db, StatusBoard(db, clock), httpx.AsyncClient(), clock)
        adapter = BlueskyAdapter(ctx, settings)
        adapter.reports = []
        adapter.report = adapter.reports.append
        return adapter, db

    yield make
    for db in made:
        db.close()


def test_counts_people_once_per_minute_and_keeps_samples(make_adapter):
    now = [T0]
    adapter, db = make_adapter(lambda: now[0])
    messages = [
        commit(1, T0 + 5, "did:plc:a", "r1", "Telstra is down"),
        commit(2, T0 + 6, "did:plc:a", "r2", "Telstra still down"),  # same person, same minute
        commit(2, T0 + 6, "did:plc:a", "r2", "Telstra still down"),  # a resent event
        commit(3, T0 + 7, "did:plc:b", "r3", "Optus outage?"),
        commit(4, T0 + 8, "did:plc:c", "r4", "nice weather"),
        commit(5, T0 + 65, "did:plc:d", "r5", "NBN not working"),
        commit(6, T0 + 220, "did:plc:e", "r6", "unrelated"),  # moves time on so both minutes are written
    ]
    for raw in messages:
        now[0] = read_message(raw).time
        adapter.handle(raw)
    assert db.counts_since("bluesky", "telco_down", 0) == [(int(T0), 2.0), (int(T0) + 60, 1.0)]
    assert db.counts_since("bluesky", "_posts", 0) == [(int(T0), 4.0), (int(T0) + 60, 1.0)]
    assert [s["text"] for s in db.samples("bluesky", "telco_down")] == [
        "NBN not working", "Optus outage?", "Telstra still down", "Telstra is down"]
    assert adapter.reports and adapter.reports[-1].level == Level.LEARNING


def test_samples_go_when_their_author_deletes_them(make_adapter):
    adapter, db = make_adapter(lambda: T0)
    adapter.handle(commit(1, T0, "did:plc:a", "r1", "Telstra is down"))
    adapter.handle(commit(2, T0, "did:plc:b", "r2", "Optus is down"))
    adapter.handle(commit(3, T0, "did:plc:c", "r3", "TPG is down"))
    adapter.handle(commit(4, T0 + 1, "did:plc:a", "r1", op="delete"))
    adapter.handle(account(5, T0 + 2, "did:plc:b", False, "deleted"))
    adapter.handle(account(6, T0 + 3, "did:plc:c", False, "throttled"))
    assert [s["text"] for s in db.samples("bluesky", "telco_down")] == ["TPG is down"]


def test_only_a_small_sample_is_kept(make_adapter):
    adapter, db = make_adapter(lambda: T0, BlueskySettings(samples_per_group=3))
    for i in range(10):
        adapter.handle(commit(i + 1, T0 + i, f"did:plc:{i}", f"r{i}", f"Telstra down {i}"))
    assert [s["text"] for s in db.samples("bluesky", "telco_down")] == ["Telstra down 9", "Telstra down 8", "Telstra down 7"]


def test_cursor_resumes_from_the_oldest_minute_not_yet_written(make_adapter):
    now = [T0]
    adapter, db = make_adapter(lambda: now[0])
    for raw in (commit(100, T0 + 1, "did:plc:a", "r1", "hi"), commit(200, T0 + 61, "did:plc:a", "r2", "hi"),
                commit(300, T0 + 155, "did:plc:a", "r3", "hi")):
        now[0] = read_message(raw).time
        adapter.handle(raw)
    # The first minute is written; the second (from seq 200) isn't yet.
    assert json.loads(db.kv_get("bluesky:cursor")) == {"seq": 200, "time": T0 + 60}
    assert adapter.url().endswith("&cursor=200")
    now[0] += 3 * 3600  # after a long break, start from now
    assert "cursor=" not in adapter.url()


def seed_history(db: Database, upto: float, people_per_hour: float) -> None:
    """Four weeks of quiet history, as if sitrep had been running all along."""
    hour = int(upto // 3600 * 3600)
    for h in range(hour - 29 * 86400, hour, 3600):
        db.add_count("bluesky", "_minutes", h, 60)
        db.add_count("bluesky", "telco_down", h, people_per_hour)
    db.kv_set("bluesky:first_seen", str(upto - 29 * 86400))
    for group in SETTINGS.groups:
        db.kv_set(f"bluesky:first_seen:{group.id}", str(upto - 29 * 86400))


def burst(adapter, now, text, people=16):
    """`people` different people post `text` over four minutes, then time moves on."""
    seq = 0
    for i in range(people):
        seq += 1
        raw = commit(seq, T0 + (i // 4) * 60 + i % 4, f"did:plc:{i}", f"r{seq}", text)
        now[0] = read_message(raw).time
        adapter.handle(raw)
    for extra in range(2):
        seq += 1
        raw = commit(seq, T0 + 400 + extra * 60, "did:plc:z", f"r{seq}", "lunch")
        now[0] = read_message(raw).time
        adapter.handle(raw)
    return adapter.reports[-1]


def test_a_group_added_later_learns_before_it_can_spike(make_adapter):
    power = KeywordGroup(id="power_out", name="Power out in Sydney", all_of=[["Sydney"], ["blackout", "power out"]])
    now = [T0]
    adapter, db = make_adapter(lambda: now[0], BlueskySettings(groups=[*SETTINGS.groups, power]))
    seed_history(db, T0, people_per_hour=3)  # the four starting groups have a month of history
    result = burst(adapter, now, "Sydney blackout?")
    assert result.level == Level.NORMAL  # 16 people would be a spike, but the group is new
    new = result.details["groups"][-1]
    assert new["id"] == "power_out" and not new["spike"] and new["learning_left"] > 6.9 * 86400
    assert "Still learning a new group (power out in sydney): 7 days to go" in result.reason
    assert db.kv_get("bluesky:first_seen:power_out") is not None
    # A week later it's judged like the others.
    assert judge_group(power, 16, 0.0, SETTINGS, learning_left=0).spike


def test_a_burst_of_posts_is_a_spike_and_goes_on_the_timeline_once(make_adapter):
    now = [T0]
    adapter, db = make_adapter(lambda: now[0])
    seed_history(db, T0, people_per_hour=3)  # 0.25 people per 5 minutes, usually
    seq = 0
    for minute in range(4):
        for person in range(4):  # 4 different people a minute say Telstra is down
            seq += 1
            raw = commit(seq, T0 + minute * 60 + person, f"did:plc:{minute}-{person}", f"r{seq}", "Telstra down?")
            now[0] = read_message(raw).time
            adapter.handle(raw)
    for extra in range(2):
        seq += 1
        raw = commit(seq, T0 + 400 + extra * 60, "did:plc:z", f"r{seq}", "lunch")
        now[0] = read_message(raw).time
        adapter.handle(raw)
    result = adapter.reports[-1]
    assert result.level == Level.ELEVATED
    assert result.reason.startswith("Spike in posts about telco down: 16 people in 5 minutes, usually about 0.5")
    group = result.details["groups"][1]
    assert group["spike"] and group["samples"][0]["text"] == "Telstra down?"
    spikes = [e for e in db.events_since(0) if e["title"] == "Bluesky spike: Telco down"]
    assert len(spikes) == 1


# --- Against a local stand-in for Jetstream ---


async def test_stream_connects_like_jetstream_expects(make_adapter):
    seen = {}

    async def handler(ws):
        seen["path"] = ws.request.path
        seen["agent"] = ws.request.headers.get("User-Agent")
        seen["subprotocol"] = ws.subprotocol
        for i in range(3):
            await ws.send(commit(i + 1, T0 + i * 100, f"did:plc:{i}", f"r{i}", "Optus is down"))

    async with serve(handler, "127.0.0.1", 0, subprotocols=["xrpc.v1.json"]) as server:
        port = server.sockets[0].getsockname()[1]
        adapter, db = make_adapter(lambda: T0 + 200, BlueskySettings(url=f"ws://127.0.0.1:{port}/subscribe"))
        await adapter.stream()  # returns when the server hangs up
    assert seen["path"] == "/subscribe?collections=app.bsky.feed.post&kinds=commit&kinds=account"
    assert seen["agent"].startswith("sitrep-bot/") and seen["subprotocol"] == "xrpc.v1.json"
    assert db.counts_since("bluesky", "telco_down", 0) == [(int(T0), 1.0)]


async def test_a_refused_cursor_starts_again_from_now(make_adapter):
    def refuse(connection, request):
        return connection.respond(HTTPStatus.BAD_REQUEST, "cursor too old\n")

    async with serve(lambda ws: asyncio.sleep(0), "127.0.0.1", 0, process_request=refuse) as server:
        port = server.sockets[0].getsockname()[1]
        adapter, db = make_adapter(lambda: T0, BlueskySettings(url=f"ws://127.0.0.1:{port}/subscribe"))
        db.kv_set("bluesky:cursor", json.dumps({"seq": 5, "time": T0 - 60}))
        adapter._cursor = {"seq": 5, "time": T0 - 60}
        with pytest.raises(SourceError, match="starting from now"):
            await adapter.stream()
    assert db.kv_get("bluesky:cursor") is None and "cursor=" not in adapter.url()


def test_the_starting_groups_learn_alongside_bluesky(make_adapter):
    now = [T0]
    adapter, db = make_adapter(lambda: now[0])
    burst(adapter, now, "Telstra down?", people=4)
    assert adapter.reports[-1].level == Level.LEARNING
    started = float(db.kv_get("bluesky:first_seen"))
    assert all(float(db.kv_get(f"bluesky:first_seen:{g.id}")) == started for g in SETTINGS.groups)
