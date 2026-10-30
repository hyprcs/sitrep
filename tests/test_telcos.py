"""Telco status pages: reading TPG and Aussie Broadband, judging outages, and the adapter end to end.

The fixtures (tests/fixtures/telcos) are made-up feeds in TPG's and Aussie Broadband's
formats, as they might look on 29 October 2026 at 4:30 pm Sydney time: eight TPG
maintenance windows and one small unplanned Aussie Broadband outage. The places, times
and wording are invented. Bigger outages are made up in the tests below.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.telcos import (
    Outage,
    TelcosAdapter,
    TelcosSettings,
    _area_name,
    assess,
    judge,
    parse_aussie,
    parse_tpg,
    postcode_state,
    tpg_categories,
)
from sitrep.config import Carrier, Config, HomeSettings, load_config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.runtime import build_adapter_specs
from sitrep.state import StatusBoard
from sitrep.util import sydney_timestamp

FIXTURES = Path(__file__).parent / "fixtures" / "telcos"
ROOT = Path(__file__).parent.parent
SETTINGS = TelcosSettings()
AEDT = timezone(timedelta(hours=11))
NOW = datetime(2026, 10, 29, 16, 30, tzinfo=AEDT).timestamp()


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def tpg_outages():
    return parse_tpg(load("tpg_now_36.json"), "NBN")


def tpg_item(**changes) -> dict:
    item = copy.deepcopy(load("tpg_now_36.json")["data"][0])
    item.update(changes)
    return item


def aussie_with(**changes):
    data = load("aussie_outages.json")
    item = next(iter(data[0]["current"].values()))
    item.update(changes)
    return data


# --- Times and places ---


@pytest.mark.parametrize("wall, offset", [
    (datetime(2026, 10, 23, 8, 0), 11),  # AEDT
    (datetime(2026, 7, 1, 8, 0), 10),  # AEST
    (datetime(2026, 4, 5, 2, 30), 11),  # summer time ends at 3 am on the first Sunday in April
    (datetime(2026, 4, 5, 3, 30), 10),
    (datetime(2026, 10, 4, 1, 59), 10),  # and starts at 2 am on the first Sunday in October
    (datetime(2026, 10, 4, 3, 0), 11),
    (datetime(2027, 1, 15, 12, 0), 11),
])
def test_sydney_time(wall, offset):
    assert sydney_timestamp(wall) == wall.replace(tzinfo=timezone(timedelta(hours=offset))).timestamp()


@pytest.mark.parametrize("code, state", [
    (2113, "NSW"), (2000, "NSW"), (2620, "NSW"), (2600, "ACT"), (2913, "ACT"),
    (3000, "VIC"), (4000, "QLD"), (5000, "SA"), (6753, "WA"), (7000, "TAS"), (870, "NT"), (9999, None),
])
def test_postcode_states(code, state):
    assert postcode_state(code) == state


def test_area_names():
    assert _area_name("NEWMAN(6753)") == ("Newman", "WA")
    assert _area_name("NBN CVC: Aspley (Link 5)") == ("Aspley", None)
    assert _area_name("PARRAMATTA (2150)") == ("Parramatta", "NSW")


# --- Reading TPG ---


def test_tpg_summary_names_services_with_incidents_now():
    assert tpg_categories(load("tpg_aggregated.json"), SETTINGS.tpg_groups) == [("36", "NBN")]
    assert tpg_categories(load("tpg_aggregated.json"), ["Others"]) == []


def test_tpg_maintenance_windows_are_planned():
    outages = tpg_outages()
    assert len(outages) == 8 and all(o.planned for o in outages)
    assert sum("NSW" in o.states for o in outages) == 3
    newtown = next(o for o in outages if o.id == "900101")
    assert (newtown.where, newtown.states, newtown.service) == ("Newtown (NSW)", ("NSW",), "NBN")
    assert newtown.since == datetime(2026, 10, 26, 0, 0, tzinfo=AEDT).timestamp()
    assert newtown.until == datetime(2026, 10, 30, 19, 0, tzinfo=AEDT).timestamp()


def test_tpg_staff_details_are_never_kept():
    raw = load("tpg_now_36.json")
    raw["data"][0]["submitting_user"] = "A Person <a.person@example.com>"
    outage = parse_tpg(raw, "NBN")[0]
    assert "example.com" not in json.dumps(outage.__dict__)
    assert "submitting_user" not in (FIXTURES / "tpg_now_36.json").read_text(encoding="utf-8")


def test_tpg_scope():
    many = parse_tpg({"data": [tpg_item(affected_sites="NSW + VIC", heading="Sydney and Melbourne Customers")]}, "NBN")[0]
    assert many.states == ("NSW", "VIC") and not many.national
    everyone = parse_tpg({"data": [tpg_item(affected_sites="ALL", heading="All Customers")]}, "NBN")[0]
    assert everyone.national and everyone.scope == "nationwide"
    hidden = parse_tpg({"data": [tpg_item(internal_only="1")]}, "NBN")
    assert hidden == []


def test_broken_tpg_data_is_a_source_error():
    with pytest.raises(SourceError, match="TPG"):
        parse_tpg({"nope": 1}, "NBN")
    with pytest.raises(SourceError, match="TPG"):
        tpg_categories([], ["Internet"])


# --- Reading Aussie Broadband ---


def test_aussie_feed_has_one_current_outage():
    outages = parse_aussie(load("aussie_outages.json"))
    assert len(outages) == 1
    newman = outages[0]
    assert (newman.where, newman.states, newman.service, newman.planned) == ("Newman (WA)", ("WA",), "NBN", False)
    assert newman.since == datetime(2026, 10, 27, 22, 32, 18, tzinfo=timezone.utc).timestamp()
    assert newman.until is None and newman.customers is None and not newman.major


def test_aussie_customer_numbers_and_acma_class():
    public = "* Description: A network fault is affecting nbn services in Pilbara Region of WA.<br />* Impacted customers: 12,541<br />"
    outage = parse_aussie(aussie_with(public=public, acma_classification="Major Outage"))[0]
    assert outage.customers == 12541 and outage.major and outage.note == "ACMA: Major Outage"
    by_field = parse_aussie(aussie_with(acma_premises_affected=900))[0]
    assert by_field.customers == 900


def test_aussie_multiple_areas_and_maintenance():
    outage = parse_aussie(aussie_with(title="Network Maintenance", areas=["NBN CVC: Aspley (Link 5)", "NBN CVC: Caboolture"]))[0]
    assert outage.planned and outage.where == "Aspley and 1 other area" and outage.until is not None


def test_broken_aussie_data_is_a_source_error():
    with pytest.raises(SourceError, match="Aussie Broadband"):
        parse_aussie({"current": {}})


# --- Judging ---


def test_live_day_is_normal():
    tpg = judge("TPG", SETTINGS.tpg_page, tpg_outages(), SETTINGS, NOW)
    aussie = judge("Aussie Broadband", SETTINGS.aussie_page, parse_aussie(load("aussie_outages.json")), SETTINGS, NOW)
    result = assess([tpg, aussie], [], SETTINGS)
    assert result.level == Level.NORMAL
    assert result.reason == (
        "TPG: no unplanned outages (8 maintenance windows now, 3 in NSW); "
        "Aussie Broadband: 1 unplanned outage, none in NSW: Newman (WA), NBN, for 31 hours (no maintenance now)"
    )


def outage(**kw) -> Outage:
    return Outage(**{"telco": "TPG", "id": "1", "where": "Somewhere", "service": "NBN", "planned": False, **kw})


def test_widespread_unplanned_outage_is_elevated():
    national = outage(where="All", national=True, since=NOW - 1800)
    status = judge("TPG", "", [national, *tpg_outages()], SETTINGS, NOW)
    assert status.level == Level.ELEVATED.value
    assert status.summary.startswith("1 unplanned outage, 1 in NSW: All, nationwide, NBN, for 30 minutes")
    two_states = judge("TPG", "", [outage(states=("NSW", "VIC"))], SETTINGS, NOW)
    assert two_states.level == Level.ELEVATED.value


def test_many_outages_in_your_state_are_elevated():
    four = [outage(id=str(i), states=("NSW",)) for i in range(4)]
    assert judge("TPG", "", four, SETTINGS, NOW).level == Level.NORMAL.value
    five = four + [outage(id="5", states=("NSW",))]
    status = judge("TPG", "", five, SETTINGS, NOW)
    assert status.level == Level.ELEVATED.value and status.summary.startswith("5 unplanned outages, 5 in NSW. Biggest:")


def test_aussie_major_or_large_outages_are_elevated():
    for changes in ({"prio": "major"}, {"prio": "critical"}, {"acma_classification": "Significant Local Outage"},
                    {"acma_premises_affected": 15000}):
        outages = parse_aussie(aussie_with(**changes))
        assert judge("Aussie Broadband", "", outages, SETTINGS, NOW).level == Level.ELEVATED.value, changes


def test_planned_work_is_never_elevated():
    planned = [outage(id=str(i), planned=True, national=True, states=("NSW",)) for i in range(20)]
    status = judge("TPG", "", planned, SETTINGS, NOW)
    assert status.level == Level.NORMAL.value and status.summary == "no unplanned outages (20 maintenance windows now, 20 in NSW)"


def test_both_feeds_failing_fails_the_source():
    from sitrep.adapters.telcos import TelcoStatus
    with pytest.raises(SourceError, match="Couldn't check TPG \\(timed out\\) or Aussie Broadband \\(HTTP 503\\)"):
        assess([TelcoStatus("TPG", "", error="timed out"), TelcoStatus("Aussie Broadband", "", error="HTTP 503")], [], SETTINGS)


def test_shipped_config_is_valid():
    cfg = load_config(ROOT / "config.yaml", env_file=None)
    telcos = next(s for cls, s in build_adapter_specs(cfg) if cls.id == "telcos")
    assert telcos.tpg and telcos.aussie_broadband and telcos.state == "NSW"
    assert [link.name for link in telcos.links] == ["Telstra", "Belong", "Optus", "Vodafone", "iiNet", "NBN Co", "Vocus"]


# --- The adapter against fake TPG and Aussie Broadband servers ---


class FakeTelcos:
    def __init__(self, tpg_status=200, aussie_status=200, aussie=None) -> None:
        self.requests: list[httpx.Request] = []
        self.tpg_status = tpg_status
        self.aussie_status = aussie_status
        self.aussie = aussie or load("aussie_outages.json")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "wrapper.aussiebroadband.com.au":
            return httpx.Response(self.aussie_status, json=self.aussie)
        if self.tpg_status != 200:
            return httpx.Response(self.tpg_status)
        if path.endswith("/aggregated"):
            return httpx.Response(200, json=load("tpg_aggregated.json"))
        if path.endswith("/categories/now/36"):
            return httpx.Response(200, json=load("tpg_now_36.json"))
        return httpx.Response(404)


HOME = Config(home=HomeSettings(isp_name="Belong", carrier="Telstra"), carriers=[Carrier(name="Telstra", asns=[1221])])


@pytest.fixture
def make_adapter():
    made = []

    def make(handler, config=HOME):
        db = Database(":memory:")
        made.append(db)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clock = lambda: NOW  # noqa: E731
        return TelcosAdapter(AdapterContext(config, db, StatusBoard(db, clock), http, clock), SETTINGS), db

    yield make
    for db in made:
        db.close()


async def test_poll_reads_both_telcos(make_adapter):
    fake = FakeTelcos()
    adapter, _ = make_adapter(fake)
    result = await adapter.poll()
    assert result.level == Level.NORMAL
    assert sorted(r.url.path for r in fake.requests) == [
        "/api/signup/system-outages", "/servicestatus/aggregated", "/servicestatus/categories/now/36"]
    assert [t["name"] for t in result.details["telcos"]] == ["TPG", "Aussie Broadband"]
    tags = {link["name"]: link["tag"] for link in result.details["links"]}
    assert tags["Belong"] == "your provider" and tags["Telstra"] == "your network" and tags["Optus"] is None


async def test_one_telco_failing_still_shows_the_other(make_adapter):
    adapter, _ = make_adapter(FakeTelcos(tpg_status=503))
    result = await adapter.poll()
    assert result.reason.startswith("TPG: couldn't check (HTTP 503); Aussie Broadband: 1 unplanned outage")
    tpg = result.details["telcos"][0]
    assert tpg["error"] == "HTTP 503" and tpg["unplanned"] == []


async def test_both_failing_fails_the_poll(make_adapter):
    adapter, _ = make_adapter(FakeTelcos(tpg_status=503, aussie_status=500))
    with pytest.raises(SourceError, match="Couldn't check TPG"):
        await adapter.poll()


async def test_widespread_outages_go_on_the_timeline_once(make_adapter):
    adapter, db = make_adapter(FakeTelcos(aussie=aussie_with(prio="major")))
    first = await adapter.poll()
    await adapter.poll()
    assert first.level == Level.ELEVATED
    events = db.events_since(NOW - 7 * 86400)
    assert [e["title"] for e in events] == ["Aussie Broadband reports an outage: Newman (WA)"]
    assert events[0]["level"] == "elevated"
