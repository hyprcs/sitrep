"""Cloudflare Radar: reading its answers, judging them, and the adapter end to end.

The fixtures are real Radar answers (see tests/fixtures/radar):

- live_*: 29 October 2026, a quiet day for Australia;
- optus2023_*: 8 November 2023, when Optus's network went down nationally from
  about 4am Sydney time (traffic is that day against the week before).

Cases that haven't happened for real (a nationwide outage, a serious hijack)
are made by editing a real answer, and say so.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.radar import (
    RadarAdapter,
    RadarSettings,
    anomaly_reports,
    assess,
    judge_traffic,
    outage_reports,
    parse_hijacks,
    parse_leaks,
    parse_time,
    parse_traffic,
)
from sitrep.config import Carrier, Config, ConfigError, HomeSettings, load_config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.runtime import build_adapter_specs
from sitrep.state import StatusBoard

FIXTURES = Path(__file__).parent / "fixtures" / "radar"
ROOT = Path(__file__).parent.parent
AEDT = timezone(timedelta(hours=11))

CARRIERS = [
    Carrier(name="Telstra", asns=[1221]),
    Carrier(name="Optus", asns=[4804, 7474]),
    Carrier(name="TPG group", asns=[7545, 133612, 4802]),
    Carrier(name="Vocus", asns=[4826]),
    Carrier(name="Aussie Broadband", asns=[4764]),
]
SETTINGS = RadarSettings(api_token="test-token", carriers=CARRIERS)


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def sydney(*args: int) -> float:
    return datetime(*args, tzinfo=AEDT).timestamp()


def cut(payload, until: float):
    """A traffic answer as Radar would have given it at `until`: later points dropped."""
    payload = copy.deepcopy(payload)
    res = payload["result"]
    keep = [i for i, t in enumerate(res["today"]["timestamps"]) if parse_time(t) <= until]
    for name in ("today", "lastweek"):
        res[name]["timestamps"] = [res[name]["timestamps"][i] for i in keep]
        res[name]["values"] = [res[name]["values"][i] for i in keep]
    return payload


def scaled(payload, factor: float, last: int):
    """Today's last `last` readings multiplied by factor."""
    payload = copy.deepcopy(payload)
    values = payload["result"]["today"]["values"]
    for i in range(len(values) - last, len(values)):
        values[i] = str(float(values[i]) * factor)
    return payload


def traffic_status(name, kind, payload, now, s=SETTINGS):
    return judge_traffic(name, kind, parse_traffic(payload), s, now)


def live_now() -> float:
    """Shortly after the live traffic fixtures were captured (Radar runs ~90 minutes behind)."""
    return parse_traffic(fixture("live_traffic_AU")).times[-1] + 5400


# --- Reading answers ---


def test_times_with_and_without_z_are_utc():
    assert parse_time("2023-11-07T16:45:03Z") == parse_time("2023-11-07T16:45:03") == 1699375503
    assert parse_time(None) is None


def test_cloudflare_error_is_a_source_error():
    with pytest.raises(SourceError, match="Cloudflare said: Invalid dateRange"):
        outage_reports({"success": False, "errors": [{"message": "Invalid dateRange"}]}, "AU", "Australia", CARRIERS)


def test_other_countries_outages_are_ignored():
    assert outage_reports(fixture("live_outages"), "AU", "Australia", CARRIERS) == []
    assert anomaly_reports(fixture("live_anomalies"), "AU", "Australia", CARRIERS) == []


def test_optus_2023_outage_report_is_a_carrier_outage():
    reports = outage_reports(fixture("optus2023_outages"), "AU", "Australia", CARRIERS)
    assert len(reports) == 1  # Mexico and Gaza in the same days are left out
    optus = reports[0]
    assert optus.who == "Optus (AS4804)"
    assert optus.description == "Mobile and NBN services unavailable"
    assert not optus.nationwide
    assert optus.ongoing(sydney(2023, 11, 8, 8, 0)) and not optus.ongoing(sydney(2023, 11, 9, 8, 0))


def test_traffic_comparison_uses_one_scale():
    traffic = parse_traffic(fixture("live_traffic_AU"))
    ratio, as_of = traffic.latest()
    assert 0.9 < ratio < 1.1
    assert as_of == traffic.times[-1]
    assert traffic.times[1] - traffic.times[0] == 900


def test_leaks_are_counted_with_carriers_on_the_path():
    assert parse_leaks(fixture("live_leaks"), CARRIERS) == (6, ["Vocus"])


# --- Judging real days ---


def test_quiet_day_is_normal():
    now = live_now()
    traffic = [
        traffic_status("Australia", "country", fixture("live_traffic_AU"), now),
        traffic_status("Telstra", "carrier", fixture("live_traffic_telstra"), now),
    ]
    count, hijacks = parse_hijacks(fixture("live_hijacks"), CARRIERS, 8, now - 6 * 3600)
    result = assess([], hijacks, traffic, SETTINGS, now)
    assert (count, hijacks) == (1, [])  # one low-confidence hijack, of a network in the Seychelles
    assert result.level == Level.NORMAL
    assert result.reason == "Traffic in Australia at 102% of last week; carriers 103%; no outages reported"


def test_optus_2023_at_8am_is_elevated():
    now = sydney(2023, 11, 8, 8, 0)
    seen = now - 5400  # what Radar had published by then
    reports = outage_reports(fixture("optus2023_outages"), "AU", "Australia", CARRIERS)
    traffic = [
        traffic_status("Australia", "country", cut(fixture("optus2023_traffic_AU"), seen), now),
        traffic_status("Optus", "carrier", cut(fixture("optus2023_traffic_optus"), seen), now),
    ]
    result = assess(reports, [], traffic, SETTINGS, now)
    assert result.level == Level.ELEVATED
    assert result.reason == (
        "Cloudflare reports an outage on Optus (AS4804): Mobile and NBN services unavailable; "
        "Optus traffic at 8% of last week for 2.8 hours"
    )
    assert traffic[0].level == "normal"  # Australia as a whole only fell to about 90%


def test_optus_2023_just_after_the_drop_waits_for_30_minutes():
    now = sydney(2023, 11, 8, 6, 0)
    optus = traffic_status("Optus", "carrier", cut(fixture("optus2023_traffic_optus"), sydney(2023, 11, 8, 4, 0)), now)
    assert optus.ratio < 0.6 and optus.level == "normal"  # one low reading isn't enough


def test_optus_2023_next_day_is_normal():
    reports = outage_reports(fixture("optus2023_outages"), "AU", "Australia", CARRIERS)
    assert assess(reports, [], [], SETTINGS, sydney(2023, 11, 9, 9, 0)).level == Level.NORMAL


# --- Judging made-up cases ---


def test_nationwide_outage_report_is_critical():
    payload = fixture("optus2023_outages")  # edited: the Optus report without its network
    payload["result"]["annotations"][0]["asns"] = []
    payload["result"]["annotations"][0]["asnsDetails"] = []
    reports = outage_reports(payload, "AU", "Australia", CARRIERS)
    assert reports[0].nationwide
    result = assess(reports, [], [], SETTINGS, sydney(2023, 11, 8, 8, 0))
    assert result.level == Level.CRITICAL
    assert result.reason.startswith("Cloudflare reports a nationwide outage in Australia")


def test_country_traffic_below_half_for_30_minutes_is_critical():
    now = live_now()
    country = traffic_status("Australia", "country", scaled(fixture("live_traffic_AU"), 0.4, last=2), now)
    result = assess([], [], [country], SETTINGS, now)
    assert result.level == Level.CRITICAL
    assert result.reason == "Traffic in Australia at 41% of last week for 30 minutes"


def test_carrier_traffic_below_half_is_only_elevated():
    now = live_now()
    telstra = traffic_status("Telstra", "carrier", scaled(fixture("live_traffic_telstra"), 0.4, last=4), now)
    assert telstra.level == "elevated" and telstra.minutes_low == 60


def test_stale_traffic_numbers_are_not_judged():
    now = live_now() + 5 * 3600
    country = traffic_status("Australia", "country", scaled(fixture("live_traffic_AU"), 0.2, last=8), now)
    assert country.level == "normal"
    assert assess([], [], [country], SETTINGS, now).reason.startswith("Traffic numbers are 6.5 hours old")


def test_confirmed_anomaly_on_a_carrier_is_elevated():
    payload = fixture("live_anomalies")  # edited: Russia's ongoing anomaly moved to Telstra
    anomaly = next(a for a in payload["result"]["trafficAnomalies"] if a["endDate"] is None)
    anomaly["asnDetails"] = {"asn": "1221", "name": "ASN-TELSTRA", "location": {"code": "AU", "name": "Australia"}}
    unconfirmed = copy.deepcopy(anomaly) | {"status": "UNVERIFIED", "uuid": "x"}
    payload["result"]["trafficAnomalies"].append(unconfirmed)
    reports = anomaly_reports(payload, "AU", "Australia", CARRIERS)
    assert [r.who for r in reports] == ["Telstra (AS1221)"]
    now = reports[0].start + 3 * 3600
    result = assess(reports, [], [], SETTINGS, now)
    assert result.level == Level.ELEVATED
    assert result.reason == "Cloudflare confirmed a traffic drop on Telstra (AS1221) (3 hours)"


def _hijack_answer(confidence: int, last_seen: str):
    payload = fixture("live_hijacks")  # edited: the real event aimed at Telstra instead
    event = payload["result"]["events"][0]
    event.update(victim_asns=[1221], confidence_score=confidence, max_hijack_ts=last_seen)
    return payload


def test_high_confidence_hijack_of_a_carrier_is_elevated():
    now = parse_time("2026-10-28T14:00:00Z")
    _, hijacks = parse_hijacks(_hijack_answer(9, "2026-10-28T12:21:47.035"), CARRIERS, 8, now - 6 * 3600)
    assert len(hijacks) == 1
    result = assess([], hijacks, [], SETTINGS, now)
    assert result.level == Level.ELEVATED
    assert result.reason == (
        "Possible hijack of Telstra (AS1221)'s addresses by AS136557 (confidence 9), seen 1.6 hours ago"
    )


@pytest.mark.parametrize(("confidence", "last_seen"), [(5, "2026-10-28T12:21:47"), (9, "2026-10-28T06:00:00")])
def test_weak_or_old_hijacks_dont_count(confidence, last_seen):
    now = parse_time("2026-10-28T14:00:00Z")
    _, hijacks = parse_hijacks(_hijack_answer(confidence, last_seen), CARRIERS, 8, now - 6 * 3600)
    assert hijacks == []


# --- Settings ---


def test_shipped_config_shares_the_carriers_with_radar():
    cfg = load_config(ROOT / "config.yaml", env_file=None)
    radar = next(s for cls, s in build_adapter_specs(cfg) if cls.id == "radar")
    assert [c.name for c in radar.carriers] == [c.name for c in cfg.carriers]


def test_home_carrier_must_be_listed():
    with pytest.raises(ValueError, match="home.carrier is 'Belong'"):
        Config(home=HomeSettings(carrier="Belong"), carriers=CARRIERS)


def test_token_never_shows_in_settings_output():
    assert "test-token" not in repr(SETTINGS) and "test-token" not in str(SETTINGS.model_dump())


# --- The adapter against a fake Cloudflare ---


class FakeCloudflare:
    def __init__(self, expires_on: str = "2027-10-29T12:59:59Z") -> None:
        self.requests: list[httpx.Request] = []
        self.expires_on = expires_on
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"success": False, "errors": [{"message": "nope"}]})
        path = request.url.path
        if path.endswith("/user/tokens/verify"):
            return httpx.Response(200, json={"success": True, "errors": [], "result": {"status": "active", "expires_on": self.expires_on}})
        names = {
            "annotations/outages": "live_outages", "traffic_anomalies": "live_anomalies",
            "hijacks/events": "live_hijacks", "leaks/events": "live_leaks",
        }
        for part, name in names.items():
            if path.endswith(part):
                return httpx.Response(200, json=fixture(name))
        if path.endswith("netflows/timeseries"):
            query = parse_qs(request.url.query.decode())
            return httpx.Response(200, json=fixture("live_traffic_AU" if "location" in query else "live_traffic_telstra"))
        return httpx.Response(404)

    def calls(self, part: str) -> list[httpx.Request]:
        return [r for r in self.requests if part in r.url.path]


@pytest.fixture
def make_adapter():
    made = []

    def make(fake, clock, token="test-token"):
        db = Database(":memory:")
        cfg = Config(home=HomeSettings(carrier="Telstra"), carriers=CARRIERS)
        http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        made.append(db)
        settings = SETTINGS.model_copy(update={"api_token": RadarSettings(api_token=token).api_token})
        return RadarAdapter(AdapterContext(cfg, db, StatusBoard(db, clock), http, clock), settings)

    yield make
    for db in made:
        db.close()


async def test_poll_asks_for_everything_with_the_token(make_adapter):
    fake = FakeCloudflare()
    now = live_now()
    result = await make_adapter(fake, lambda: now).poll()
    assert result.level == Level.NORMAL
    assert all(r.headers["Authorization"] == "Bearer test-token" for r in fake.requests)
    assert len(fake.calls("/netflows/timeseries")) == 1 + len(CARRIERS)
    optus = parse_qs(next(r for r in fake.calls("/netflows/timeseries") if "4804" in str(r.url)).url.query.decode())
    assert optus["asn"] == ["4804,7474", "4804,7474"] and optus["dateRange"] == ["1d", "1dcontrol"]
    assert result.details["chart"]["step"] == 900
    assert result.details["leak_carriers"] == ["Vocus"]
    assert next(t for t in result.details["traffic"] if t["name"] == "Telstra")["home"]


async def test_token_is_checked_once_a_day(make_adapter):
    fake = FakeCloudflare()
    clock = {"now": live_now()}
    adapter = make_adapter(fake, lambda: clock["now"])
    await adapter.poll()
    clock["now"] += 600
    await adapter.poll()
    assert len(fake.calls("/user/tokens/verify")) == 1
    clock["now"] += 86400
    await adapter.poll()
    assert len(fake.calls("/user/tokens/verify")) == 2


async def test_token_expiring_soon_is_mentioned(make_adapter):
    now = live_now()
    soon = datetime.fromtimestamp(now + 5 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = await make_adapter(FakeCloudflare(expires_on=soon), lambda: now).poll()
    assert result.level == Level.NORMAL
    assert "Your Cloudflare token expires on" in result.reason


async def test_missing_token_says_what_to_do(make_adapter):
    fake = FakeCloudflare()
    with pytest.raises(SourceError, match="add CLOUDFLARE_API_TOKEN to .env"):
        await make_adapter(fake, live_now, token="").poll()
    assert fake.requests == []  # nothing is sent without a token


async def test_refused_token_says_to_make_a_new_one(make_adapter):
    fake = FakeCloudflare()
    fake.status = 403
    with pytest.raises(SourceError, match="Cloudflare refused the token"):
        await make_adapter(fake, live_now).poll()


async def test_slow_down_carries_retry_after(make_adapter):
    adapter = make_adapter(lambda request: httpx.Response(429, headers={"Retry-After": "60"}), live_now)
    with pytest.raises(SourceError, match="slow down") as caught:
        await adapter.poll()
    assert caught.value.retry_after == 60
