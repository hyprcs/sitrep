"""Simulate mode: watch the dashboard and the Telegram alerts work, with nothing real happening.

Start it with:  python -m sitrep simulate

It runs a second, separate sitrep (on the next port up, 8081 by default) with its own
throwaway database, so your real history, Bluesky's learning and your real alerts are
never touched. Every source is replaced by a stand-in that follows a script: a Bluesky
spike, then IODA seeing Telstra drop, then Australia going down, then everything calming,
then your own internet dropping out for a minute. The real scoring, dashboard and alert
code all run as normal, on shorter timings so the whole story takes about 7 minutes.

Telegram alerts are real messages to your chat, each marked "SIMULATION: not a real alert".

The stand-ins can also play other scripts: `python -m sitrep simulate --replay` runs a set of
realistic scenarios on a pretend clock with the real timings, and prints which ones would
alert (see scenarios.py). Nothing is sent and no dashboard is started.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .adapters import REGISTRY
from .adapters.base import AdapterSettings, SourceAdapter
from .alerts import Alerts
from .config import Config
from .models import INTERNET_DOWN, OFFLINE_HEADLINE, Assessment, Level, Signal, national
from .runtime import Runtime

log = logging.getLogger("sitrep.simulate")

# Shorter waits, so the story fits in a few minutes. The real values are in config.yaml.
FAST_SCORING = {"evaluate_every_seconds": 5, "raise_after_checks": 2, "lower_after_minutes": 1}
FAST_ALERTS = {"all_clear_after_minutes": 1, "report_internet_outages": True, "internet_outage_min_minutes": 0.5,
               "startup_grace_minutes": 0.25}

NORMAL = {
    "probes": "Your router and all 8 outside checks answer",
    "reach": "Sydney, Singapore, Tokyo, US West, US East and Europe all answering at their usual speed",
    "ioda": "Australia, NSW and the 5 big carriers all look normal",
    "radar": "Australian traffic is normal for the time of day; no outages reported",
    "cloud": "No incidents for AWS Sydney, Azure Australia East, Google Cloud Sydney or Cloudflare SYD",
    "acsc": "No new alerts or advisories in the last 24 hours",
    "ransomware": "0 Australian victims in the last 24 hours (usually 0.5 a day)",
    "advice": "US: Level 1, exercise normal precautions; UK: doesn't advise against travel to any part of Australia",
    "rfs": "No Emergency Warning within 50 km of Macquarie Park, and no Watch and Act within 15 km",
    "livetraffic": "4 hazards on Greater Sydney's roads (0 major)",
    "transit": "No unplanned disruptions on Sydney Trains or Metro",
    "bom": "No severe weather, thunderstorm, fire weather or major flood warning for Sydney",
    "news": "14 headlines in the last hour (ABC News 6, Sydney Morning Herald 5, Guardian Australia 3)",
    "aemo": "NSW grid normal (demand 7,100 MW, price $95/MWh); no supply warnings from AEMO",
    "fuel": "Median prices (c/L): Unleaded 91 189.9 near home, 186.5 across NSW; Diesel 199.9 near home, "
            "197.9 across NSW. No sudden jump",
    "adsb": "14 aircraft in the air within 40 km of the CBD (usually 15 at this hour of the week)",
    "telcos": "No unplanned outages",
    "bluesky": "No keyword group is busier than usual for this hour",
}


@dataclass(frozen=True)
class Report:
    """What a stand-in reports at one step."""

    level: Level
    reason: str
    scope: str | None = None  # what it's about; the real source's own scope if None
    key: str | None = None  # the event's key; the source's id if None
    backlog: bool = False  # already old when sitrep saw it: shown, but can't alert
    emergency: bool = False


@dataclass(frozen=True)
class Item:
    """An item a stand-in has seen: say, a headline from while the internet was down."""

    source: str
    title: str
    minutes_old: float  # how old it already was when sitrep saw it
    level: str = "info"
    details: dict[str, Any] | None = None  # a place and so on, as the real source would store it


@dataclass(frozen=True)
class Step:
    at: float  # seconds after the simulation starts
    title: str
    explain: str
    reports: dict[str, tuple[Level, str] | Report]
    offline: bool = False  # the probes report "can't reach the internet"
    items: tuple[Item, ...] = ()


def _all_normal() -> dict[str, tuple[Level, str]]:
    return {source: (Level.NORMAL, reason) for source, reason in NORMAL.items()}


SCRIPT = (
    Step(0, "Everything is normal",
         "All sources report normal. Keep an eye on the overall status at the top.",
         _all_normal()),
    Step(30, "Bluesky spikes",
         "Lots of people post that Telstra is down. That's one category (social) on its own, which "
         "isn't enough: the overall level stays normal and nothing is sent.",
         {"bluesky": Report(Level.ELEVATED, '"Telco down" is spiking: 23 people in 5 minutes, usually about 1',
                            scope=national("internet"), key="bluesky:telco_down")}),
    Step(75, "IODA sees Telstra dropping",
         "A second category (network measurement) agrees, and both are about Australia's internet, so "
         "the overall level goes to Elevated and Telegram gets a message. Elevated messages arrive "
         "silently; only Critical makes a sound.",
         {"ioda": (Level.ELEVATED, "Telstra (AS1221) is dropping on 2 methods")}),
    Step(135, "Australia goes down on IODA",
         "One category is now critical, so the overall level goes to Critical and Telegram gets a "
         "second alert. TPG's status page reports an outage too; it's shown, but never scored.",
         {"ioda": (Level.CRITICAL, "Australia is down on 2 methods; Telstra and Optus are both dropping"),
          "telcos": (Level.ELEVATED, "1 unplanned outage: Sydney (NSW), NBN, 48,000 customers, widespread")}),
    Step(195, "Everything calms down",
         "All sources are back to normal. The overall level waits 1 calm minute before coming down "
         "(15 in real life), then the all-clear waits 1 more minute (30 in real life) and arrives "
         "silently.",
         _all_normal()),
    Step(360, "Your internet goes down",
         "Your router answers but nothing outside does. The dashboard says so straight away. sitrep "
         "can't send anything while it's offline.",
         {}, offline=True),
    Step(410, "Your internet is back",
         "sitrep waits a few seconds (2 minutes in real life) while the sources catch up, then "
         "Telegram gets one silent message saying how long the internet was down and what came in "
         "meanwhile.",
         {"probes": (Level.NORMAL, NORMAL["probes"])},
         items=(Item("acsc", "ACSC advisory: Patch your VPN appliances", 0.5),)),
    Step(440, "Simulation finished",
         "That's the whole story. Nothing on this page was real. Stop the simulation with Ctrl+C in "
         "the window it's running in.",
         {}),
)


class StandInSettings(AdapterSettings):
    interval_seconds: float = 30  # the uptime strip asks the probes for this


class StandIn(SourceAdapter):
    """Takes the place of a real source. Reports only what the script tells it to."""

    Settings = StandInSettings

    @property
    def stale_after(self) -> float:
        return 24 * 3600  # never goes stale during a simulation

    @property
    def content_budget(self) -> float | None:
        return None  # a stand-in's reports carry no data to go stale

    async def run(self) -> None:
        await asyncio.Event().wait()

    def play(self, report: Report) -> None:
        signals: tuple[Signal, ...] = ()
        if report.level in (Level.ELEVATED, Level.CRITICAL):
            scope = report.scope or self.scope
            signals = (Signal(report.key or self.id, report.level, (scope,) if scope else (), title=report.reason,
                              alertable=not report.backlog, emergency=report.emergency),)
        self.report(Assessment(report.level, f"{report.reason} (simulated)", signals=signals))


def stand_in_for(real: type[SourceAdapter]) -> type[StandIn]:
    attrs = {"id": real.id, "name": real.name, "category": real.category, "link": real.link,
             "scope": real.scope, "source_type": real.source_type}
    return type(f"StandIn{real.__name__}", (StandIn,), attrs)


def as_report(spec: tuple[Level, str] | Report) -> Report:
    return spec if isinstance(spec, Report) else Report(*spec)


def simulation_config(cfg: Config, port: int | None = None) -> Config:
    """The real settings, with the simulation's own port, database and shorter timings."""
    real_db = Path(cfg.storage.db_path)
    sim_db = real_db.with_name("simulate.db")
    if sim_db == real_db:
        sim_db = real_db.with_name("simulate-run.db")
    return cfg.model_copy(update={
        "server": cfg.server.model_copy(update={"port": port or cfg.server.port + 1}),
        "storage": cfg.storage.model_copy(update={"db_path": str(sim_db)}),
        "scoring": cfg.scoring.model_copy(update=FAST_SCORING),
        "alerts": cfg.alerts.model_copy(update=FAST_ALERTS),
    })


def fresh_database(path: str) -> None:
    """Delete the simulation's database from last time, so each run starts clean."""
    db = Path(path)
    assert db.name.startswith("simulate"), db  # never the real database
    for leftover in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        leftover.unlink(missing_ok=True)


def play_step(runtime: Runtime, step: Step, now: float) -> None:
    """Make the stand-ins report what one step of a script says."""
    for n, item in enumerate(step.items):
        adapter = runtime.adapters.get(item.source)
        if adapter is not None:
            adapter.see_item(f"sim:{int(now)}:{n}", now - item.minutes_old * 60, title=item.title,
                             level=item.level, now=now, details=item.details)
    for source_id, spec in step.reports.items():
        adapter = runtime.adapters.get(source_id)
        if isinstance(adapter, StandIn):
            adapter.play(as_report(spec))
    if step.offline and "probes" in runtime.adapters:
        reason = f"{OFFLINE_HEADLINE}: your router answers, but nothing outside has for 1 minute (simulated)"
        runtime.adapters["probes"].report(Assessment(Level.CRITICAL, reason, flags=frozenset({INTERNET_DOWN})))


class Simulation:
    """Plays the script, one step at a time, by reporting through the stand-ins."""

    def __init__(self, clock: Callable[[], float], script: tuple[Step, ...] = SCRIPT) -> None:
        self.clock = clock
        self.script = script
        self.started_at: float | None = None
        self.step = -1  # index of the step being shown

    def apply(self, runtime: Runtime, index: int) -> None:
        play_step(runtime, self.script[index], self.clock())
        self.step = index
        log.info("Simulation step %d of %d: %s", index + 1, len(self.script), self.script[index].title)

    def due(self, now: float) -> list[int]:
        """Steps whose time has come but which haven't been played yet."""
        if self.started_at is None:
            self.started_at = now
        return [i for i, s in enumerate(self.script) if i > self.step and now - self.started_at >= s.at]

    async def run(self, runtime: Runtime) -> None:
        while self.step < len(self.script) - 1:
            for index in self.due(self.clock()):
                self.apply(runtime, index)
            await asyncio.sleep(1)

    def status(self) -> dict[str, Any]:
        if self.step < 0:
            return {"step": 0, "steps": len(self.script), "title": "Starting", "explain": "", "next_at": None,
                    "finished": False}
        step = self.script[self.step]
        later = self.script[self.step + 1] if self.step + 1 < len(self.script) else None
        return {
            "step": self.step + 1,
            "steps": len(self.script),
            "title": step.title,
            "explain": step.explain,
            "next_at": self.started_at + later.at if later and self.started_at is not None else None,
            "finished": later is None,
        }


class SimulatedRuntime(Runtime):
    """A Runtime whose sources are all stand-ins, played by a Simulation."""

    def __init__(self, cfg: Config, clock: Callable[[], float] = time.time) -> None:
        super().__init__(cfg, clock)
        self.specs = [(stand_in_for(real), StandInSettings()) for real in REGISTRY.values()]
        self.simulation = Simulation(self.clock)

    def setup(self) -> None:
        fresh_database(self.cfg.storage.db_path)
        super().setup()

    def _build_alerts(self, simulation: bool = True) -> Alerts:
        return super()._build_alerts(simulation=True)

    async def start(self) -> None:
        await super().start()
        self._spawn(self.simulation.run(self), "simulation")

    def state_payload(self) -> dict[str, Any]:
        payload = super().state_payload()
        payload["simulation"] = self.simulation.status()
        return payload
