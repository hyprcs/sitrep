"""Connectivity probes: is it my line, or the wider internet?

Every round (30 seconds by default) runs these checks at the same time:

- the home router: a plain TCP connection to its web page port;
- well-known addresses (1.1.1.1, 8.8.8.8, 9.9.9.9): TCP connections to port 443;
- DNS lookups through two resolvers: the router, which passes them on to your
  ISP, and a public one;
- HTTPS requests to sites on different networks (only the response headers
  are read, never the page).

TCP and HTTPS are used rather than ping, so Docker needs no extra privileges.

How a round is judged (see assess()):

- everything answers -> normal;
- the same outside check fails two rounds running, or your ISP's DNS stops
  answering -> elevated, naming what's failing;
- nothing outside answers for three rounds running (about 90 seconds) ->
  critical, flagged "internet down". If the router still answers, the problem
  is beyond your house; if it doesn't, it's probably inside it.
"""

from __future__ import annotations

import asyncio
import ssl
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Sequence

import certifi
import dns.asyncbackend
import dns.asyncquery
import dns.exception
import dns.message
import dns.rcode
import dns.rdatatype
import httpx
from pydantic import Field

from ..config import Settings
from ..models import INTERNET_DOWN, OFFLINE_HEADLINE, ROUTER_DOWN, Assessment, Category, Level
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings


class TcpTarget(Settings):
    name: str
    host: str
    port: int


class Resolver(Settings):
    name: str
    server: str
    # True for the resolver that goes through your ISP (usually the router).
    # It's used to spot ISP DNS failures, not to decide whether the internet is up,
    # because a router can answer from its cache while the internet is down.
    isp: bool = False


class DnsSettings(Settings):
    lookup_names: list[str] = Field(default_factory=lambda: ["www.google.com"], min_length=1)
    resolvers: list[Resolver] = Field(default_factory=list)


class HttpsTarget(Settings):
    name: str
    url: str
    # HEAD asks for headers only, which is lighter for the site than GET.
    method: str = "HEAD"


class ProbeSettings(PollingSettings):
    interval_seconds: float = 30
    timeout_seconds: float = 5
    offline_after_rounds: int = Field(3, ge=1)
    target_down_after_rounds: int = Field(2, ge=1)
    router: TcpTarget
    tcp: list[TcpTarget] = Field(default_factory=list)
    dns: DnsSettings = Field(default_factory=DnsSettings)
    https: list[HttpsTarget] = Field(default_factory=list)


@dataclass(frozen=True)
class Check:
    kind: str  # router | tcp | dns | https
    name: str
    ok: bool
    ms: float | None = None
    error: str | None = None
    isp: bool = False
    note: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.name)

    @property
    def counts_as_outside(self) -> bool:
        """Does this check prove anything about the internet beyond the house?"""
        return self.kind in ("tcp", "https") or (self.kind == "dns" and not self.isp)


@dataclass(frozen=True)
class Round:
    ts: float
    checks: tuple[Check, ...]

    @property
    def router(self) -> Check | None:
        return next((c for c in self.checks if c.kind == "router"), None)

    @property
    def outside(self) -> list[Check]:
        return [c for c in self.checks if c.counts_as_outside]

    @property
    def dark(self) -> bool:
        """True when nothing outside the house answered."""
        return bool(self.outside) and not any(c.ok for c in self.outside)

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "checks": [asdict(c) for c in self.checks]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Round:
        return cls(ts=data["ts"], checks=tuple(Check(**c) for c in data["checks"]))


def short_error(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException, dns.exception.Timeout)):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    text = str(exc) or type(exc).__name__
    if any(s in text for s in ("getaddrinfo", "Name or service not known", "name resolution")):
        return "DNS lookup failed"
    if isinstance(exc, httpx.ConnectError):
        return "couldn't connect"
    return text if len(text) <= 100 else text[:97] + "..."


def _ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


async def check_tcp(kind: str, target: TcpTarget, timeout: float) -> Check:
    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(target.host, target.port), timeout)
    except Exception as exc:
        return Check(kind, target.name, False, error=short_error(exc))
    ms = _ms_since(start)
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 1)
    except Exception:
        pass
    return Check(kind, target.name, True, ms)


async def check_dns(resolver: Resolver, qname: str, timeout: float) -> Check:
    query = dns.message.make_query(qname, dns.rdatatype.A)
    start = time.perf_counter()
    try:
        response = await dns.asyncquery.udp(query, resolver.server, timeout=timeout)
    except Exception as exc:
        return Check("dns", resolver.name, False, error=short_error(exc), isp=resolver.isp)
    ms = _ms_since(start)
    if response.rcode() != dns.rcode.NOERROR:
        error = f"answered {dns.rcode.to_text(response.rcode())}"
        return Check("dns", resolver.name, False, ms, error, isp=resolver.isp)
    if not response.answer:
        return Check("dns", resolver.name, False, ms, "empty answer", isp=resolver.isp)
    return Check("dns", resolver.name, True, ms, isp=resolver.isp, note=qname)


async def check_https(client: httpx.AsyncClient, target: HttpsTarget) -> Check:
    start = time.perf_counter()
    try:
        async with client.stream(target.method, target.url) as response:
            status = response.status_code  # any HTTP answer proves the path works
    except Exception as exc:
        return Check("https", target.name, False, error=short_error(exc))
    return Check("https", target.name, True, _ms_since(start), note=f"HTTP {status}")


def _capitalise(text: str) -> str:
    return text[:1].upper() + text[1:]


def assess(history: Sequence[Round], s: ProbeSettings, isp_name: str = "your ISP") -> Assessment:
    """Judge the latest round, using earlier rounds to ignore one-off blips."""
    current = history[-1]
    outside = current.outside
    router_ok = current.router is not None and current.router.ok

    dark_rounds = 0
    for rnd in reversed(history):
        if not rnd.dark:
            break
        dark_rounds += 1
    details: dict[str, Any] = {
        "checks": [asdict(c) for c in current.checks],
        "round_ts": current.ts,
        "router_ok": router_ok,
        "dark_rounds": dark_rounds,
    }

    if current.dark:
        dark_for = current.ts - history[-dark_rounds].ts + s.interval_seconds
        if dark_rounds < s.offline_after_rounds:
            reason = (
                f"Nothing outside answered ({dark_rounds} of {s.offline_after_rounds} rounds "
                "before sitrep calls the internet unreachable)"
            )
            return Assessment(Level.ELEVATED, reason, details)
        if router_ok:
            reason = (
                f"{OFFLINE_HEADLINE}: your router answers, but nothing outside has for "
                f"{format_duration(dark_for)}. It could be your NBN line, {isp_name}, or something bigger"
            )
            return Assessment(Level.CRITICAL, reason, details, frozenset({INTERNET_DOWN}))
        reason = (
            f"{OFFLINE_HEADLINE}: your router isn't answering either, for {format_duration(dark_for)}. "
            "Check the router's power and cables, or this computer's network connection"
        )
        return Assessment(Level.CRITICAL, reason, details, frozenset({INTERNET_DOWN, ROUTER_DOWN}))

    # A check only counts as down if it failed in each of the last few rounds.
    recent = list(history)[-s.target_down_after_rounds:]
    down: list[Check] = []
    if len(recent) == s.target_down_after_rounds:
        failing = [{c.key for c in rnd.checks if not c.ok and c.kind != "router"} for rnd in recent]
        down_keys = set.intersection(*failing)
        down = [c for c in current.checks if c.key in down_keys]

    problems = []
    if any(c.kind == "dns" and c.isp for c in down):
        public_dns_ok = any(c.kind == "dns" and not c.isp and c.ok for c in current.checks)
        problems.append(
            f"{isp_name}'s DNS (through your router) isn't answering"
            + (", but public DNS works" if public_dns_ok else "")
        )
    other = [c for c in down if c.counts_as_outside]
    if other:
        names = ", ".join(c.name for c in other)
        problems.append(f"{len(other)} of {len(outside)} outside checks failing: {names}")
    if problems:
        return Assessment(Level.ELEVATED, _capitalise("; ".join(problems)), details)

    reason = f"All {len(outside)} outside checks OK"
    web_ms = [c.ms for c in current.checks if c.kind == "https" and c.ok and c.ms is not None]
    if web_ms:
        reason += f"; websites answer in about {round(statistics.median(web_ms))} ms"
    if not router_ok:
        reason += ". The router check is failing, so check its address in your .env"
    return Assessment(Level.NORMAL, reason, details)


class ProbesAdapter(PollingAdapter):
    id = "probes"
    name = "Your connection"
    category = Category.CONNECTIVITY
    Settings = ProbeSettings
    settings: ProbeSettings

    def __init__(self, ctx: AdapterContext, settings: ProbeSettings) -> None:
        super().__init__(ctx, settings)
        keep = max(10, settings.offline_after_rounds, settings.target_down_after_rounds)
        self.history: deque[Round] = deque(maxlen=keep)
        self._round_no = 0
        self._ssl = ssl.create_default_context(cafile=certifi.where())
        # Load the DNS library's async machinery now, so the first round's timings are honest.
        try:
            dns.asyncbackend.get_default_backend()
        except Exception:
            pass  # not inside a running event loop (e.g. in tests); it'll load on first use

    async def poll(self) -> Assessment:
        rnd = await self.run_round()
        self.history.append(rnd)
        self._record(rnd)
        return assess(self.history, self.settings, self.ctx.config.home.isp_name)

    async def run_round(self) -> Round:
        s = self.settings
        started = self.ctx.clock()
        qname = s.dns.lookup_names[self._round_no % len(s.dns.lookup_names)]
        self._round_no += 1
        # The quick TCP checks run first, on their own, so their timings aren't
        # blurred by the heavier DNS and HTTPS work. Worst case (everything timing
        # out) a round takes two timeouts, well inside the interval.
        tcp_checks = await asyncio.gather(
            check_tcp("router", s.router, s.timeout_seconds),
            *(check_tcp("tcp", t, s.timeout_seconds) for t in s.tcp),
        )
        # A fresh client with no keep-alive, so every round makes new connections.
        async with httpx.AsyncClient(
            timeout=s.timeout_seconds,
            verify=self._ssl,
            follow_redirects=False,
            headers={"User-Agent": self.ctx.config.http.user_agent},
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client:
            other_checks = await asyncio.gather(
                *(check_dns(r, qname, s.timeout_seconds) for r in s.dns.resolvers),
                *(check_https(client, t) for t in s.https),
            )
        return Round(ts=started, checks=(*tcp_checks, *other_checks))

    def _record(self, rnd: Round) -> None:
        """Count rounds for the 24-hour connectivity strip."""
        db = self.ctx.db
        db.add_count(self.id, "rounds", rnd.ts)
        if rnd.dark:
            db.add_count(self.id, "dark", rnd.ts)
        elif any(not c.ok for c in rnd.outside):
            db.add_count(self.id, "partial", rnd.ts)
