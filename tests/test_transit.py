"""Sydney Trains and Metro alerts: telling unplanned suspensions apart from planned trackwork."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.transit import TransitAdapter, TransitSettings, assess, line_code, parse
from sitrep.config import Config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.state import StatusBoard

FIXTURES = Path(__file__).parent / "fixtures" / "transit"  # real feeds, © Transport for NSW, CC BY 4.0
S = TransitSettings(api_key=SecretStr("test-token"))


def load(feed: str) -> dict:
    return json.loads((FIXTURES / f"{feed}_20261031.json").read_text(encoding="utf-8"))


NOW = float(load("sydneytrains")["header"]["timestamp"])


def alert(alert_id: str, header: str, *, cause="UNKNOWN_CAUSE", effect="UNKNOWN_EFFECT", lines=(), stops=False,
          operator=False, start=NOW - 600, end=None, description="") -> dict:
    informed = [{"agencyId": "SydneyTrains"}] if operator else []
    for code in lines:
        entity = {"agencyId": "SydneyTrains", "routeId": f"{code}_1a", "directionId": 0}
        if stops:
            entity["stopId"] = "200060"
        informed.append(entity)
    period = {"start": str(int(start))}
    if end:
        period["end"] = str(int(end))
    return {"id": alert_id, "alert": {
        "activePeriod": [period], "informedEntity": informed, "cause": cause, "effect": effect,
        "headerText": {"translation": [{"text": header, "language": "en"}]},
        "descriptionText": {"translation": [{"text": description or header, "language": "en"}]},
    }}


def with_alerts(*extra: dict, feed="sydneytrains") -> dict:
    data = load(feed)
    data["entity"] = data["entity"] + list(extra)
    return data


def judged(*extra: dict):
    alerts, made = parse(with_alerts(*extra), "sydneytrains")
    return assess(alerts, S, NOW, made=made)


# --- Reading ---


def test_the_real_feed_is_all_planned_or_station_updates():
    alerts, made = parse(load("sydneytrains"), "sydneytrains")
    assert len(alerts) == 18 and made == NOW
    assert not any(a.disruption for a in alerts)
    result = assess(alerts, S, NOW, made=made)
    assert result.level == Level.NORMAL
    assert result.reason.startswith("No unplanned disruptions on Sydney Trains or Metro. 14 planned trackwork alerts")
    assert result.content_at == NOW


def test_line_codes():
    assert line_code("IWL_2b") == "IWL" and line_code("4T.T.BMT") == "BMT" and line_code("SMNW_M1") == "SMNW"
    assert line_code("2404") is None and line_code("NT32") is None  # bus routes at a station


def test_station_updates_dont_count_bus_routes_as_lines():
    alerts, _ = parse(load("sydneytrains"), "sydneytrains")
    central = next(a for a in alerts if a.header == "Station Update - Central")
    assert central.planned and not central.disruption
    assert set(central.lines) <= set(["NSN", "WST", "IWL", "BNK", "T3", "ESI", "CMB", "T6", "OLY", "APS", "NTH",
                                      "BMT", "CCN", "HUN", "SCO", "SHL", "SMNW"])


def test_something_else_is_an_error():
    with pytest.raises(SourceError, match="GTFS-realtime"):
        parse([], "sydneytrains")


# --- Judging ---


def test_a_network_wide_suspension_is_elevated():
    result = judged(alert("x1", "Trains are not running due to a power supply issue", effect="NO_SERVICE", operator=True))
    assert result.level == Level.ELEVATED
    assert result.reason.startswith("Unplanned suspension across the network: Trains are not running")
    (signal,) = result.signals
    assert (signal.key, signal.scopes) == ("transit:x1", ("sydney",))


def test_a_suspension_on_three_lines_is_elevated_on_two_it_isnt():
    three = judged(alert("x2", "Trains are not running between Central and Strathfield due to a signal failure",
                         lines=("WST", "IWL", "NTH")))
    assert three.level == Level.ELEVATED
    assert "T1 Western, T2 Inner West & Leppington and T9 Northern" in three.reason
    two = judged(alert("x3", "Trains are not running between Central and Strathfield", lines=("WST", "IWL")))
    assert two.level == Level.NORMAL
    assert two.reason.startswith("1 unplanned disruption, none across several lines")


def test_planned_weekend_trackwork_across_many_lines_doesnt_count():
    result = judged(alert("x4", "Buses replace trains between Hornsby and Strathfield", cause="MAINTENANCE",
                          effect="MODIFIED_SERVICE", lines=("NTH", "WST", "CCN", "IWL")))
    assert result.level == Level.NORMAL and result.details["disruptions"] == []


def test_delays_are_shown_but_dont_count():
    result = judged(alert("x5", "Allow extra travel time", effect="SIGNIFICANT_DELAYS", lines=("ESI",)))
    assert result.level == Level.NORMAL
    assert result.details["disruptions"][0]["line_names"] == ["T4 Eastern Suburbs & Illawarra"]


def test_the_whole_metro_line_suspended_is_elevated_one_station_isnt():
    whole = assess(parse(with_alerts(alert("m1", "Metro services are suspended", effect="NO_SERVICE",
                                           lines=("SMNW",)), feed="metro"), "metro")[0], S, NOW)
    assert whole.level == Level.ELEVATED
    one = assess(parse(with_alerts(alert("m2", "Metro services are suspended at Central", effect="NO_SERVICE",
                                         lines=("SMNW",), stops=True), feed="metro"), "metro")[0], S, NOW)
    assert one.level == Level.NORMAL


def test_alerts_not_in_force_are_ignored():
    later = judged(alert("x6", "Trains are not running", operator=True, start=NOW + 3600))
    over = judged(alert("x7", "Trains are not running", operator=True, start=NOW - 7200, end=NOW - 3600))
    assert later.level == over.level == Level.NORMAL


# --- The adapter ---


async def test_poll_reads_both_feeds_once_and_new_alerts_can_alert():
    feeds = {"sydneytrains": load("sydneytrains"), "metro": load("metro")}
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        feed = request.url.path.rsplit("/", 1)[-1]
        asked.append((feed, request.url.params.get("format"), request.headers.get("Authorization")))
        return httpx.Response(200, json=feeds[feed])

    db = Database(":memory:")
    now = [NOW]
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now[0]), http, lambda: now[0])
    adapter = TransitAdapter(ctx, TransitSettings(api_key=SecretStr("test-token")))
    adapter.register()
    adapter.report(await adapter.poll())
    assert sorted(asked) == [("metro", "json", "apikey test-token"), ("sydneytrains", "json", "apikey test-token")]
    # Central's station update is in both feeds; it's counted once.
    assert adapter.ctx.board.views()[0].details["alerts_total"] == 18

    now[0] += 120
    feeds["sydneytrains"] = with_alerts(alert("x8", "Trains are not running on any line", operator=True,
                                              start=now[0] - 60))
    result = await adapter.poll()
    (signal,) = result.signals
    assert signal.alertable  # new since a good look two minutes ago
    assert [e["title"] for e in db.events_since(0)] == ["Trains: the network: Trains are not running on any line"]
    await http.aclose()
    db.close()
