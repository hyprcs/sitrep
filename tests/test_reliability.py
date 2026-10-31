"""Reliability: source states, content age, backlog that can't alert, and the gaps panel."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from sitrep.adapters.base import AdapterContext, PollingAdapter, PollingSettings
from sitrep.config import Config, StorageSettings
from sitrep.db import Database
from sitrep.models import Assessment, Category, Level, Signal, national
from sitrep.runtime import HEARTBEAT_KEY, Runtime
from sitrep.state import StatusBoard, describe_gap

MIN = 60


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def board_with(db, clock, budget=None, stale_after=300):
    board = StatusBoard(db, clock)
    board.register("aemo", "AEMO electricity grid", Category.STRESS, None, stale_after,
                   content_budget=budget, default_scope="national:power")
    return board


# --- The four states ---


def test_fresh_data_is_ok_and_old_data_is_frozen(db):
    clock = Clock()
    board = board_with(db, clock, budget=20 * MIN)
    board.report("aemo", Assessment(Level.ELEVATED, "LOR2", content_at=clock.now - 5 * MIN))
    assert board.views()[0].level == Level.ELEVATED and board.views()[0].health == "ok"

    board.report("aemo", Assessment(Level.ELEVATED, "LOR2", content_at=clock.now - 25 * MIN))
    view = board.views()[0]
    assert view.level == Level.FROZEN and view.health == "frozen"
    assert view.since == clock.now - 25 * MIN
    assert view.last_known_level == Level.ELEVATED
    assert view.signals == ()  # frozen data can't count
    assert "newest data is 25 minutes old" in view.reason


def test_data_without_dates_is_unknown_age_never_the_fetch_time(db):
    clock = Clock()
    board = board_with(db, clock, budget=20 * MIN)
    board.report("aemo", Assessment(Level.NORMAL, "ok", content_at=None))
    view = board.views()[0]
    assert view.level == Level.FROZEN and view.content_at is None
    assert "no dates" in view.reason


def test_quiet_sources_have_no_budget(db):
    clock = Clock()
    board = board_with(db, clock, budget=None)
    board.report("aemo", Assessment(Level.NORMAL, "nothing new for weeks", content_at=clock.now - 30 * 86400,
                                    feed_at=clock.now - 3600))
    view = board.views()[0]
    assert view.level == Level.NORMAL and view.feed_at == clock.now - 3600


def test_two_failures_in_a_row_mean_fetch_failing(db):
    clock = Clock()
    board = board_with(db, clock)
    board.report("aemo", Assessment(Level.NORMAL, "ok", content_at=clock.now))
    board.report_failure("aemo", "HTTP 503")
    assert board.views()[0].level == Level.NORMAL  # one blip isn't a failure yet
    board.report_failure("aemo", "HTTP 503")
    view = board.views()[0]
    assert view.level == Level.FAILING and view.health == "failing"
    assert view.reason == "No update for 0 seconds (last error: HTTP 503)"


def test_a_raised_report_without_signals_gets_one_for_the_whole_source(db):
    clock = Clock()
    board = board_with(db, clock)
    board.report("aemo", Assessment(Level.ELEVATED, "LOR2 in NSW"))
    (signal,) = board.views()[0].signals
    assert (signal.key, signal.level, signal.scopes, signal.alertable) == (
        "aemo", Level.ELEVATED, ("national:power",), True)


def test_signals_survive_a_restart(db):
    clock = Clock()
    sig = Signal("aemo:lor2:NSW1", Level.ELEVATED, ("national:power",), title="LOR2", alertable=False)
    board_with(db, clock).report("aemo", Assessment(Level.ELEVATED, "LOR2", signals=(sig,)))
    assert board_with(db, clock).views()[0].signals == (sig,)


def test_gap_lines(db):
    clock = Clock()
    board = board_with(db, clock, budget=5 * MIN)
    assert describe_gap(board.views()[0], clock.now) == "AEMO electricity grid: waiting for the first update"
    board.report("aemo", Assessment(Level.NORMAL, "ok", content_at=clock.now - 22 * MIN))
    assert describe_gap(board.views()[0], clock.now).startswith("AEMO electricity grid frozen since ")
    clock.now += 40 * MIN
    board.report_failure("aemo", "timed out")
    board.report_failure("aemo", "timed out")
    assert describe_gap(board.views()[0], clock.now) == "No AEMO electricity grid data for 40 minutes (last error: timed out)"
    board.report("aemo", Assessment(Level.NORMAL, "ok", content_at=clock.now))
    assert describe_gap(board.views()[0], clock.now) is None


def test_an_older_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE source_status (source_id TEXT PRIMARY KEY, level TEXT NOT NULL, reason TEXT NOT NULL, "
                 "details TEXT NOT NULL DEFAULT '{}', flags TEXT NOT NULL DEFAULT '[]', observed_at REAL, "
                 "last_attempt_at REAL, last_success_at REAL, last_error TEXT)")
    conn.execute("INSERT INTO source_status (source_id, level, reason) VALUES ('acsc', 'normal', 'quiet')")
    conn.commit()
    conn.close()
    database = Database(path)
    saved = database.load_source_status("acsc")
    assert saved["reason"] == "quiet" and saved["signals"] == [] and saved["content_at"] is None
    database.close()


# --- Items: backlog can't alert ---


class Poller(PollingAdapter):
    id = "acsc"
    name = "ACSC"
    category = Category.OFFICIAL
    Settings = PollingSettings
    content_budget_minutes = 30
    publish_delay_minutes = 15

    async def poll(self) -> Assessment:
        return Assessment(Level.NORMAL, "ok")


def poller(db, clock, **settings) -> Poller:
    ctx = AdapterContext(Config(), db, StatusBoard(db, clock), None, clock)
    adapter = Poller(ctx, PollingSettings(interval_seconds=600, **settings))
    adapter.register()
    return adapter


def test_recency_limit_covers_slow_sources(db):
    clock = Clock()
    assert poller(db, clock).recency_limit == 25 * MIN  # 10-minute polls plus a 15-minute publishing delay
    assert poller(db, clock, alert_recency_minutes=5).recency_limit == 5 * MIN


def test_content_budget_can_be_changed_or_switched_off(db):
    clock = Clock()
    assert poller(db, clock).content_budget == 30 * MIN
    assert poller(db, clock, content_age_budget_minutes=90).content_budget == 90 * MIN
    assert poller(db, clock, content_age_budget_minutes="none").content_budget is None
    assert poller(db, clock, content_age_budget_minutes=None).content_budget is None


def test_new_items_alert_and_backlog_does_not(db):
    clock = Clock()
    p = poller(db, clock)
    assert p.see_item("new", clock.now - 5 * MIN, title="fresh").alertable
    assert not p.see_item("old", clock.now - 3 * 3600, title="from before the outage").alertable


def test_an_update_after_an_outage_counts_but_a_late_copy_never_stops_one_counting(db):
    clock = Clock()
    p = poller(db, clock)
    assert not p.see_item("rfs:1", clock.now - 3 * 3600, title="EW").alertable  # backlog
    clock.now += 60
    seen = p.see_item("rfs:1", clock.now - 30, title="EW, updated")  # a new version, just issued
    assert seen.alertable and seen.updated
    clock.now += 3 * 3600
    assert p.see_item("rfs:1", clock.now - 2 * 3600, title="EW, late copy").alertable


def test_items_without_their_own_time_need_a_recent_good_look(db):
    clock = Clock()
    p = poller(db, clock)
    assert not p.see_item("a", None, title="first look since start").alertable
    p.report(Assessment(Level.NORMAL, "ok"))
    clock.now += 600
    assert p.see_item("b", None, title="appeared since the last poll").alertable
    clock.now += 3 * 3600  # sitrep couldn't look at the source for hours
    assert not p.see_item("c", None, title="could be from any time").alertable


def test_items_seen_while_offline_are_listed(db):
    clock = Clock()
    p = poller(db, clock)
    p.see_item("before", clock.now - 3600, title="before", now=clock.now - 3500)
    p.see_item("during", clock.now - 600, title="during", level="elevated")
    found = db.items_seen_between(clock.now - 1800, clock.now)
    assert [i["item_id"] for i in found] == ["during"]


def test_items_and_alert_keys_are_cleaned_up(db):
    now = 100 * 86400.0
    db.see_item("acsc", "old", ts=now - 9 * 86400, now=now - 9 * 86400, fresh=True, source_type="official", title="x")
    db.mark_alerted(["critical:ioda"], now - 3 * 86400)
    deleted = db.cleanup(now, StorageSettings())
    assert deleted["items"] == 1 and deleted["alert_keys"] == 1


# --- Waking up when the internet is back ---


async def test_a_sleeping_source_can_be_woken(db):
    p = poller(db, Clock())
    task = asyncio.create_task(p._sleep(3600))
    await asyncio.sleep(0)
    p.wake()
    assert await asyncio.wait_for(task, 1) is True


def test_restart_gap_comes_from_the_heartbeat(tmp_path):
    clock = Clock()
    cfg = Config(storage=StorageSettings(db_path=str(tmp_path / "s.db")))
    first = Runtime(cfg, clock)
    first.setup()
    first.evaluate()
    assert first.db.kv_get(HEARTBEAT_KEY) == str(clock.now)
    stopped_at = clock.now
    first.db.close()

    clock.now += 20 * MIN
    second = Runtime(cfg, clock)
    second.setup()
    assert second._restart_gap() == (stopped_at, clock.now)
    second.db.close()


def test_signal_round_trip():
    sig = Signal("bluesky:telco_down", Level.ELEVATED, (national("internet"),), title="spike", alertable=False,
                 at=1.0, keywords=("telco_down",))
    assert Signal.from_dict(sig.to_dict()) == sig
