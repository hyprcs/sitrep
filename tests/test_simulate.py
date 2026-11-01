"""Simulate mode: the script tells the intended story, and never touches the real database."""

from __future__ import annotations

from pathlib import Path

import pytest

from sitrep.alerts import Alert, Alerts, Notifier
from sitrep.config import AlertsSettings, Config, ServerSettings, StorageSettings
from sitrep.db import Database
from sitrep.models import Overall
from sitrep.simulate import CYBER_SCRIPT, SCRIPT, SimulatedRuntime, fresh_database, simulation_config
from sitrep.telegram import SIMULATION_LINE, format_alert

T0 = 1_800_000_000.0


class Recorder(Notifier):
    name = "Recorder"

    def __init__(self, db: Database) -> None:
        super().__init__(db, lambda: T0)
        self.alerts: list[tuple[float, Alert]] = []

    def queue(self, alert: Alert) -> None:
        self.alerts.append((alert.at - T0, alert))

    async def check(self) -> None:
        pass

    async def send(self, alert: Alert) -> None:
        pass


def real_config(tmp_path: Path) -> Config:
    return Config(server=ServerSettings(port=8080), storage=StorageSettings(db_path=str(tmp_path / "sitrep.db")),
                  alerts=AlertsSettings(dashboard_url="http://ds920:8080"))


def test_simulation_settings(tmp_path):
    real = real_config(tmp_path)
    sim = simulation_config(real)
    assert sim.server.port == 8081
    assert Path(sim.storage.db_path) == tmp_path / "simulate.db"
    assert (sim.scoring.lower_after_minutes, sim.alerts.all_clear_after_minutes) == (1, 1)
    assert simulation_config(real, port=9000).server.port == 9000
    # The real settings are left as they were.
    assert (real.server.port, real.scoring.lower_after_minutes, real.alerts.all_clear_after_minutes) == (8080, 15, 30)


def test_only_a_simulation_database_is_ever_deleted(tmp_path):
    with pytest.raises(AssertionError):
        fresh_database(str(tmp_path / "sitrep.db"))


async def test_the_whole_story(tmp_path):
    now = [T0]
    runtime = SimulatedRuntime(simulation_config(real_config(tmp_path)), clock=lambda: now[0])
    runtime.setup()
    recorder = Recorder(runtime.db)
    runtime.alerts = Alerts(runtime.cfg.alerts, runtime.db, [recorder], started_at=T0)
    sim = runtime.simulation

    levels = {}
    for second in range(0, int(SCRIPT[-1].at) + 10, 5):
        now[0] = T0 + second
        for index in sim.due(now[0]):
            sim.apply(runtime, index)
        runtime.evaluate()
        levels[second] = runtime.tracker.current.level
    await runtime.stop()

    assert levels[25] == Overall.NORMAL
    assert levels[70] == Overall.NORMAL  # Bluesky alone isn't enough
    assert levels[100] == Overall.ELEVATED
    assert levels[160] == Overall.CRITICAL
    assert levels[230] == Overall.CRITICAL  # calming down takes a minute
    assert levels[300] == Overall.NORMAL
    assert levels[380] == Overall.OFFLINE
    assert levels[430] == Overall.NORMAL

    story = [(round(at), a.kind, a.level, a.loud) for at, a in recorder.alerts]
    assert [(kind, level, loud) for _, kind, level, loud in story] == [
        ("raised", Overall.ELEVATED, False),
        ("raised", Overall.CRITICAL, True),
        ("lowered", Overall.NORMAL, False),
        ("summary", Overall.NORMAL, False),
    ]
    times = [at for at, *_ in story]
    assert 75 < times[0] <= 90 and 135 < times[1] <= 150 and 300 < times[2] < 360 and 420 <= times[3] <= 430
    assert recorder.alerts[3][1].items["total"] == 1  # the ACSC item that came in while it was offline
    assert "Telstra (AS1221) is dropping on 2 methods (simulated)" in recorder.alerts[0][1].reason
    assert not (tmp_path / "sitrep.db").exists()  # the real database was never opened


async def test_the_cyber_attack_story(tmp_path):
    now = [T0]
    runtime = SimulatedRuntime(simulation_config(real_config(tmp_path)), clock=lambda: now[0], script=CYBER_SCRIPT)
    runtime.setup()
    recorder = Recorder(runtime.db)
    runtime.alerts = Alerts(runtime.cfg.alerts, runtime.db, [recorder], started_at=T0)
    sim = runtime.simulation

    levels = {}
    for second in range(0, int(CYBER_SCRIPT[-1].at) + 10, 5):
        now[0] = T0 + second
        for index in sim.due(now[0]):
            sim.apply(runtime, index)
        runtime.evaluate()
        levels[second] = runtime.tracker.current.level
    await runtime.stop()

    assert levels[70] == Overall.NORMAL  # a ransomware listing alone isn't enough
    assert levels[100] == Overall.ELEVATED  # ACSC backs it up
    assert levels[205] == Overall.ELEVATED  # posts, news and one telco dropping: still the same event
    assert levels[230] == Overall.CRITICAL  # Australia drops: something measured
    assert levels[400] == Overall.ELEVATED  # the news story outlasts the sources

    story = [(round(at), a.kind, a.level, a.loud) for at, a in recorder.alerts]
    assert [(kind, level, loud) for _, kind, level, loud in story] == [
        ("raised", Overall.ELEVATED, False),
        ("raised", Overall.CRITICAL, True),
        ("lowered", Overall.ELEVATED, False),
    ]
    times = [at for at, *_ in story]
    assert 75 < times[0] <= 90 and 210 < times[1] <= 225 and 330 < times[2] < 420
    assert "Example Telecom" in recorder.alerts[0][1].reason and "ACSC" in recorder.alerts[0][1].reason
    assert "Story" in recorder.alerts[2][1].reason
    assert not (tmp_path / "sitrep.db").exists()


async def test_status_for_the_dashboard(tmp_path):
    now = [T0]
    runtime = SimulatedRuntime(simulation_config(real_config(tmp_path)), clock=lambda: now[0])
    runtime.setup()
    assert runtime.state_payload()["simulation"]["title"] == "Starting"
    runtime.simulation.apply(runtime, *runtime.simulation.due(now[0]))
    status = runtime.state_payload()["simulation"]
    assert (status["step"], status["steps"], status["title"]) == (1, len(SCRIPT), "Everything is normal")
    assert status["next_at"] == T0 + SCRIPT[1].at and status["finished"] is False
    assert {s["id"] for s in runtime.state_payload()["sources"]} >= {"probes", "ioda", "bluesky", "telcos"}
    await runtime.stop()


def test_simulated_messages_say_so_and_have_no_link():
    alert = Alert("raised", Overall.CRITICAL, "Something (simulated)", T0)
    lines = format_alert(alert, AlertsSettings(dashboard_url="http://ds920:8080"), simulation=True).splitlines()
    assert lines[0] == SIMULATION_LINE
    assert not any("href" in line for line in lines)
