"""IODA: reading alerts, judging them, the charts, and the adapter end to end.

The fixtures are made-up answers in IODA's format (see tests/fixtures/ioda). IODA's
data isn't licensed for redistribution, so every number is invented, but each one
copies the shape of a moment seen on IODA:

- oct18_*: 18 October 2026, when active probing for Australia, NSW and every big
  carrier fell to about 20% of usual for an hour while routing stayed flat;
- sep03_*: 3 September 2026, when TPG dipped on probing alone, on and off all day;
- live_alerts_asn_ongoing: a routing outage still going (AS23922, a small network);
- live_alerts_asn_other: network telescope noise (AS21859);
- live_alerts_*: a quiet fortnight for Australia, NSW and the carriers.

To replay a moment, alerts after it are dropped, which is what IODA would have
returned then.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.ioda import (
    ALERT_LIMIT,
    Alert,
    IodaAdapter,
    IodaSettings,
    assess,
    combine,
    current_drops,
    normalise,
    parse_alerts,
    parse_signals,
)
from sitrep.config import Config, HomeSettings, load_config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.runtime import build_adapter_specs
from sitrep.state import StatusBoard

FIXTURES = Path(__file__).parent / "fixtures" / "ioda"
ROOT = Path(__file__).parent.parent

SETTINGS = IodaSettings.model_validate(
    {
        "areas": [
            {"name": "Australia", "type": "country", "code": "AU"},
            {"name": "NSW", "type": "region", "code": 173},
        ],
        "carriers": [
            {"name": "Telstra", "asns": [1221]},
            {"name": "Optus", "asns": [4804, 7474]},
            {"name": "TPG group", "asns": [7545, 133612, 4802]},
            {"name": "Vocus", "asns": [4826]},
            {"name": "Aussie Broadband", "asns": [4764]},
        ],
    }
)
KINDS = {"country": "country", "region": "region", "asn": "asn"}


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


AEST = timezone(timedelta(hours=10))  # Sydney in winter
AEDT = timezone(timedelta(hours=11))  # Sydney from the first Sunday in October (4 Oct 2026)


def sydney(text: str) -> float:
    """A Sydney wall-clock time in 2026, as a timestamp."""
    moment = datetime.strptime(text, "%Y-%m-%d %H:%M")
    return moment.replace(tzinfo=AEDT if moment >= datetime(2026, 10, 4, 3) else AEST).timestamp()


def alerts_at(name: str, now: float) -> list[Alert]:
    return [a for a in parse_alerts(fixture(name)) if a.time <= now]


def replay(prefix: str, now: float, s: IodaSettings = SETTINGS, home: str = "Telstra"):
    drops = {kind: current_drops(alerts_at(f"{prefix}_alerts_{kind}", now), s.ignore_methods) for kind in KINDS}
    return assess(drops, s, now, home)


def entity(result, name):
    return next(e for e in result.details["entities"] if e["name"] == name)


def critical(code: str, method: str, ts: float, value: float, usual: float) -> Alert:
    return Alert(code, method, ts, True, value, usual)


# --- Real moments ---


def test_quiet_fortnight_is_normal():
    now = float(fixture("live_alerts_asn")["requestParameters"]["until"])
    result = replay("live", now)
    assert result.level == Level.NORMAL
    assert result.reason == "No outages for Australia, NSW or 5 carriers"
    assert {e["level"] for e in result.details["entities"]} == {"normal"}


def test_probing_glitch_on_18_october_is_elevated_with_a_hint():
    result = replay("oct18", sydney("2026-10-18 11:30"))
    assert result.level == Level.ELEVATED
    assert result.reason.startswith("Australia and NSW down on active probing (21% of usual) for 30 minutes")
    assert "glitch in IODA's own measurements" in result.reason
    # Every carrier dipped on one method only, so none counts as down.
    carriers = [e for e in result.details["entities"] if e["kind"] == "carrier"]
    assert {e["level"] for e in carriers} == {"watch"}
    telstra = entity(result, "Telstra")
    assert telstra["home"] and telstra["detail"] == "Active probing at 18% of usual for 30 minutes"
    assert entity(result, "Optus")["detail"].startswith("AS4804: ")


def test_18_october_after_recovery_is_normal():
    assert replay("oct18", sydney("2026-10-18 12:30")).level == Level.NORMAL


def test_single_method_carrier_dip_is_shown_but_not_counted():
    now = sydney("2026-09-03 05:00")
    drops = {"asn": current_drops(alerts_at("sep03_alerts_asn_tpg", now))}
    result = assess(drops, SETTINGS, now)
    assert result.level == Level.NORMAL
    assert "TPG group dipped on active probing (61% of usual) for 40 minutes" in result.reason
    assert "one method isn't enough on its own" in result.reason
    assert entity(result, "TPG group")["label"] == "Dip"


def test_ongoing_outage_counts_from_its_first_alert():
    payload = fixture("live_alerts_asn_ongoing")
    now = float(payload["requestParameters"]["until"])
    s = IodaSettings.model_validate({"carriers": [{"name": "Box Hill", "asns": [23922]}], "carrier_down_methods": 1})
    result = assess({"asn": current_drops(parse_alerts(payload), s.ignore_methods)}, s, now)
    assert result.level == Level.ELEVATED
    assert result.reason.startswith("Box Hill down on routing (28% of usual) for 7")
    assert result.reason.endswith("days")


def test_network_telescope_is_ignored_by_default():
    now = sydney("2026-10-28 05:10")  # just after one of AS21859's many telescope alerts
    alerts = alerts_at("live_alerts_asn_other", now)
    zen = {"carriers": [{"name": "Zenlayer", "asns": [21859]}]}
    quiet = IodaSettings.model_validate(zen)
    noisy = IodaSettings.model_validate({**zen, "ignore_methods": []})
    assert assess({"asn": current_drops(alerts, quiet.ignore_methods)}, quiet, now).details["entities"][0]["level"] == "normal"
    assert assess({"asn": current_drops(alerts, noisy.ignore_methods)}, noisy, now).details["entities"][0]["level"] == "watch"


# --- The rules, with one real alert plus a made-up second method ---


def test_area_down_on_two_methods_is_critical():
    now = sydney("2026-10-18 11:30")
    alerts = alerts_at("oct18_alerts_country", now) + [critical("AU", "bgp", now - 600, 160_000, 184_830)]
    result = assess({"country": current_drops(alerts)}, SETTINGS, now)
    assert result.level == Level.CRITICAL
    assert result.reason.startswith(
        "Australia down on routing (87% of usual) and active probing (21%) for 30 minutes"
    )
    assert "glitch" not in result.reason


def _carriers_down(*asns: str, now: float) -> dict:
    alerts = []
    for asn in asns:
        alerts += [critical(asn, "bgp", now - 900, 10, 100), critical(asn, "ping-slash24", now - 600, 20, 100)]
    return {"asn": current_drops(alerts)}


def test_one_carrier_down_on_two_methods_is_elevated():
    now = 1_800_000_000
    result = assess(_carriers_down("1221", now=now), SETTINGS, now, "Telstra")
    assert result.level == Level.ELEVATED
    assert result.reason == (
        "Telstra (your network) down on routing (10% of usual) and active probing (20%) for 15 minutes"
    )
    assert entity(result, "Telstra")["label"] == "Down"


def test_two_carriers_down_is_critical_and_your_network_comes_first():
    now = 1_800_000_000
    result = assess(_carriers_down("4804", "1221", now=now), SETTINGS, now, "telstra")
    assert result.level == Level.CRITICAL
    assert result.reason == (
        "Telstra (your network) and Optus down on routing (10% of usual) and active probing (20%) for 15 minutes"
    )


def test_a_carrier_counts_its_worst_network():
    now = 1_800_000_000
    result = assess(_carriers_down("7474", now=now), SETTINGS, now)  # Optus's second ASN
    assert entity(result, "Optus")["detail"].startswith("AS7474: routing at 10%")


def test_recovery_ends_a_drop_and_the_next_one_starts_fresh():
    alerts = [
        critical("1221", "bgp", 100, 50, 100),
        critical("1221", "bgp", 200, 40, 100),
        Alert("1221", "bgp", 300, False, 100, 100),
        critical("1221", "bgp", 400, 30, 100),
    ]
    drop = current_drops(alerts)["1221"]["bgp"]
    assert (drop.since, drop.ratio) == (400, 0.3)
    run = current_drops(alerts[:2])["1221"]["bgp"]
    assert (run.since, run.ratio) == (100, 0.4)


# --- Reading responses ---


def test_ioda_error_field_is_a_source_error():
    with pytest.raises(SourceError, match="IODA said: invalid entity"):
        parse_alerts({"data": None, "error": "invalid entity"})


def test_unreadable_alert_is_a_source_error():
    with pytest.raises(SourceError, match="can't read"):
        parse_alerts({"data": [{"datasource": "bgp"}], "error": None})


def test_charts_combine_a_carriers_networks():
    series = parse_signals(fixture("oct18_signals_asn_ping"))
    parts = [series[("asn", code)] for code in ("7545", "133612", "4802")]
    tpg = combine(parts)
    assert len(tpg.values) == 144
    assert tpg.values[0] == sum(p.values[0] for p in parts)
    gaps = [i for i, v in enumerate(tpg.values) if v is None]
    assert gaps and all(any(p.values[i] is None for p in parts) for i in gaps)  # a gap anywhere is a gap
    chart = normalise(tpg)
    assert chart["step"] == 600
    known = sorted(v for v in chart["values"] if v is not None)
    assert known[len(known) // 2] == pytest.approx(1.0, abs=0.01)


def test_charts_handle_missing_data():
    assert normalise(None) is None
    assert combine([None, None]) is None
    country = parse_signals(fixture("oct18_signals_country_ping"))[("country", "AU")]
    assert normalise(country)["values"][0] == pytest.approx(1.0, abs=0.02)


# --- Settings ---


def test_settings_need_something_to_watch():
    with pytest.raises(ValueError, match="at least one area or carrier"):
        IodaSettings.model_validate({})


def test_region_code_can_be_written_as_a_number():
    assert SETTINGS.areas[1].code == "173"


def test_shipped_config_is_valid():
    cfg = load_config(ROOT / "config.yaml", env_file=None)
    ids = [cls.id for cls, _ in build_adapter_specs(cfg)]
    assert "ioda" in ids


# --- The adapter against a fake IODA ---


class FakeIoda:
    """Answers like IODA would have at `now`, from the fixtures."""

    def __init__(self, prefix: str, now: float) -> None:
        self.prefix = prefix
        self.now = now
        self.requests: list[httpx.Request] = []
        self.fail_signals = False
        self.full_page_for_days: float | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        path = request.url.path
        if path.endswith("/outages/alerts"):
            days = (int(params["until"]) - int(params["from"])) / 86400
            if self.full_page_for_days and days >= self.full_page_for_days:
                item = fixture(f"{self.prefix}_alerts_asn")["data"][0]
                return httpx.Response(200, json={"data": [item] * ALERT_LIMIT, "error": None})
            payload = fixture(f"{self.prefix}_alerts_{params['entityType']}")
            payload["data"] = [a for a in payload["data"] if a["time"] <= self.now]
            return httpx.Response(200, json=payload)
        if "/signals/raw/" in path:
            if self.fail_signals:
                return httpx.Response(503)
            kind = path.split("/")[-2]
            if kind == "region":
                return httpx.Response(200, json={"data": [], "error": None})
            name = "asn" if kind == "asn" else "country"
            return httpx.Response(200, json=fixture(f"{self.prefix}_signals_{name}_ping"))
        return httpx.Response(404)

    def calls(self, part: str) -> list[httpx.Request]:
        return [r for r in self.requests if part in r.url.path]


@pytest.fixture
def make_adapter():
    made = []

    def make(fake: FakeIoda, clock):
        db = Database(":memory:")
        cfg = Config(home=HomeSettings(carrier="Telstra"), carriers=SETTINGS.carriers)
        http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        ctx = AdapterContext(cfg, db, StatusBoard(db, clock), http, clock)
        made.append((db, http))
        return IodaAdapter(ctx, SETTINGS)

    yield make
    for db, _ in made:
        db.close()


async def test_poll_asks_once_per_kind_and_judges_the_answer(make_adapter):
    now = sydney("2026-10-18 11:30")
    fake = FakeIoda("oct18", now)
    adapter = make_adapter(fake, lambda: now)
    result = await adapter.poll()
    assert result.level == Level.ELEVATED

    alerts = fake.calls("/outages/alerts")
    assert len(alerts) == 3
    asks = {parse_qs(r.url.query.decode())["entityType"][0]: parse_qs(r.url.query.decode()) for r in alerts}
    assert asks["asn"]["entityCode"] == ["1221,4804,7474,7545,133612,4802,4826,4764"]
    assert asks["region"]["entityCode"] == ["173"]
    assert int(asks["country"]["until"][0]) - int(asks["country"]["from"][0]) == 14 * 86400

    signals = fake.calls("/signals/raw/")
    assert sorted(r.url.path.split("/")[-2] for r in signals) == ["asn", "country", "region"]
    assert all(parse_qs(r.url.query.decode())["datasource"] == ["ping-slash24"] for r in signals)
    tpg = entity(result, "TPG group")
    assert len(tpg["chart"]["values"]) == 144
    assert entity(result, "NSW")["chart"] is None  # this fake has no NSW series
    assert result.details["charts_at"] == now


async def test_charts_are_fetched_every_15_minutes_not_every_poll(make_adapter):
    clock = {"now": sydney("2026-10-18 11:30")}
    fake = FakeIoda("oct18", clock["now"])
    adapter = make_adapter(fake, lambda: clock["now"])
    await adapter.poll()
    clock["now"] += 300
    result = await adapter.poll()
    assert len(fake.calls("/signals/raw/")) == 3
    assert entity(result, "Telstra")["chart"] is not None  # kept from the first poll
    clock["now"] += 600
    await adapter.poll()
    assert len(fake.calls("/signals/raw/")) == 6


async def test_failing_charts_dont_fail_the_poll(make_adapter):
    now = sydney("2026-10-18 12:30")
    fake = FakeIoda("oct18", now)
    fake.fail_signals = True
    adapter = make_adapter(fake, lambda: now)
    result = await adapter.poll()
    assert result.level == Level.NORMAL
    assert result.details["charts_at"] is None
    assert entity(result, "Telstra")["chart"] is None


async def test_a_full_page_of_alerts_asks_again_for_less(make_adapter):
    now = sydney("2026-10-18 11:30")
    fake = FakeIoda("oct18", now)
    fake.full_page_for_days = 14
    adapter = make_adapter(fake, lambda: now)
    await adapter.poll()
    spans = sorted(
        (int(q["until"][0]) - int(q["from"][0])) / 86400
        for q in (parse_qs(r.url.query.decode()) for r in fake.calls("/outages/alerts"))
    )
    assert spans == [1, 1, 1, 14, 14, 14]


async def test_slow_down_answer_carries_retry_after(make_adapter):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "120"})

    adapter = make_adapter(handler, lambda: 1_800_000_000)
    with pytest.raises(SourceError, match="slow down") as caught:
        await adapter.poll()
    assert caught.value.retry_after == 120


async def test_server_error_fails_the_poll(make_adapter):
    adapter = make_adapter(lambda request: httpx.Response(502), lambda: 1_800_000_000)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.poll()
