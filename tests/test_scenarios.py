"""Scenario replays: each one plays out as its description says, under the real timings."""

from __future__ import annotations

import pytest

from sitrep.alerts import AlertPolicy, Alerts, Notifier
from sitrep.config import AlertsSettings, Config
from sitrep.db import Database
from sitrep.models import Level, Overall, Signal
from sitrep.scenarios import SCENARIOS, play, play_all, report

N, E, C, OFFLINE = Overall.NORMAL, Overall.ELEVATED, Overall.CRITICAL, Overall.OFFLINE
T0 = 1_800_000_000.0
MIN = 60


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_each_scenario_alerts_as_described(scenario):
    outcome = await play(Config(), scenario)
    assert [(a.kind, a.level, a.loud) for a in outcome.alerts] == list(scenario.expect)


async def test_grid_and_cyber_alerts_only_under_the_plain_rule():
    (scenario,) = [s for s in SCENARIOS if s.name == "grid-and-cyber"]
    label, overrides = scenario.compare
    outcome = await play(Config(), scenario, label, overrides)
    assert [(a.kind, a.level) for a in outcome.alerts] == [("raised", E), ("lowered", N)]


async def test_the_report_reads_plainly():
    text = report(await play_all(Config(), ["back-after-outage"]))
    assert "Back online after a 3-hour outage" in text
    assert "while you were offline (silent)" in text
    assert "It lists 5 items that came in meanwhile" in text
    assert "Verdict: would send a silent message only." in text


# --- The alert rules the scenarios lean on, one at a time ---


def sig(key, level=Level.CRITICAL):
    return Signal(key, level, ("national:internet",), title=key)


def test_no_alerts_during_the_start_up_grace():
    p = AlertPolicy(AlertsSettings(), started_at=T0)
    assert p.update(E, "trouble", T0 + MIN) is None
    assert p.update(E, "trouble", T0 + 2 * MIN).kind == "raised"


def test_no_alerts_just_after_the_internet_comes_back():
    p = AlertPolicy(AlertsSettings(internet_outage_min_minutes=60))
    p.update(N, "calm", T0)
    p.update(OFFLINE, "down", T0 + MIN)
    assert p.update(C, "backlog", T0 + 2 * MIN) is None  # a short outage, so no summary, but still the grace
    assert p.update(C, "still", T0 + 4 * MIN).kind == "raised"


def test_the_same_event_doesnt_alert_twice_but_can_escalate():
    p = AlertPolicy(AlertsSettings(all_clear_after_minutes=0))
    p.update(N, "calm", T0)
    assert p.update(E, "ioda elevated", T0 + MIN, drivers=(sig("ioda", Level.ELEVATED),)).kind == "raised"
    assert p.update(C, "ioda critical", T0 + 2 * MIN, drivers=(sig("ioda"),)).kind == "raised"
    assert p.update(N, "calm", T0 + 3 * MIN).kind == "lowered"
    assert p.update(C, "ioda critical again", T0 + 4 * MIN, drivers=(sig("ioda"),)) is None  # within 30 minutes
    assert p.told == C  # quietly up to date
    assert p.update(N, "calm", T0 + 5 * MIN).kind == "lowered"
    assert p.update(C, "ioda critical later", T0 + 40 * MIN, drivers=(sig("ioda"),)).kind == "raised"


def test_a_new_critical_event_while_critical_gets_its_own_message():
    p = AlertPolicy(AlertsSettings())
    p.update(N, "calm", T0)
    assert p.update(C, "ioda", T0 + MIN, drivers=(sig("ioda"),)).kind == "raised"
    both = (sig("ioda"), sig("aemo:load_shed"))
    assert p.update(C, "both", T0 + 2 * MIN, drivers=both) is None  # seen once: wait for a second look
    alert = p.update(C, "both", T0 + 2.5 * MIN, drivers=both)
    assert (alert.kind, alert.reason, alert.loud) == ("another", "aemo:load_shed", True)
    assert p.update(C, "both", T0 + 40 * MIN, drivers=both) is None  # never again while it lasts


class Rec(Notifier):
    name = "Rec"

    def queue(self, alert):
        pass

    async def check(self):
        pass

    async def send(self, alert):
        pass


def test_alerts_remember_keys_across_a_restart(tmp_path):
    db = Database(tmp_path / "a.db")
    cfg = AlertsSettings(startup_grace_minutes=0)
    alerts = Alerts(cfg, db, [Rec(db, lambda: T0)], started_at=T0)
    alerts.update(N, "calm", T0)
    alerts.update(C, "ioda", T0 + MIN, drivers=(sig("ioda"),))
    again = Alerts(cfg, db, [Rec(db, lambda: T0)], started_at=T0 + 5 * MIN)
    assert again.policy.alerted == {"critical:ioda": T0 + MIN}
    db.close()


def test_a_restart_gap_is_summarised(tmp_path):
    db = Database(tmp_path / "a.db")
    p = AlertPolicy(AlertsSettings(), N, T0 - 3600, started_at=T0, restart_gap=(T0 - 20 * MIN, T0))
    assert p.update(N, "calm", T0 + MIN) is None
    alert = p.update(N, "calm", T0 + 2 * MIN)
    assert (alert.kind, alert.restarted, alert.offline) == ("summary", True, (T0 - 20 * MIN, T0))
    db.close()
