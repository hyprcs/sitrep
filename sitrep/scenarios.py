"""Scenario replays: would this have alerted?

    python -m sitrep simulate --replay              every scenario
    python -m sitrep simulate --replay optus-outage  just one

Each scenario is a short script of what the sources report, played on a pretend clock
through the real scoring and alert rules, with the real timings from config.yaml (so a
3-hour outage takes a moment, not 3 hours). Nothing is sent, no dashboard is started,
and the real database is never touched: each replay gets a throwaway one.

The output says what the dashboard would show and which messages would be sent, with
or without a sound. Some scenarios also show what a different setting would do.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters import REGISTRY
from .alerts import Alert, Alerts, Notifier
from .config import Config, TelegramSettings
from .db import Database
from .geo import cell_of
from .models import Level, Overall, area
from .runtime import Runtime
from .simulate import Item, Report, StandInSettings, Step, _all_normal, play_step, stand_in_for
from .util import format_duration

MIN = 60
E, C, N = Level.ELEVATED, Level.CRITICAL, Level.NORMAL


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    story: str  # what happens, in a sentence or two
    steps: tuple[Step, ...]
    end_minutes: float
    # Optional: the same story under different settings, as (label, {section: {setting: value}}).
    compare: tuple[str, dict[str, dict[str, Any]]] | None = None
    # What should happen, for the tests: (kind, level, loud) for each message, in order.
    expect: tuple[tuple[str, Overall, bool], ...] = ()


def at(minutes: float, title: str, reports: dict[str, Any], **extra: Any) -> Step:
    return Step(minutes * MIN, title, "", reports, **extra)


def placed(lat: float, lon: float, **more: Any) -> dict[str, Any]:
    """Item details for something at a point, as the real sources store them."""
    return {"lat": lat, "lon": lon, **more}


def headline(outlet: str, title: str, lat: float | None = None, lon: float | None = None, place: str = "") -> Item:
    details = {"outlet": outlet, "places": [place] if place else [], "lat": lat, "lon": lon,
               "sydney_wide": lat is None}
    return Item("news", title, 1, details=details)


def post(text: str, lat: float | None = None, lon: float | None = None) -> Item:
    return Item("bluesky", text, 1, details={"lat": lat, "lon": lon, "sydney_wide": lat is None})


M2 = (-33.7785, 151.1235)  # the M2 at Macquarie Park
ARCADIA = (-33.6203, 151.0436)  # about 20 km north-west of Macquarie Park
CBD = (-33.8688, 151.2093)
KELLYVILLE = (-33.7080, 150.9510)
PARRAMATTA = (-33.8150, 151.0011)

CALM = at(0, "Everything is normal", _all_normal())

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "optus-outage", "Optus-style national outage",
        "IODA sees Optus drop, Bluesky fills with \"Optus down\" posts, then IODA sees Australia-wide trouble.",
        (
            CALM,
            at(5, "IODA: Optus dropping", {"ioda": Report(E, "Optus (AS4804) is dropping on 2 methods")}),
            at(6, "Bluesky: telco-down spike", {"bluesky": Report(
                E, 'Spike in posts about telco down: 41 people in 5 minutes, usually about 1',
                scope="national:internet", key="bluesky:telco_down")}),
            at(20, "IODA: Australia down", {"ioda": Report(
                C, "Australia is down on 2 methods; Optus (AS4804) is dropping on 2 methods")}),
            at(70, "Everything recovers", _all_normal()),
        ),
        end_minutes=130,
        expect=(("raised", Overall.ELEVATED, False), ("raised", Overall.CRITICAL, True),
                ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "bushfire-near-home", "Bushfire Emergency Warning near home",
        "A fire in Lane Cove National Park is upgraded to an Emergency Warning, 3 km from home. An "
        "Emergency Warning within 15 km is critical on its own: it doesn't need anything to back it up.",
        (
            CALM,
            at(5, "RFS: Watch and Act 3 km away", {"rfs": Report(
                E, "Watch and Act: Lane Cove National Park fire (3 km away, Ryde)", key="rfs:900001",
                scope=area(*cell_of(-33.785, 151.155)))}),
            at(20, "RFS: upgraded to Emergency Warning", {"rfs": Report(
                C, "Emergency Warning: Lane Cove National Park fire (3 km away, Ryde)", key="rfs:900001",
                scope=area(*cell_of(-33.785, 151.155)), emergency=True)}),
            at(180, "Downgraded to Advice", _all_normal()),
        ),
        end_minutes=240,
        expect=(("raised", Overall.CRITICAL, True), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "storm-sydney", "Very dangerous thunderstorms over Sydney",
        "BoM issues a Severe Thunderstorm Warning for Sydney Metropolitan, then upgrades it: very dangerous "
        "thunderstorms with giant hail. The first is elevated (one category, so nothing is sent on its own); "
        "the second is critical by itself.",
        (
            CALM,
            at(5, "BoM: Severe Thunderstorm Warning for Sydney", {"bom": Report(
                E, "Severe Thunderstorm Warning for damaging winds and large hail for people in Sydney Metropolitan",
                key="bom:IDN21033", emergency=True)}),
            at(25, "BoM: upgraded to very dangerous thunderstorms", {"bom": Report(
                C, "Severe Thunderstorm Warning for very dangerous thunderstorms with giant hail for people in "
                   "Sydney Metropolitan", key="bom:IDN21033", emergency=True)}),
            at(90, "Storms pass", _all_normal()),
        ),
        end_minutes=150,
        expect=(("raised", Overall.CRITICAL, True), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "trains-down", "Sydney Trains suspended across the network",
        "A power failure stops trains on every line. A network-wide suspension makes the official category "
        "elevated, and a spike in Bluesky posts about an incident in Sydney backs it up: both are about "
        "Sydney as a whole.",
        (
            CALM,
            at(5, "Sydney Trains: suspended across the network", {"transit": Report(
                E, "Unplanned suspension across the network: Trains are not running due to a power supply issue",
                key="transit:900001")}),
            at(9, "Bluesky: Sydney incident spike", {"bluesky": Report(
                E, "Spike in posts about Sydney or NSW incident: 14 people in 5 minutes, usually about 1",
                scope="sydney", key="bluesky:sydney_incident")}),
            at(60, "Trains running again", _all_normal()),
        ),
        end_minutes=120,
        expect=(("raised", Overall.ELEVATED, False), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "m2-crash", "Big crash on the M2 at Macquarie Park",
        "A truck rolls over on the M2 at Macquarie Park. Live Traffic marks it major, two outlets report it, "
        "and people post about the traffic. That's a story from official, news and social sources "
        "(triangulated), and a convergence of road hazard, news and posts in one area, so it's Elevated. It "
        "never becomes critical: no emergency warning, and no keyword group matches a crash.",
        (
            CALM,
            at(5, "Live Traffic: major crash on the M2", {}, items=(Item(
                "livetraffic", "Live Traffic: Crash: M2 Motorway, Macquarie Park", 2, "watch",
                placed(*M2, suburb="Macquarie Park", major=True)),)),
            at(12, "ABC: crash closes the M2", {}, items=(headline(
                "ABC News", "Truck rollover crash closes M2 at Macquarie Park", *M2, place="Macquarie Park"),)),
            at(15, "SMH: same crash", {}, items=(headline(
                "Sydney Morning Herald", "M2 crash: long delays after truck rolls at Macquarie Park", *M2,
                place="Macquarie Park"),)),
            at(20, "Bluesky: posts about the crash", {}, items=(post(
                "massive crash on the M2 at Macquarie Park, traffic backed up to Epping", *M2),)),
            at(22, "Bluesky: another person posts", {}, items=(post(
                "M2 crash at Macquarie Park has everything gridlocked", *M2),)),
        ),
        end_minutes=240,
        expect=(("raised", Overall.ELEVATED, False), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "unexplained-activity", "Three small things in one place",
        "Near Kellyville: an RFS grass fire at Advice level, an ordinary Live Traffic hazard, and two people "
        "posting about smoke. None of them counts on its own, and they don't share words, so there's no story. "
        "But three kinds of event in one area is a convergence: Elevated.",
        (
            CALM,
            at(5, "RFS: grass fire (Advice)", {}, items=(Item(
                "rfs", "RFS Advice: GRASS FIRE, OLD WINDSOR RD, KELLYVILLE", 3, "info",
                placed(*KELLYVILLE, level="Advice")),)),
            at(8, "Live Traffic: hazard on Old Windsor Road", {}, items=(Item(
                "livetraffic", "Live Traffic: Hazard: Old Windsor Road, Kellyville", 2, "info",
                placed(*KELLYVILLE, suburb="Kellyville", kind="incident", planned=False, major=False)),)),
            at(12, "Bluesky: someone posts about smoke", {}, items=(post(
                "huge smoke cloud over Kellyville, anyone know what's going on?", *KELLYVILLE),)),
            at(14, "Bluesky: someone else does too", {}, items=(post(
                "smell of smoke right through Kellyville this afternoon", *KELLYVILLE),)),
        ),
        end_minutes=240,
        expect=(("raised", Overall.ELEVATED, False), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "bushfire-in-the-news", "Emergency Warning 20 km away, then posts from people nearby",
        "An RFS Emergency Warning at Arcadia, about 20 km away: elevated, not critical (that needs 15 km). The "
        "news reports it, making a story from official and news sources: Elevated. Then people nearby post "
        "about it, so the story is triangulated and includes an emergency-level item: Critical.",
        (
            CALM,
            at(5, "RFS: Emergency Warning at Arcadia", {"rfs": Report(
                E, "Emergency Warning: ARCADIA FIRE (20 km away, Hornsby)", key="rfs:900002",
                scope=area(*cell_of(*ARCADIA)), emergency=True)}, items=(Item(
                    "rfs", "RFS Emergency Warning: ARCADIA FIRE", 3, "elevated",
                    placed(*ARCADIA, level="Emergency Warning")),)),
            at(15, "News: bushfire threatens homes at Arcadia", {}, items=(headline(
                "ABC News NSW", "Emergency warning as bushfire threatens homes at Arcadia", *ARCADIA,
                place="Arcadia"),)),
            at(30, "Bluesky: smoke and flames at Arcadia", {}, items=(post(
                "fire is right behind the houses in Arcadia, smoke everywhere", *ARCADIA),)),
            at(200, "Downgraded to Advice", _all_normal()),
        ),
        end_minutes=480,
        expect=(("raised", Overall.ELEVATED, False), ("raised", Overall.CRITICAL, True),
                ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "cbd-lockdown", "Shooting and lockdown in the CBD",
        "Live Traffic shows a major police operation in the CBD, outlets report shots fired and a lockdown, "
        "and people post from inside. The story is triangulated and matches the \"Sydney or NSW incident\" "
        "keyword group: Critical.",
        (
            CALM,
            at(5, "Live Traffic: police operation in the CBD", {}, items=(Item(
                "livetraffic", "Live Traffic: Police operation: George Street, Sydney", 2, "watch",
                placed(*CBD, suburb="Sydney", major=True)),)),
            at(9, "News: shots fired, CBD in lockdown", {}, items=(
                headline("ABC News", "Shots fired as Sydney CBD goes into lockdown, police say", *CBD, place="Sydney"),
                headline("Guardian Australia", "Police lock down Sydney CBD after reports of shooting", *CBD,
                         place="Sydney"))),
            at(12, "Bluesky: posts from inside the lockdown", {}, items=(post(
                "we're in lockdown at Martin Place, police everywhere, heard shots", *CBD),)),
        ),
        end_minutes=300,
        expect=(("raised", Overall.ELEVATED, False), ("raised", Overall.CRITICAL, True),
                ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "pacific-cable-fault", "Pacific cable fault",
        "A cable between Australia and the US is cut and US traffic reroutes the long way round: US West "
        "and US East are suddenly 100 ms slower while Sydney is fine. That's one category (connectivity) "
        "on its own, so nothing is sent until Cloudflare Radar sees Australian traffic dip as well.",
        (
            CALM,
            at(5, "Regional reachability: US routes slow, Sydney fine", {"reach": Report(
                E, "Overseas routes degraded, Sydney fine: US West 118 ms slower than usual; "
                   "US East 104 ms slower than usual", key="reach:overseas")}),
            at(30, "Cloudflare Radar: Australian traffic dips", {"radar": Report(
                E, "Traffic in Australia at 55% of last week for 30 minutes")}),
            at(90, "Routes recover", _all_normal()),
        ),
        end_minutes=150,
        expect=(("raised", Overall.ELEVATED, False), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "grid-and-cyber", "NSW grid warning plus an unrelated cyber advisory",
        "On a hot afternoon AEMO declares an actual LOR2 in NSW, and ACSC publishes an advisory about "
        "active exploitation of a VPN product. Two categories are elevated, but about different things.",
        (
            CALM,
            at(5, "AEMO: actual LOR2 in NSW", {"aemo": Report(
                E, "Actual LOR2 in New South Wales: the grid is one big failure away from load shedding",
                key="aemo:lor2:NSW1")}),
            at(7, "ACSC: advisory about active exploitation", {"acsc": Report(
                E, 'New ACSC advisory: "Active exploitation of a VPN appliance vulnerability" '
                   "(mentions active exploitation)", key="acsc:vpn")}),
            at(60, "Both end", _all_normal()),
        ),
        end_minutes=120,
        compare=("If any two national signals backed each other up (national_needs_related_themes: false)",
                 {"scoring": {"national_needs_related_themes": False}}),
        expect=(),
    ),
    Scenario(
        "telco-cyber-attack", "Cyber attack on a telco",
        "ACSC publishes an alert about a cyber incident at an Australian telco, and IODA sees Telstra drop "
        "soon after. Cyber and internet trouble are related, so they back each other up.",
        (
            CALM,
            at(5, "ACSC: telco cyber incident", {"acsc": Report(
                E, 'New ACSC alert: "Cyber incident affecting an Australian telecommunications provider" '
                   "(mentions telecommunications)", key="acsc:telco")}),
            at(8, "IODA: Telstra dropping", {"ioda": Report(E, "Telstra (AS1221) is dropping on 2 methods")}),
            at(60, "Everything recovers", _all_normal()),
        ),
        end_minutes=120,
        expect=(("raised", Overall.ELEVATED, False), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "second-emergency", "A second emergency while already critical",
        "IODA sees Australia drop off the internet. Fifteen minutes later AEMO directs load shedding in "
        "NSW: a different critical event, so it gets its own message. Later updates about the same two "
        "events don't.",
        (
            CALM,
            at(5, "IODA: Australia down", {"ioda": Report(C, "Australia is down on 2 methods")}),
            at(20, "AEMO: load shedding in NSW", {"aemo": Report(
                C, "Load shedding in New South Wales: AEMO has told the networks to cut power to some customers",
                key="aemo:load_shed:NSW1", emergency=True)}),
            at(25, "Both still going, with new wording", {
                "ioda": Report(C, "Australia is down on 3 methods"),
                "aemo": Report(C, "Load shedding in New South Wales, and an actual LOR3",
                               key="aemo:load_shed:NSW1", emergency=True)}),
            at(90, "Everything recovers", _all_normal()),
        ),
        end_minutes=150,
        expect=(("raised", Overall.CRITICAL, True), ("another", Overall.CRITICAL, True),
                ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "police-helicopter", "A helicopter circles over Parramatta",
        "A police helicopter circles over Parramatta for 10 minutes, a headline says police are at a "
        "Parramatta shopping centre, and two people post about it. The headline and the posts make a story "
        "(news and social), and with the helicopter it's also a convergence of aircraft circling, news and "
        "posts in one area: Elevated. Circling alone does nothing.",
        (
            CALM,
            at(12, "ADS-B: a helicopter has circled Parramatta for 10 minutes", {}, items=(Item(
                "adsb", "Helicopter POL21 (AS50) circling near Parramatta for 10 minutes", 10, "info",
                placed(*PARRAMATTA, orbit=True, suburb="Parramatta", aircraft="POL21 (AS50)", helicopter=True)),)),
            at(20, "ABC: police at a Parramatta shopping centre", {}, items=(headline(
                "ABC News", "Heavy police presence at Parramatta shopping centre", *PARRAMATTA, place="Parramatta"),)),
            at(24, "Bluesky: someone posts about it", {}, items=(post(
                "helicopter has been hovering over Parramatta for ages", *PARRAMATTA),)),
            at(26, "Bluesky: someone else does too", {}, items=(post(
                "loads of police cars near Westfield Parramatta right now", *PARRAMATTA),)),
        ),
        end_minutes=200,
        expect=(("raised", Overall.ELEVATED, False), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "fuel-price-jump", "Fuel prices jump",
        "Diesel near home jumps 15 c/L past the highest it's been in 30 days. The fuel tile turns Elevated, "
        "but nothing else is about fuel, so the overall status stays Normal and no message is sent.",
        (
            CALM,
            at(10, "FuelCheck: diesel jumps", {"fuel": Report(
                E, "Diesel within 10 km of home is 229.9 c/L, 15.0 c/L above its highest in the last 30 days "
                   "(214.9; usually 199.9)", key="fuel:DL:home")}),
        ),
        end_minutes=60,
        expect=(),
    ),
    Scenario(
        "telco-ransomware", "A ransomware gang lists an Australian telco",
        "ransomware.live sees a gang list an Australian broadband provider. That's elevated (telecoms is a "
        "critical sector), but a listing is only a claim, so on its own it's nothing to alert on. Forty "
        "minutes later IODA sees the provider's network dropping. Cyber and internet trouble are related, so "
        "together they're Elevated.",
        (
            CALM,
            at(5, "ransomware.live: a telco listed", {"ransomware": Report(
                E, "New Australian ransomware victim in Telecoms: Example Broadband (Technology), listed by "
                   "examplegang (5 minutes ago)", key="ransomware:example-broadband")}),
            at(45, "IODA: the provider's network dropping", {"ioda": Report(
                E, "Example Broadband (AS64500) is dropping on 2 methods")}),
            at(120, "IODA recovers", {"ioda": Report(N, "No outages for Australia, NSW or 5 carriers")}),
        ),
        end_minutes=180,
        expect=(("raised", Overall.ELEVATED, False), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "sydney-cloud-outage", "AWS Sydney goes down",
        "AWS posts a service disruption for EC2 in Sydney. Your line and the telcos are fine, so on its own "
        "that's one category elevated: nothing to alert on. Then people start posting that sites are down "
        "across Australia. Both are about the internet in Australia, so together they're Elevated.",
        (
            CALM,
            at(5, "AWS: EC2 disruption in Sydney", {"cloud": Report(
                E, "AWS: Amazon Elastic Compute Cloud (Sydney): Service disruption: Increased error rates and "
                   "instance connectivity issues (posted 4 minutes ago)", key="cloud:AWS:ec2-sydney")}),
            at(20, "Bluesky: posts about sites being down", {"bluesky": Report(
                E, "Spike in posts about internet down: 18 people in the last 5 minutes (usually 2)",
                scope="national:internet", key="bluesky:internet_down")}),
            at(90, "Both recover", _all_normal()),
        ),
        end_minutes=150,
        expect=(("raised", Overall.ELEVATED, False), ("lowered", Overall.NORMAL, False)),
    ),
    Scenario(
        "foreign-advice-raised", "The US raises its travel advisory for Australia",
        "The US State Department raises Australia from Level 1 to Level 2. Foreign governments rarely "
        "change their advice about Australia, so a raised level is critical on its own, and stays critical "
        "for 24 hours (this replay ends after 2).",
        (
            CALM,
            at(30, "US: Australia raised to Level 2", {"advice": Report(
                C, "The US raised its travel advisory for Australia from Level 1 to Level 2: Exercise Increased "
                   "Caution", key="advice:us:level2")}),
        ),
        end_minutes=120,
        expect=(("raised", Overall.CRITICAL, True),),
    ),
    Scenario(
        "foreign-advice-routine", "Routine travel advice updates",
        "The US reissues its advisory at the same level, and the FCDO adds a note about the bushfire "
        "season. Neither is a change in level or about security, so they're only on the timeline.",
        (
            CALM,
            at(10, "US reissues Level 1; FCDO notes the bushfire season", {"advice": Report(
                N, "US: Level 1, exercise normal precautions (issued 31 Oct 2026); UK: doesn't advise against "
                   "travel to any part of Australia. Latest UK update 10 minutes ago")}, items=(
                Item("advice", "UK travel advice: Information about a higher risk of bushfires during the spring "
                               "and summer seasons ('Warnings and insurance' page).", 10),
            )),
        ),
        end_minutes=60,
        expect=(),
    ),
    Scenario(
        "back-after-outage", "Back online after a 3-hour outage",
        "Your internet is down for 3 hours. When it's back, the sources catch up on what happened "
        "meanwhile: an ACSC advisory, an AEMO LOR2 and a Bluesky spike, all from hours ago. They show on "
        "the dashboard, but none of it is new enough to alert on, so you get one summary instead.",
        (
            CALM,
            at(5, "Internet down", {}, offline=True),
            at(185, "Internet back; sources catch up", {
                "probes": Report(N, "Your router and all 8 outside checks answer"),
                "acsc": Report(E, 'ACSC advisory from 2 hours ago: "Active exploitation of a VPN appliance '
                                  'vulnerability" (mentions active exploitation)', key="acsc:vpn", backlog=True),
                "aemo": Report(E, "Actual LOR2 in New South Wales (declared 95 minutes ago)",
                               key="aemo:lor2:NSW1", backlog=True),
                "bluesky": Report(E, "Spike in posts about internet down an hour ago (catching up)",
                                  scope="national:internet", key="bluesky:internet_down", backlog=True),
            }, items=(
                Item("aemo", "AEMO: ACTUAL LACK OF RESERVE LEVEL 2 (LOR2) IN THE NSW REGION", 95, "elevated"),
                Item("acsc", "ACSC advisory: Active exploitation of a VPN appliance vulnerability", 120, "elevated"),
                Item("bluesky", "Spike in posts about internet down in Australia: 12 people", 60, "elevated"),
                Item("aemo", "AEMO: Forecast LOR1 in the NSW region", 150),
                Item("aemo", "AEMO: Cancellation of actual LOR2 in the NSW region", 30),
            )),
            at(240, "Everything calm", _all_normal()),
        ),
        end_minutes=300,
        expect=(("summary", Overall.NORMAL, False),),
    ),
)


# --- Playing a scenario ---


class Recorder(Notifier):
    """Keeps the messages instead of sending them."""

    name = "Replay"

    def __init__(self, db: Database, clock: Any) -> None:
        super().__init__(db, clock)
        self.sent: list[Alert] = []

    def queue(self, alert: Alert) -> None:
        self.sent.append(alert)

    async def check(self) -> None:
        pass

    async def send(self, alert: Alert) -> None:
        pass


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class Outcome:
    scenario: Scenario
    label: str  # "" for the settings in config.yaml
    dashboard: list[tuple[float, Overall, str]] = field(default_factory=list)  # (seconds in, level, reason)
    alerts: list[Alert] = field(default_factory=list)
    start: float = 0.0


class ReplayRuntime(Runtime):
    def __init__(self, cfg: Config, clock: Clock) -> None:
        super().__init__(cfg, clock)
        self.specs = [(stand_in_for(real), StandInSettings()) for real in REGISTRY.values()]


def replay_config(cfg: Config, folder: Path, overrides: dict[str, dict[str, Any]] | None = None) -> Config:
    update: dict[str, Any] = {
        "storage": cfg.storage.model_copy(update={"db_path": str(folder / "replay.db")}),
        "alerts": cfg.alerts.model_copy(update={"telegram": TelegramSettings()}),  # never the real bot
    }
    for section, values in (overrides or {}).items():
        update[section] = update.get(section, getattr(cfg, section)).model_copy(update=values)
    return cfg.model_copy(update=update)


async def play(cfg: Config, scenario: Scenario, label: str = "",
               overrides: dict[str, dict[str, Any]] | None = None) -> Outcome:
    start = 1_800_000_000.0  # any fixed moment will do
    clock = Clock(start)
    with tempfile.TemporaryDirectory(prefix="sitrep-replay-") as folder:
        runtime = ReplayRuntime(replay_config(cfg, Path(folder), overrides), clock)
        runtime.setup()
        recorder = Recorder(runtime.db, clock)
        runtime.alerts = Alerts(runtime.cfg.alerts, runtime.db, [recorder], started_at=start,
                                names={a.id: a.name for a in runtime.adapters.values()})
        outcome = Outcome(scenario, label, start=start)
        steps = sorted(scenario.steps, key=lambda s: s.at)
        step_no = 0
        shown = None
        tick = runtime.cfg.scoring.evaluate_every_seconds
        t = 0.0
        while t <= scenario.end_minutes * MIN:
            clock.now = start + t
            while step_no < len(steps) and steps[step_no].at <= t:
                play_step(runtime, steps[step_no], clock.now)
                step_no += 1
            runtime.evaluate()
            current = runtime.tracker.current
            if current.level != shown:
                outcome.dashboard.append((t, current.level, current.reason))
                shown = current.level
            t += tick
        outcome.alerts = list(recorder.sent)
        await runtime.stop()
    return outcome


async def play_all(cfg: Config, names: list[str] | None = None) -> list[Outcome]:
    chosen = [s for s in SCENARIOS if not names or s.name in names]
    outcomes = []
    for scenario in chosen:
        outcomes.append(await play(cfg, scenario))
        if scenario.compare:
            label, overrides = scenario.compare
            outcomes.append(await play(cfg, scenario, label, overrides))
    return outcomes


# --- Saying what happened ---

LEVEL_WORDS = {Overall.NORMAL: "Normal", Overall.ELEVATED: "Elevated", Overall.CRITICAL: "Critical",
               Overall.OFFLINE: "Can't reach the internet", Overall.UNKNOWN: "Starting up"}
KIND_WORDS = {"raised": "", "lowered": "all clear", "another": "another event", "summary": "while you were offline"}


def _when(seconds: float) -> str:
    return f"+{format_duration(seconds)}" if seconds else "start"


def describe(outcome: Outcome) -> str:
    s = outcome.scenario
    lines = []
    if outcome.label:
        lines.append(f"  {outcome.label}:")
    else:
        lines += [f"{s.title}  [{s.name}]", f"  {s.story}", "  What the sources report:"]
        for step in sorted(s.steps, key=lambda x: x.at)[1:]:
            lines.append(f"    {_when(step.at):>13}  {step.title}")
    lines.append("  Dashboard: " + "; ".join(
        f"{LEVEL_WORDS[level]} at {_when(t)}" for t, level, _ in outcome.dashboard if level != Overall.UNKNOWN))
    if not outcome.alerts:
        lines.append("  Messages: none")
    for alert in outcome.alerts:
        kind = KIND_WORDS.get(alert.kind, alert.kind)
        what = LEVEL_WORDS[alert.shown or alert.level] if alert.kind == "summary" else LEVEL_WORDS[alert.level]
        sound = "with a sound" if alert.loud else "silent"
        head = f"{what}{', ' + kind if kind else ''}"
        reason = alert.shown_reason if alert.kind == "summary" else alert.reason
        lines.append(f"  Message {_when(alert.at - outcome.start)}: {head} ({sound}). {reason[:160]}")
        if alert.kind == "summary" and alert.items:
            counts = ", ".join(f"{n} from {name}" for name, n in alert.items.get("counts", {}).items())
            lines.append(f"      It lists {alert.items.get('total', 0)} items that came in meanwhile ({counts}).")
    loud = [a for a in outcome.alerts if a.loud]
    verdict = ("would page you (a message with a sound)" if loud
               else "would send a silent message only" if outcome.alerts else "would not alert")
    lines.append(f"  Verdict: {verdict}.")
    return "\n".join(lines)


def report(outcomes: list[Outcome]) -> str:
    blocks = []
    for outcome in outcomes:
        text = describe(outcome)
        if outcome.label and blocks:
            blocks[-1] += "\n" + text
        else:
            blocks.append(text)
    return "\n\n".join(blocks)


def main(cfg: Config, names: list[str] | None) -> int:
    unknown = [n for n in names or [] if n not in {s.name for s in SCENARIOS}]
    if unknown:
        print(f"No scenario called {', '.join(unknown)}. Scenarios: {', '.join(s.name for s in SCENARIOS)}")
        return 2
    started = time.monotonic()
    outcomes = asyncio.run(play_all(cfg, names))
    print(report(outcomes))
    print(f"\n({len(outcomes)} replays with the timings in config.yaml, in {time.monotonic() - started:.1f} seconds. "
          "Nothing was sent.)")
    return 0
