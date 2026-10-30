"""AEMO: reading the summary, notices and history; judging real grid events; the adapter end to end.

The fixtures (tests/fixtures/aemo) are AEMO's own data, used under AEMO's copyright
permissions notice:

- live_summary.json, live_5min_nsw.json, listing.html, notices/: AEMO and NEMWeb as of
  29 October 2026, about 3 pm market time (a quiet day, apart from a NSW SCADA outage);
- feb2024_*.json: 13 February 2024, when transmission towers fell in Victoria and AEMO
  directed load shedding (the notices that day, and Victorian and NSW demand);
- jun2022_notices.json: NSW reserve, price-cap and suspension notices from 13 to 24
  June 2022, when AEMO suspended the whole market.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sitrep.adapters.aemo import (
    AemoAdapter,
    AemoSettings,
    Notice,
    RegionNow,
    active_conditions,
    assess,
    classify,
    find_drop,
    make_notice,
    market_time,
    notice_regions,
    notice_summary,
    parse_history,
    parse_listing,
    parse_notice_file,
    parse_summary,
    parse_summary_notice,
    worth_showing,
)
from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.config import Config, load_config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.runtime import build_adapter_specs
from sitrep.state import StatusBoard

FIXTURES = Path(__file__).parent / "fixtures" / "aemo"
ROOT = Path(__file__).parent.parent
SETTINGS = AemoSettings()
LISTING_URL = SETTINGS.notices_url


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def live():
    return parse_summary(load("live_summary.json"))


def notices_from(name: str) -> list[Notice]:
    return [parse_summary_notice(n) for n in load(name)]


def at(text: str) -> float:
    return market_time(text)


def region_now(region: str, when: float, **flags) -> RegionNow:
    return RegionNow(region, when - 60, 7000.0, 90.0, flags.get("suspended", False), flags.get("apc", False))


def replay(notices, when: str, home: str = "NSW1", history=None, **flags):
    """Judge a moment from history, as if sitrep had been running then."""
    now = at(when)
    regions = {r: region_now(r, now, **(flags if r == home else {})) for r in ("NSW1", "QLD1", "VIC1", "SA1", "TAS1")}
    return assess(regions, notices, history or {}, AemoSettings(region=home), now)


# --- Reading AEMO's data ---


def test_live_summary_parses():
    regions, notices = live()
    assert sorted(regions) == ["NSW1", "QLD1", "SA1", "TAS1", "VIC1"]
    nsw = regions["NSW1"]
    assert (nsw.demand, nsw.price, nsw.suspended, nsw.apc) == (5740.58, 66.61, False, False)
    assert nsw.at == 1793250000  # 2026-10-29 15:00 market time (05:00 UTC)
    assert [n.id for n in notices] == list(range(145869, 145859, -1))
    scada = next(n for n in notices if n.id == 145867)
    assert (scada.type, scada.title, scada.regions) == ("NEM SYSTEMS", "SERVICE INTERRUPTION - NSW SCADA", ("NSW1",))
    assert "\r" not in scada.body and scada.body.startswith("AEMO ELECTRICITY MARKET NOTICE")


def test_nemweb_files_say_the_same_as_the_summary():
    _, notices = live()
    by_id = {n.id: n for n in notices}
    for path in sorted((FIXTURES / "notices").glob("*R14586*")):
        from_file = parse_notice_file(path.read_text(encoding="utf-8"))
        same = by_id[from_file.id]
        assert (from_file.type, from_file.title, from_file.time, from_file.regions) == \
            (same.type, same.title, same.time, same.regions)
        assert from_file.body.split() == same.body.split()


def test_listing_gives_each_notice_file():
    files = parse_listing((FIXTURES / "listing.html").read_text(encoding="utf-8"), LISTING_URL)
    assert [f.id for f in files] == list(range(145858, 145870))
    assert files[0].day == "20261028" and files[-1].day == "20261029"
    assert files[-1].url == "https://nemweb.com.au/Reports/CURRENT/Market_Notice/NEMITWEB1_MKTNOTICE_20261029.R145869"


def test_broken_notice_file_is_a_source_error():
    with pytest.raises(SourceError, match="missing"):
        parse_notice_file("Notice ID : 5\nnothing else")


def test_history_is_one_region_every_five_minutes():
    history = parse_history(load("live_5min_nsw.json"), "NSW1")
    times = sorted(history)
    assert len(times) == 576 and all(b - a == 300 for a, b in zip(times, times[1:]))
    assert history[times[-1]] == (5740.58, 66.61)
    assert parse_history(load("live_5min_nsw.json"), "VIC1") == {}


def test_regions_come_from_the_title_or_else_the_text():
    assert notice_regions("Load Shedding Direction in the Vic Region", "") == ("VIC1",)
    assert notice_regions("[EventId:147] AP STARTED for ENERGY in NSW1 at 13 June 2022 18:25", "") == ("NSW1",)
    feb = {n.id: n for n in notices_from("feb2024_notices.json")}
    assert feb[114577].regions == ("VIC1",)  # "Significant power system event - 13 Feb 2024": the text says Vic
    jun = {n.id: n for n in notices_from("jun2022_notices.json")}
    assert set(jun[97704].regions) == {"NSW1", "QLD1", "VIC1", "SA1", "TAS1"}  # the whole market
    assert notice_regions("A note", "sa is not a region, nor is vicinity or Tasty") == ()


@pytest.mark.parametrize("type_, title, expected", [
    ("RESERVE NOTICE", "Actual Lack Of Reserve Level 2 (LOR2) in the NSW Region on 13/06/2022", ("lor2", "start")),
    ("RESERVE NOTICE", "Update - Actual Lack Of Reserve Level 2 (LOR2) in the NSW Region on 18/06/2022", ("lor2", "start")),
    ("RESERVE NOTICE", "Cancellation of Actual (LOR2) condition in the NSW region - 17/06/2022", ("lor2", "end")),
    ("RESERVE NOTICE", "Actual Lack Of Reserve Level 3 (LOR3) in the VIC region - 13/02/2024", ("lor3", "start")),
    ("RESERVE NOTICE", "PDPASA - Forecast Lack Of Reserve Level 3 (LOR3) in the NSW Region on 17/06/2022", ("forecast_lor3", "start")),
    ("RESERVE NOTICE", "PDPASA - Cancellation of the Forecast Lack Of Reserve Level 3 (LOR3) in the NSW Region on 17/06/2022", ("forecast_lor3", "end")),
    ("RESERVE NOTICE", "PDPASA - Forecast Lack Of Reserve Level 2 (LOR2) in the VIC Region on 13/02/2024", None),
    ("RESERVE NOTICE", "RESERVE NOTICE MT PASA PUBLICATION 13 February 2024", None),
    ("LOAD SHED", "Load Shedding Direction in the Vic Region", ("load_shed", "start")),
    ("LOAD RESTORE", "Load Restoration Direction in the Vic Region", ("load_shed", "end")),
    ("MARKET SUSPENSION", "Declaration of electricity market suspension", ("suspension", "start")),
    ("MARKET SUSPENSION", "Update - Market suspension - FCAS prices", None),
    ("MARKET SUSPENSION", "Declaration of electricity market resumption", ("suspension", "end")),
    ("ADMINISTERED PRICE CAP", "[EventId:150] AP STARTED for ENERGY in NSW1 at 14 June 2022 03:55", ("apc", "start")),
    ("ADMINISTERED PRICE CAP", "AP ENDED for ENERGY in NSW1 at 15 June 2022 14:05", ("apc", "end")),
    ("POWER SYSTEM EVENTS", "Significant power system event - Victoria region - 13/02/2024", ("significant_event", "start")),
    ("POWER SYSTEM EVENTS", "Non-credible contingency event - NSW region - 05/06/2022", None),
    ("NEM SYSTEMS", "SERVICE INTERRUPTION - NSW SCADA", None),
])
def test_real_titles_are_classified(type_, title, expected):
    assert classify(make_notice(1, 0, type_, title, "")) == expected


# --- Judging real events ---


def test_quiet_live_day_is_normal():
    regions, notices = live()
    history = parse_history(load("live_5min_nsw.json"), "NSW1")
    result = assess(regions, notices, history, SETTINGS, regions["NSW1"].at + 60)
    assert result.level == Level.NORMAL
    assert result.reason == "NSW grid normal (demand 5,741 MW, price $67/MWh); no supply warnings from AEMO"
    shown = [(n["id"], n["level"]) for n in result.details["notices"]]
    assert shown == [(145869, "info"), (145867, "info")]  # the SCADA outage, then AEMO's all-clear for it
    assert result.details["notices_total"] == 10
    chart = result.details["chart"]
    assert len(chart["today"]) == 288 and None not in chart["today"] and None not in chart["yesterday"]
    assert result.details["drop"] is None


def test_feb_2024_from_nsw_load_shedding_in_victoria_is_elevated():
    feb = notices_from("feb2024_notices.json")
    before = replay(feb, "2024-02-13T13:00:00")
    assert before.level == Level.NORMAL
    during = replay(feb, "2024-02-13T14:40:00")
    assert during.level == Level.ELEVATED
    assert during.reason == ("AEMO directed load shedding in Victoria 5 minutes ago; AEMO declared an actual LOR3 "
                             "in Victoria 12 minutes ago: there isn't enough supply, so some customers are being cut off")
    assert replay(feb, "2024-02-13T15:30:00").level == Level.NORMAL  # LOR3 cancelled, load restored


def test_feb_2024_in_victoria_itself():
    feb = notices_from("feb2024_notices.json")
    history = parse_history(load("feb2024_5min.json"), "VIC1")
    event = replay(feb, "2024-02-13T13:30:00", home="VIC1", history=history)
    assert event.level == Level.ELEVATED
    assert event.reason == ("AEMO reported a significant power system event in Victoria 8 minutes ago: "
                            "Multiple tripping of generation and transmission lines in the Vic region")
    shedding = replay(feb, "2024-02-13T14:40:00", home="VIC1", history=history)
    assert shedding.level == Level.CRITICAL
    after = replay(feb, "2024-02-13T15:30:00", home="VIC1", history=history)
    assert after.level == Level.ELEVATED  # still a significant event, and demand fell by more than a quarter
    assert after.reason.endswith("Victoria demand fell 27% in half an hour (7,533 MW to 5,533 MW, 30 minutes ago), "
                                 "which usually means a large area has lost power")
    assert replay(feb, "2024-02-13T20:00:00", home="VIC1", history=history).level == Level.NORMAL


def test_jun_2022_reserves_prices_and_suspension():
    jun = notices_from("jun2022_notices.json")
    lor2 = replay(jun, "2022-06-13T18:00:00")
    assert lor2.level == Level.ELEVATED
    assert "AEMO declared an actual LOR2 in NSW 9 minutes ago: the grid is one big failure away from load shedding" in lor2.reason
    assert "Prices in NSW are capped" not in lor2.reason  # administered pricing started at 18:25
    capped = replay(jun, "2022-06-13T19:00:00")
    assert "Prices in NSW are capped after a long run of extreme ones" in capped.reason
    suspended = replay(jun, "2022-06-15T14:10:00")
    assert suspended.level == Level.CRITICAL
    assert ("AEMO suspended the electricity market in NSW, Queensland, Victoria, South Australia and Tasmania "
            "6 minutes ago") in suspended.reason
    still = replay(jun, "2022-06-23T12:00:00")
    assert still.level == Level.CRITICAL  # nine days later, still suspended
    resumed = replay(jun, "2022-06-24T11:00:00")
    assert "suspended" not in resumed.reason


def test_a_forecast_lor3_is_shown_but_not_counted():
    forecast = make_notice(10, at("2022-06-17T05:39:04"), "RESERVE NOTICE",
                           "PDPASA - Forecast Lack Of Reserve Level 3 (LOR3) in the NSW Region beginning on 17/06/2022", "")
    result = replay([forecast], "2022-06-17T06:00:00")
    assert result.level == Level.NORMAL
    assert result.reason.endswith("; AEMO forecasts a possible LOR3 (a supply shortfall) in NSW; nothing has happened yet")
    assert [(c["label"], c["level"]) for c in result.details["conditions"]] == [("Forecast LOR3", "watch")]


def test_market_flags_count_without_a_notice():
    suspended = replay([], "2026-10-29T15:00:00", suspended=True)
    assert suspended.level == Level.CRITICAL and suspended.reason == "The electricity market is suspended in NSW"
    capped = replay([], "2026-10-29T15:00:00", apc=True)
    assert capped.level == Level.ELEVATED


def test_conditions_without_an_ending_fade_after_twelve_hours():
    lor3 = make_notice(1, at("2026-10-29T08:00:00"), "RESERVE NOTICE", "Actual Lack Of Reserve Level 3 (LOR3) in the NSW region", "")
    assert replay([lor3], "2026-10-29T19:00:00").level == Level.CRITICAL
    assert replay([lor3], "2026-10-29T20:30:00").level == Level.NORMAL


def test_notices_dated_in_the_future_are_ignored():
    lor3 = make_notice(1, at("2026-10-29T16:00:00"), "RESERVE NOTICE", "Actual Lack Of Reserve Level 3 (LOR3) in the NSW region", "")
    assert active_conditions([lor3], at("2026-10-29T15:00:00")) == []


def test_follow_ups_to_a_shown_notice_are_shown():
    _, notices = live()
    shown = [n.title for n, _ in worth_showing(notices, "NSW1")]
    assert shown == ["SERVICE INTERRUPTION - NSW SCADA", "SERVICE RESUMPTION"]
    assert [n.title for n, _ in worth_showing(notices, "QLD1")] == []  # not your region, not severe


def test_notice_summaries_drop_aemo_boilerplate():
    _, notices = live()
    scada = next(n for n in notices if n.id == 145867)
    assert notice_summary(scada).startswith(
        "AEMO is experiencing a service interruption relating to NSW SCADA. AEMO is working on restoring the service.")
    feb = {n.id: n for n in notices_from("feb2024_notices.json")}
    assert notice_summary(feb[114632]) == ("The Actual LOR3 Condition in the VIC region advised in AEMO Electricity "
                                           "Market Notice No.114604 is cancelled at 1515 hrs 13/02/2024.")
    # A sentence AEMO wrapped onto a second line stays one sentence.
    assert notice_summary(feb[114607]).startswith(
        "AEMO has directed load shedding commencing at 1420 hrs 13/02/2024 in the Victoria region to restore")
    assert len(notice_summary(feb[114577])) <= 221 and notice_summary(feb[114577]).endswith("…")


def test_dashboard_notices_carry_a_badge_and_summary():
    regions, notices = live()
    result = assess(regions, notices, {}, SETTINGS, regions["NSW1"].at + 60)
    badges = [(n["label"], n["title"]) for n in result.details["notices"]]
    assert badges == [("Follow-up", "SERVICE RESUMPTION"), ("AEMO systems", "SERVICE INTERRUPTION - NSW SCADA")]
    assert all(n["summary"] for n in result.details["notices"])


def test_stale_figures_fail_the_poll():
    regions, notices = live()
    with pytest.raises(SourceError, match="stopped updating 25 minutes ago"):
        assess(regions, notices, {}, SETTINGS, regions["NSW1"].at + 25 * 60)


# --- Demand drops ---


def test_tower_collapse_drop_is_found_and_ordinary_days_are_not():
    vic = parse_history(load("feb2024_5min.json"), "VIC1")
    drop = find_drop(vic, SETTINGS, at("2024-02-13T15:10:00"))
    assert drop is not None and drop.share > 0.25
    nsw = parse_history(load("feb2024_5min.json"), "NSW1")
    days = parse_history(load("live_5min_nsw.json"), "NSW1")
    for history in (nsw, days):
        assert all(find_drop(history, SETTINGS, t) is None for t in history)
    assert all(find_drop(vic, SETTINGS, t) is None for t in vic if t < at("2024-02-13T14:30:00"))


def test_one_odd_reading_is_not_a_drop():
    start = at("2026-10-29T12:00:00")
    history = {start + i * 300: (7000.0, None) for i in range(12)}
    history[start + 10 * 300] = (3000.0, None)
    assert find_drop(history, SETTINGS, start + 11 * 300) is None
    history[start + 11 * 300] = (3000.0, None)  # two in a row is
    assert find_drop(history, SETTINGS, start + 11 * 300).share == pytest.approx(4 / 7)


def test_small_demand_swings_are_ignored():
    start = at("2026-10-29T12:00:00")
    history = {start + i * 300: (600.0 if i < 8 else 100.0, None) for i in range(10)}  # South Australia at noon
    assert find_drop(history, SETTINGS, start + 9 * 300) is None


def test_shipped_config_is_valid():
    cfg = load_config(ROOT / "config.yaml", env_file=None)
    aemo = next(s for cls, s in build_adapter_specs(cfg) if cls.id == "aemo")
    assert (aemo.region, aemo.demand_drop, aemo.interval_seconds) == ("NSW1", 0.25, 300)


# --- The adapter against fake AEMO and NEMWeb servers ---


class FakeAemo:
    def __init__(self, summary=None, history_status=200, listing_status=200) -> None:
        self.requests: list[httpx.Request] = []
        self.summary = summary or load("live_summary.json")
        self.history_status = history_status
        self.listing_status = listing_status

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/ELEC_NEM_SUMMARY"):
            return httpx.Response(200, json=self.summary)
        if path.endswith("/5MIN") and request.method == "POST":
            if self.history_status != 200:
                return httpx.Response(self.history_status)
            return httpx.Response(200, json=load("live_5min_nsw.json"))
        if path.endswith("/Market_Notice/"):
            if self.listing_status != 200:
                return httpx.Response(self.listing_status)
            return httpx.Response(200, text=(FIXTURES / "listing.html").read_text(encoding="utf-8"))
        file = FIXTURES / "notices" / path.rsplit("/", 1)[-1]
        return httpx.Response(200, text=file.read_text(encoding="utf-8")) if file.exists() else httpx.Response(404)


NOW = market_time("2026-10-29T15:01:00")


@pytest.fixture
def make_adapter():
    made = []

    def make(handler, clock=lambda: NOW, settings=SETTINGS):
        db = Database(":memory:")
        made.append(db)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return AemoAdapter(AdapterContext(Config(), db, StatusBoard(db, clock), http, clock), settings), db

    yield make
    for db in made:
        db.close()


async def test_first_poll_fills_in_notices_and_history(make_adapter):
    fake = FakeAemo()
    adapter, _ = make_adapter(fake)
    result = await adapter.poll()
    assert result.level == Level.NORMAL
    assert fake.paths()[:2] == ["GET /aemo/apps/api/report/ELEC_NEM_SUMMARY", "GET /Reports/Current/Market_Notice/"]
    fetched = sorted(p.rsplit(".", 1)[-1] for p in fake.paths() if "MKTNOTICE" in p)
    assert fetched == ["R145858", "R145859"]  # only the ones the summary didn't carry
    assert fake.paths()[-1] == "POST /aemo/apps/api/report/5MIN"
    assert result.details["notices_total"] == 12
    assert result.details["chart"]["today"][-1] == 5741

    fake.requests.clear()
    await adapter.poll()
    assert fake.paths() == ["GET /aemo/apps/api/report/ELEC_NEM_SUMMARY"]  # from now on, one small request


async def test_a_burst_of_new_notices_fills_in_again(make_adapter):
    fake = FakeAemo()
    now = [NOW]
    adapter, _ = make_adapter(fake, lambda: now[0])
    await adapter.poll()
    summary = load("live_summary.json")
    for i, n in enumerate(summary["ELEC_NEM_SUMMARY_MARKET_NOTICE"]):
        n["NOTICEID"] = 145900 + i  # ten notices sitrep has never seen
    fake.summary = summary
    fake.requests.clear()
    now[0] += 300
    await adapter.poll()
    assert "GET /Reports/Current/Market_Notice/" not in fake.paths()  # not twice in 10 minutes
    now[0] += 600
    await adapter.poll()
    assert "GET /Reports/Current/Market_Notice/" in fake.paths()


async def test_nemweb_and_history_failures_dont_fail_the_poll(make_adapter):
    fake = FakeAemo(history_status=503, listing_status=503)
    adapter, _ = make_adapter(fake)
    result = await adapter.poll()
    assert result.level == Level.NORMAL and result.details["notices_total"] == 10
    assert result.details["chart"] is None  # one reading isn't a chart yet


async def test_shown_notices_go_on_the_timeline_once(make_adapter):
    adapter, db = make_adapter(FakeAemo())
    await adapter.poll()
    await adapter.poll()
    titles = [e["title"] for e in db.events_since(NOW - 86400)]
    assert sorted(titles) == ["AEMO: SERVICE INTERRUPTION - NSW SCADA", "AEMO: SERVICE RESUMPTION"]


async def test_slow_down_carries_retry_after(make_adapter):
    adapter, _ = make_adapter(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(SourceError, match="slow down") as caught:
        await adapter.poll()
    assert caught.value.retry_after == 120


async def test_summary_without_your_region_fails(make_adapter):
    summary = load("live_summary.json")
    summary["ELEC_NEM_SUMMARY"] = [r for r in summary["ELEC_NEM_SUMMARY"] if r["REGIONID"] != "NSW1"]
    adapter, _ = make_adapter(FakeAemo(summary=summary))
    with pytest.raises(SourceError, match="no figures for NSW"):
        await adapter.poll()
