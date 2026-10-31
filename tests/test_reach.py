"""Regional reachability: timing hosts in each region, learning what's usual, and judging routes."""

from __future__ import annotations

import asyncio
import ssl

import certifi
import pytest
from pydantic import ValidationError

from sitrep.adapters.base import AdapterContext
from sitrep.adapters.reach import (
    Probe,
    ReachAdapter,
    ReachHost,
    ReachRegion,
    ReachSettings,
    Round,
    assess,
    judge_host,
    judge_region,
    probe,
    usual_ms,
)
from sitrep.config import Config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.state import StatusBoard

S = ReachSettings()
HOUR = 3600
WEEK = 7 * 86400
T0 = 1_800_000_000.0

# Times measured from a Belong NBN line in Macquarie Park on 30 October 2026 (TLS handshake, ms).
USUAL = {
    "Sydney": (4.5, 6.6), "Singapore": (178.7, 176.7), "Tokyo": (132.1, 270.4),
    "US West": (148.2, 139.4), "US East": (207.9, 205.9), "Europe": (299.0, 316.8),
}


def ok(region: str, provider: str, host: str, tls: float, tcp: float | None = None) -> Probe:
    return Probe(region, provider, host, "192.0.2.1", True, tcp_ms=tls if tcp is None else tcp, tls_ms=tls)


def lost(region: str, provider: str, host: str) -> Probe:
    return Probe(region, provider, host, "192.0.2.1", False, error="timed out", route_failure=True)


TARGET = ReachHost(name="AWS", host="dynamodb.us-west-1.amazonaws.com")


# --- One host ---


def test_a_host_at_its_usual_speed_is_normal():
    view = judge_host("US West", TARGET, [ok("US West", "AWS", TARGET.host, t) for t in (148, 149, 147, 150, 148)],
                      148.0, S)
    assert (view.state, view.now_ms, view.extra_ms) == ("normal", 148.0, 0.0)


def test_a_host_well_above_usual_is_slow():
    probes = [ok("US West", "AWS", TARGET.host, t) for t in (148, 260, 265, 262, 270)]
    view = judge_host("US West", TARGET, probes, 148.0, S)
    assert view.state == "slow" and view.extra_ms == 114.0


def test_small_or_noisy_rises_are_not_slow():
    assert judge_host("US West", TARGET, [ok("US West", "AWS", TARGET.host, 175)] * 5, 148.0, S).state == "normal"
    # One spike in five doesn't move the median.
    probes = [ok("US West", "AWS", TARGET.host, t) for t in (148, 900, 149, 150, 148)]
    assert judge_host("US West", TARGET, probes, 148.0, S).state == "normal"
    # Sydney's few milliseconds need a real jump, not a doubling.
    syd = ReachHost(name="AWS", host="dynamodb.ap-southeast-2.amazonaws.com")
    assert judge_host("Sydney", syd, [ok("Sydney", "AWS", syd.host, 14)] * 5, 5.0, S).state == "normal"


def test_failed_or_resent_connections_are_packet_loss():
    probes = [ok("US West", "AWS", TARGET.host, 148)] * 6 + [lost("US West", "AWS", TARGET.host)] * 2
    probes += [ok("US West", "AWS", TARGET.host, 1150, tcp=1148)] * 2  # first packet resent after a second
    view = judge_host("US West", TARGET, probes, None, S)
    assert view.state == "lossy" and view.loss == 0.4


def test_three_failures_in_a_row_is_down():
    probes = [ok("US West", "AWS", TARGET.host, 148)] * 4 + [lost("US West", "AWS", TARGET.host)] * 3
    assert judge_host("US West", TARGET, probes, 148.0, S).state == "down"


def test_problems_on_sitreps_side_dont_count_as_route_trouble():
    bad_cert = Probe("US West", "AWS", TARGET.host, "192.0.2.1", False, tcp_ms=148, error="certificate problem")
    view = judge_host("US West", TARGET, [ok("US West", "AWS", TARGET.host, 148)] * 4 + [bad_cert], 148.0, S)
    assert view.state == "unknown"
    no_name = Probe("US West", "AWS", TARGET.host, None, False, error="name lookup failed")
    assert judge_host("US West", TARGET, [no_name] * 3, None, S).state == "unknown"


# --- A region ---


def host(state: str, extra: float | None = None):
    view = judge_host("US West", TARGET, [], None, S)
    view.state = state
    if extra is not None:
        view.now_ms, view.usual_ms = 148 + extra, 148.0
    return view


def test_a_region_needs_both_providers_to_agree():
    region = ReachRegion(name="US West", hosts=[TARGET, ReachHost(name="Vultr", host="lax-ca-us-ping.vultr.com")])
    assert judge_region(region, [host("slow", 120), host("normal", 0)]).state == "normal"
    degraded = judge_region(region, [host("slow", 120), host("slow", 104)])
    assert degraded.state == "degraded" and degraded.describe() == "US West 104 ms slower than usual"
    assert judge_region(region, [host("down"), host("down")]).describe() == "US West unreachable"
    assert judge_region(region, [host("slow", 120), host("lossy")]).describe() == (
        "US West slower than usual and losing packets")
    assert judge_region(region, [host("slow", 120), host("unknown")]).state == "normal"  # one can't confirm
    assert judge_region(region, [host("unknown"), host("waiting")]).state == "unknown"


# --- Judging the whole picture ---


def regions(**states):
    """Region views: every region normal unless named (US_West="degraded", ...)."""
    out = []
    for name in USUAL:
        state = states.get(name.replace(" ", "_"), "normal")
        hosts = [host("slow" if state == "degraded" else "down" if state == "unreachable" else "normal",
                      110 if state == "degraded" else 0)] * 2
        region = ReachRegion(name=name, home=name == "Sydney", hosts=[TARGET, TARGET])
        out.append(judge_region(region, hosts))
    return out


def test_overseas_route_degraded_australia_fine_is_elevated():
    result = assess(regions(US_West="degraded", US_East="unreachable"), S, learning_left=0, at=T0)
    assert result.level == Level.ELEVATED
    assert result.reason == ("Overseas routes degraded, Sydney fine: US West 110 ms slower than usual; "
                             "US East unreachable")
    assert result.details["state"] == "overseas_degraded"
    (signal,) = result.signals
    assert signal.scopes == ("national:internet",) and signal.key == "reach:overseas"


def test_one_region_alone_is_shown_but_not_counted():
    result = assess(regions(Europe="degraded"), S, learning_left=0, at=T0)
    assert result.level == Level.NORMAL and result.details["state"] == "one_region"
    assert result.reason.startswith("Europe 110 ms slower than usual; one region alone")


def test_trouble_in_sydney_too_points_closer_to_home():
    result = assess(regions(Sydney="unreachable", US_West="unreachable", US_East="unreachable"), S,
                    learning_left=0, at=T0)
    assert result.level == Level.NORMAL and result.details["state"] == "local_trouble"


def test_learning_still_sees_regions_dropping_out():
    assert assess(regions(), S, learning_left=3 * 86400, at=T0).level == Level.LEARNING
    result = assess(regions(Tokyo="unreachable", Singapore="unreachable"), S, learning_left=3 * 86400, at=T0)
    assert result.level == Level.ELEVATED
    assert "Still learning each host's usual speed: 3 days to go" in result.reason


def test_all_normal():
    result = assess(regions(), S, learning_left=0, at=T0)
    assert result.reason == "Sydney, Singapore, Tokyo, US West, US East and Europe all answering at their usual speed"
    assert result.content_at == T0


# --- Learning what's usual ---


def test_usual_comes_from_the_same_hour_of_the_week():
    hour = int(T0 // HOUR * HOUR)
    values = {hour - WEEK: 150.0, hour - WEEK + HOUR: 152.0, hour - 2 * WEEK: 148.0, hour - 3 * HOUR: 400.0}
    assert usual_ms(values, T0, 4) == 150.0


def test_usual_falls_back_to_the_last_week_then_to_nothing():
    hour = int(T0 // HOUR * HOUR)
    day = {hour - h * HOUR: 140.0 + h % 3 for h in range(1, 25)}
    assert usual_ms(day, T0, 4) == 141.0
    assert usual_ms(dict(list(day.items())[:10]), T0, 4) is None


def test_exactly_one_home_region():
    with pytest.raises(ValidationError, match="exactly one region needs home: true"):
        ReachSettings(regions=[ReachRegion(name="A", hosts=[TARGET]), ReachRegion(name="B", hosts=[TARGET])])


# --- The adapter ---


def adapter(db, now) -> ReachAdapter:
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now[0]), None, lambda: now[0])
    a = ReachAdapter(ctx, ReachSettings())
    a.register()
    return a


def full_round(ts: float, extra: dict[str, float] | None = None) -> Round:
    probes = []
    for region in S.regions:
        for target, usual in zip(region.hosts, USUAL[region.name]):
            probes.append(ok(region.name, target.name, target.host, usual + (extra or {}).get(region.name, 0)))
    return Round(ts, tuple(probes))


def test_the_adapter_learns_then_spots_a_cable_fault():
    db = Database(":memory:")
    now = [T0 - 8 * 86400]
    a = adapter(db, now)
    # Eight days of ordinary rounds, one an hour (enough to fill every hour of the week).
    while now[0] < T0:
        rnd = full_round(now[0])
        a.history.append(rnd)
        a._record(rnd)
        now[0] += HOUR
    assert a.judge(now[0]).level == Level.NORMAL
    for _ in range(5):
        now[0] += 60
        a.history.append(full_round(now[0], {"US West": 118, "US East": 104}))
    result = a.judge(now[0])
    assert result.level == Level.ELEVATED
    assert "US West 118 ms slower than usual; US East 104 ms slower than usual" in result.reason
    db.close()


def test_a_new_install_is_learning():
    db = Database(":memory:")
    now = [T0]
    a = adapter(db, now)
    rnd = full_round(T0)
    a.history.append(rnd)
    a._record(rnd)
    result = a.judge(T0)
    assert result.level == Level.LEARNING and result.reason.startswith("Learning each host's usual speed: 7 days")
    db.close()


# --- Real connections, on this computer only ---


async def test_probe_tells_route_failures_from_its_own_problems():
    context = ssl.create_default_context(cafile=certifi.where())
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        closes = await probe("Here", ReachHost(name="Local", host="127.0.0.1", port=port), 2, context)
        assert not closes.ok and closes.tcp_ms is not None and closes.route_failure  # TCP fine, TLS cut off
    finally:
        server.close()
        await server.wait_closed()
    refused = await probe("Here", ReachHost(name="Local", host="127.0.0.1", port=port), 2, context)
    assert not refused.ok and refused.route_failure
    unknown = await probe("Here", ReachHost(name="Nowhere", host="sitrep-test.invalid"), 2, context)
    assert not unknown.ok and not unknown.route_failure and unknown.error.startswith("name lookup failed")
