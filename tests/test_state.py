"""Storage, the status board (stale handling, restarts) and config loading."""

from __future__ import annotations

import pytest

from sitrep.config import ConfigError, StorageSettings, expand_env, load_config
from sitrep.db import Database
from sitrep.models import INTERNET_DOWN, Assessment, Category, Level
from sitrep.state import StatusBoard


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def make_board(db, clock):
    board = StatusBoard(db, clock)
    board.register("probes", "Connectivity", Category.CONNECTIVITY, None, stale_after=120)
    return board


# --- Status board ---


def test_new_source_is_learning_then_failing(db):
    clock = Clock()
    board = make_board(db, clock)
    assert board.views()[0].level == Level.LEARNING
    assert board.views()[0].health == "learning"
    clock.now += 121
    assert board.views()[0].level == Level.FAILING
    assert board.views()[0].reason == "No successful update yet"


def test_report_then_go_stale_keeps_last_known(db):
    clock = Clock()
    board = make_board(db, clock)
    board.report("probes", Assessment(Level.ELEVATED, "Google failing", flags=frozenset({INTERNET_DOWN})))
    assert board.views()[0].level == Level.ELEVATED

    clock.now += 200
    board.report_failure("probes", "timed out")
    stale = board.views()[0]
    assert stale.level == Level.FAILING
    assert stale.health == "failing"
    assert stale.reason == "No update for 3 minutes (last error: timed out)"
    assert stale.last_known_level == Level.ELEVATED
    assert stale.last_known_reason == "Google failing"
    assert stale.flags == frozenset()  # stale data can't raise alarms
    assert stale.signals == ()


def test_last_known_state_survives_a_restart(db):
    clock = Clock()
    make_board(db, clock).report("probes", Assessment(Level.NORMAL, "All good"))
    restarted = make_board(db, clock)
    view = restarted.views()[0]
    assert view.level == Level.NORMAL
    assert view.reason == "All good"
    clock.now += 500
    assert restarted.views()[0].level == Level.FAILING


# --- Database ---


def test_counts_add_up_in_minute_and_hour_buckets(db):
    for ts in (3600 * 10 + 5, 3600 * 10 + 20, 3600 * 10 + 70):
        db.add_count("probes", "rounds", ts)
    assert db.counts_since("probes", "rounds", 0) == [(36000, 2.0), (36060, 1.0)]
    assert db.counts_since("probes", "rounds", 0, hourly=True) == [(36000, 3.0)]


def test_bucketed_counts(db):
    for ts in (0, 60, 1800, 1860):
        db.add_count("probes", "rounds", ts)
    db.add_count("probes", "dark", 1860)
    assert db.bucketed_counts("probes", 0, 1800) == {
        (0, "rounds"): 2.0, (1800, "rounds"): 2.0, (1800, "dark"): 1.0,
    }


def test_events_dedupe(db):
    assert db.add_event(1, "acsc", "official", "elevated", "New alert", dedupe_key="a1")
    assert not db.add_event(2, "acsc", "official", "elevated", "New alert", dedupe_key="a1")
    assert len(db.events_since(0)) == 1


def test_cleanup_respects_retention(db):
    day = 86400
    now = 100 * day
    db.add_count("probes", "rounds", now - 8 * day)  # minute row too old, hour row kept
    db.add_count("probes", "rounds", now - 1 * day)
    db.add_event(now - 91 * day, "probes", "connectivity", "elevated", "old")
    db.add_event(now - 1 * day, "probes", "connectivity", "elevated", "new")
    deleted = db.cleanup(now, StorageSettings())
    assert deleted["counts_minute"] == 1
    assert deleted["counts_hour"] == 0
    assert deleted["events"] == 1
    assert [e["title"] for e in db.events_since(0)] == ["new"]


def test_kv_store(db):
    assert db.kv_get("cursor") is None
    db.kv_set("cursor", "1")
    db.kv_set("cursor", "2")
    assert db.kv_get("cursor") == "2"


# --- Config ---


def test_expand_env_uses_values_and_defaults(monkeypatch):
    monkeypatch.setenv("ROUTER_ADDRESS", "10.0.0.1")
    monkeypatch.setenv("EMPTY_ONE", "")
    data = {"a": "${ROUTER_ADDRESS:-192.168.1.1}", "b": ["${MISSING:-x}", "${EMPTY_ONE:-fallback}"], "c": 5}
    assert expand_env(data) == {"a": "10.0.0.1", "b": ["x", "fallback"], "c": 5}


def test_load_config_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("HOME_ISP", raising=False)
    (tmp_path / "config.yaml").write_text('home:\n  isp_name: "${HOME_ISP:-your ISP}"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("HOME_ISP=Belong\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml", env_file=tmp_path / ".env")
    assert cfg.home.isp_name == "Belong"


def test_misspelt_setting_is_a_clear_error(tmp_path):
    (tmp_path / "config.yaml").write_text("scoring:\n  raise_after_check: 3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="scoring.raise_after_check"):
        load_config(tmp_path / "config.yaml", env_file=None)


def test_missing_config_file_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="Can't find"):
        load_config(tmp_path / "nope.yaml", env_file=None)
