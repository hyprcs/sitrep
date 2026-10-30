"""Connectivity probes: the checks themselves, and how a round is judged."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import dns.exception
import dns.message
import dns.rcode
import dns.rrset
import httpx
import pytest

from sitrep.adapters import probes
from sitrep.adapters.probes import (
    Check,
    HttpsTarget,
    ProbeSettings,
    Resolver,
    Round,
    TcpTarget,
    assess,
    check_dns,
    check_https,
    check_tcp,
)
from sitrep.models import INTERNET_DOWN, ROUTER_DOWN, Level

FIXTURES = Path(__file__).parent / "fixtures" / "probes"

SETTINGS = ProbeSettings.model_validate(
    {
        "interval_seconds": 30,
        "offline_after_rounds": 3,
        "target_down_after_rounds": 2,
        "router": {"name": "Home router", "host": "192.168.1.1", "port": 80},
    }
)


def live_round(name: str = "round_live_ok.json") -> Round:
    """A real round captured from a home connection (see tests/fixtures/probes)."""
    return Round.from_dict(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def broken(rnd: Round, ts: float, fails) -> Round:
    """A copy of rnd at time ts where every check matching fails() timed out."""
    checks = tuple(
        Check(c.kind, c.name, False, None, "timed out", c.isp) if fails(c) else c for c in rnd.checks
    )
    return Round(ts, checks)


def rounds(*fail_rules) -> list[Round]:
    """One round per rule, 30 seconds apart, starting from the live fixture."""
    base = live_round()
    return [broken(base, base.ts + 30 * i, rule) for i, rule in enumerate(fail_rules)]


NOTHING = lambda c: False  # noqa: E731
OUTSIDE = lambda c: c.counts_as_outside  # noqa: E731
EVERYTHING = lambda c: True  # noqa: E731


# --- Judging rounds ---


def test_live_round_is_normal():
    rnd = live_round()
    result = assess([rnd], SETTINGS, "Belong")
    assert result.level == Level.NORMAL
    assert result.reason.startswith(f"All {len(rnd.outside)} outside checks OK")
    assert not result.flags
    assert len(result.details["checks"]) == len(rnd.checks)


def test_real_timeout_fixture_counts_as_a_failing_check():
    partial = live_round("round_live_partial.json")
    failed = [c for c in partial.checks if not c.ok]
    assert [c.error for c in failed] == ["timed out"]
    later = Round(partial.ts + 30, partial.checks)
    result = assess([partial, later], SETTINGS)
    assert result.level == Level.ELEVATED
    assert "Unroutable test address" in result.reason


def test_one_off_blip_is_ignored():
    history = rounds(NOTHING, lambda c: c.name == "Google")
    assert assess(history, SETTINGS).level == Level.NORMAL


def test_same_check_failing_twice_is_elevated():
    google = lambda c: c.name == "Google"  # noqa: E731
    history = rounds(NOTHING, google, google)
    result = assess(history, SETTINGS)
    assert result.level == Level.ELEVATED
    assert f"1 of {len(history[-1].outside)} outside checks failing: Google" in result.reason
    assert not result.flags


def test_isp_dns_failure_is_named():
    isp_dns = lambda c: c.kind == "dns" and c.isp  # noqa: E731
    result = assess(rounds(isp_dns, isp_dns), SETTINGS, "Belong")
    assert result.level == Level.ELEVATED
    assert result.reason == "Belong's DNS (through your router) isn't answering, but public DNS works"


def test_isp_dns_does_not_count_as_internet_up():
    # A router can answer DNS from its cache while the internet is down.
    outside_only = rounds(OUTSIDE, OUTSIDE, OUTSIDE)
    assert all(rnd.dark for rnd in outside_only)
    assert assess(outside_only, SETTINGS).level == Level.CRITICAL


def test_dark_rounds_below_threshold_are_elevated():
    result = assess(rounds(NOTHING, OUTSIDE, OUTSIDE), SETTINGS)
    assert result.level == Level.ELEVATED
    assert "2 of 3 rounds" in result.reason
    assert not result.flags


def test_offline_with_router_up_is_critical_internet_down():
    result = assess(rounds(OUTSIDE, OUTSIDE, OUTSIDE), SETTINGS, "Belong")
    assert result.level == Level.CRITICAL
    assert result.flags == frozenset({INTERNET_DOWN})
    assert "your router answers" in result.reason
    assert "Belong" in result.reason
    assert "90 seconds" in result.reason


def test_offline_with_router_down_points_at_home_network():
    result = assess(rounds(EVERYTHING, EVERYTHING, EVERYTHING), SETTINGS)
    assert result.level == Level.CRITICAL
    assert result.flags == frozenset({INTERNET_DOWN, ROUTER_DOWN})
    assert "your router isn't answering either" in result.reason


def test_recovers_as_soon_as_outside_answers():
    result = assess(rounds(OUTSIDE, OUTSIDE, OUTSIDE, NOTHING), SETTINGS)
    assert result.level == Level.NORMAL


def test_router_failing_with_internet_fine_is_normal_with_a_hint():
    router = lambda c: c.kind == "router"  # noqa: E731
    result = assess(rounds(router, router), SETTINGS)
    assert result.level == Level.NORMAL
    assert "router check is failing" in result.reason


def test_round_survives_json_round_trip():
    rnd = live_round()
    assert Round.from_dict(json.loads(json.dumps(rnd.to_dict()))) == rnd


# --- The checks themselves ---


async def test_check_tcp_open_port():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        result = await check_tcp("tcp", TcpTarget(name="local", host="127.0.0.1", port=port), 2)
    assert result.ok and result.ms is not None


async def test_check_tcp_closed_port_fails():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # nothing listens here once the socket closes
    result = await check_tcp("tcp", TcpTarget(name="closed", host="127.0.0.1", port=port), 3)
    assert not result.ok
    assert result.error


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_check_https_any_status_means_reachable():
    async with _client(lambda request: httpx.Response(403)) as client:
        result = await check_https(client, HttpsTarget(name="site", url="https://example.test/"))
    assert result.ok
    assert result.note == "HTTP 403"


async def test_check_https_uses_head_by_default():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200)

    async with _client(handler) as client:
        await check_https(client, HttpsTarget(name="site", url="https://example.test/"))
    assert seen == ["HEAD"]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "DNS lookup failed"),
        (httpx.ConnectError("connection reset"), "couldn't connect"),
        (httpx.ConnectTimeout("too slow"), "timed out"),
    ],
)
async def test_check_https_failures_are_plain_english(exc, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    async with _client(handler) as client:
        result = await check_https(client, HttpsTarget(name="site", url="https://example.test/"))
    assert not result.ok
    assert result.error == expected


def _fake_dns(monkeypatch, make_response):
    async def fake_udp(query, where, timeout):
        return make_response(query)

    monkeypatch.setattr(probes.dns.asyncquery, "udp", fake_udp)


async def test_check_dns_answer_is_ok(monkeypatch):
    def answer(query):
        response = dns.message.make_response(query)
        response.answer.append(dns.rrset.from_text("www.example.com.", 60, "IN", "A", "192.0.2.10"))
        return response

    _fake_dns(monkeypatch, answer)
    resolver = Resolver(name="Router", server="192.168.1.1", isp=True)
    result = await check_dns(resolver, "www.example.com", 2)
    assert result.ok and result.isp


async def test_check_dns_nxdomain_fails(monkeypatch):
    def nxdomain(query):
        response = dns.message.make_response(query)
        response.set_rcode(dns.rcode.NXDOMAIN)
        return response

    _fake_dns(monkeypatch, nxdomain)
    result = await check_dns(Resolver(name="DNS", server="1.1.1.1"), "www.example.com", 2)
    assert not result.ok
    assert result.error == "answered NXDOMAIN"


async def test_check_dns_timeout_fails(monkeypatch):
    async def slow(query, where, timeout):
        raise dns.exception.Timeout()

    monkeypatch.setattr(probes.dns.asyncquery, "udp", slow)
    result = await check_dns(Resolver(name="DNS", server="1.1.1.1"), "www.example.com", 2)
    assert result.error == "timed out"
