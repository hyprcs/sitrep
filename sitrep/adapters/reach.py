"""Regional reachability: can sitrep still reach the rest of the world, and how fast?

Every minute sitrep opens a connection to two hosts in each of six regions (Sydney,
Singapore, Tokyo, US West, US East and Europe) and times two things: the TCP connection
and the TLS handshake. Nothing else is sent: no web page is fetched. The two hosts in a
region belong to different providers, so one provider's trouble doesn't look like a
broken route.

The hosts really sit in their regions. sitrep avoids anycast and CDN addresses (1.1.1.1,
Cloudflare, Google's front ends), which answer from a Sydney data centre whatever the
route overseas is doing. Each host's address is looked up every round, because the AWS
ones change.

The TLS handshake time is the one judged: the TCP connection time on a home line often
includes a few milliseconds of first-packet delay, which would make the Sydney numbers
noisy.

How it's judged:

- sitrep learns each host's usual handshake time for each hour of the week (from the
  last 4 weeks; the whole last week while that's thin). Until it has 7 days of history
  it shows "learning" and can only see total loss and packet loss, which need no history.
- A host is slow when its handshakes over the last 5 minutes are well above usual: at
  least 30 ms and at least 25% more. It has packet loss when 3 or more of its last 10
  tries failed or took over a second (a sign of a lost and resent first packet). It's
  down when its last 3 tries all failed.
- A region is degraded when all its hosts are slow or losing packets, and unreachable
  when they're all down.
- Two or more overseas regions degraded or unreachable while Sydney is fine is the
  distinct state "overseas route degraded, Australia fine": elevated. A Pacific cable
  fault shows up first this way, as US latency jumping while local hosts stay normal.
  One region on its own is shown but not counted. If Sydney is in trouble too, the
  problem is closer to home, and "Your connection" is the source that judges that.
- Never critical on its own: it's one home line's view of the world.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

import certifi
from pydantic import Field, model_validator

from ..config import Settings
from ..models import Assessment, Category, Level, Signal, national
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings
from .probes import short_error

# A first packet that's lost is resent after a second, so a connection this slow means loss.
RESENT_MS = 1000


class ReachHost(Settings):
    name: str  # the provider, as the dashboard shows it
    host: str
    port: int = 443


class ReachRegion(Settings):
    name: str
    # True for the region you live in. The others are "overseas".
    home: bool = False
    hosts: list[ReachHost] = Field(min_length=1)


DEFAULT_REGIONS = [
    {"name": "Sydney", "home": True, "hosts": [
        {"name": "AWS", "host": "dynamodb.ap-southeast-2.amazonaws.com"},
        {"name": "Vultr", "host": "syd-au-ping.vultr.com"}]},
    {"name": "Singapore", "hosts": [
        {"name": "AWS", "host": "dynamodb.ap-southeast-1.amazonaws.com"},
        {"name": "Hetzner", "host": "sin-speed.hetzner.com"}]},
    {"name": "Tokyo", "hosts": [
        {"name": "AWS", "host": "dynamodb.ap-northeast-1.amazonaws.com"},
        {"name": "Oracle", "host": "objectstorage.ap-tokyo-1.oraclecloud.com"}]},
    {"name": "US West", "hosts": [
        {"name": "AWS", "host": "dynamodb.us-west-1.amazonaws.com"},
        {"name": "Vultr", "host": "lax-ca-us-ping.vultr.com"}]},
    {"name": "US East", "hosts": [
        {"name": "AWS", "host": "dynamodb.us-east-1.amazonaws.com"},
        {"name": "Hetzner", "host": "ash-speed.hetzner.com"}]},
    {"name": "Europe", "hosts": [
        {"name": "AWS", "host": "dynamodb.eu-west-2.amazonaws.com"},
        {"name": "Hetzner", "host": "fsn1-speed.hetzner.com"}]},
]


class ReachSettings(PollingSettings):
    interval_seconds: float = 60
    timeout_seconds: float = 5
    regions: list[ReachRegion] = Field(default_factory=lambda: [ReachRegion(**r) for r in DEFAULT_REGIONS])
    # A host is slow when its recent handshakes are at least this many ms above usual...
    degraded_extra_ms: float = Field(30, ge=0)
    # ...and at least this share above usual.
    degraded_extra_share: float = Field(0.25, ge=0)
    # Latency is judged on the median of this many recent rounds.
    recent_rounds: int = Field(5, ge=1)
    # A host has packet loss when this share of its last loss_rounds tries failed or were resent.
    loss_rounds: int = Field(10, ge=2)
    loss_share: float = Field(0.3, gt=0, le=1)
    # A host is down when its last this-many tries all failed.
    down_after_rounds: int = Field(3, ge=1)
    # This many overseas regions in trouble while the home region is fine is elevated.
    elevated_when_regions: int = Field(2, ge=1)
    # The usual time for an hour of the week comes from this many previous weeks.
    baseline_weeks: int = Field(4, ge=1)

    @model_validator(mode="after")
    def _one_home(self) -> ReachSettings:
        homes = [r.name for r in self.regions if r.home]
        if len(homes) != 1:
            raise ValueError(f"exactly one region needs home: true (found {len(homes)})")
        return self


# --- Measuring ---


@dataclass(frozen=True)
class Probe:
    """One host, one round."""

    region: str
    provider: str
    host: str
    ip: str | None
    ok: bool
    tcp_ms: float | None = None
    tls_ms: float | None = None
    error: str | None = None
    # True when a failure says something about the route (a timeout or a refused
    # connection), False when it doesn't (the name didn't resolve, a certificate problem).
    route_failure: bool = False

    @property
    def lossy(self) -> bool:
        return self.route_failure or (self.tcp_ms is not None and self.tcp_ms >= RESENT_MS)


@dataclass(frozen=True)
class Round:
    ts: float
    probes: tuple[Probe, ...]


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 1)
    except Exception:
        pass


async def probe(region: str, target: ReachHost, timeout: float, context: ssl.SSLContext) -> Probe:
    """Look the host up, then time a TCP connection and a TLS handshake to it. Nothing else is sent."""
    loop = asyncio.get_running_loop()
    base = {"region": region, "provider": target.name, "host": target.host}
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(target.host, target.port, family=socket.AF_INET, type=socket.SOCK_STREAM), timeout
        )
        ip = infos[0][4][0]
    except Exception as exc:
        return Probe(**base, ip=None, ok=False, error=f"name lookup failed ({short_error(exc)})")

    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, target.port), timeout)
    except Exception as exc:
        return Probe(**base, ip=ip, ok=False, error=short_error(exc), route_failure=True)
    tcp_ms = _ms(start)

    start = time.perf_counter()
    try:
        await asyncio.wait_for(writer.start_tls(context, server_hostname=target.host), timeout)
    except ssl.SSLCertVerificationError as exc:
        await _close(writer)
        return Probe(**base, ip=ip, ok=False, tcp_ms=tcp_ms, error=f"certificate problem ({exc.verify_message})")
    except Exception as exc:
        await _close(writer)
        return Probe(**base, ip=ip, ok=False, tcp_ms=tcp_ms, error=f"TLS {short_error(exc)}", route_failure=True)
    tls_ms = _ms(start)
    await _close(writer)
    return Probe(**base, ip=ip, ok=True, tcp_ms=tcp_ms, tls_ms=tls_ms)


# --- Judging ---


def usual_ms(values: dict[int, float], at: float, weeks: int) -> float | None:
    """A host's usual handshake time at this hour of the week (and the hour either side),
    from the previous `weeks` weeks. Falls back to the whole last week while that's thin."""
    hour = int(at // 3600 * 3600)
    week = 7 * 86400
    same = [values[h] for w in range(1, weeks + 1) for offset in (-3600, 0, 3600)
            if (h := hour - w * week + offset) in values]
    if len(same) >= 2:
        return statistics.median(same)
    last_week = [v for h, v in values.items() if hour - week <= h < hour]
    if len(last_week) >= 24:
        return statistics.median(last_week)
    return None


@dataclass
class HostView:
    region: str
    provider: str
    host: str
    ip: str | None
    state: str  # normal, slow, lossy, down, unknown (couldn't check), waiting (not enough rounds yet)
    now_ms: float | None = None  # median handshake over the recent rounds
    usual_ms: float | None = None
    tcp_ms: float | None = None  # the latest TCP connection time
    loss: float | None = None  # share of recent tries lost
    error: str | None = None

    @property
    def extra_ms(self) -> float | None:
        if self.now_ms is None or self.usual_ms is None:
            return None
        return self.now_ms - self.usual_ms

    @property
    def bad(self) -> bool:
        return self.state in ("slow", "lossy", "down")


@dataclass
class RegionView:
    name: str
    home: bool
    state: str  # normal, degraded, unreachable, unknown
    hosts: list[HostView] = field(default_factory=list)

    @property
    def extra_ms(self) -> float | None:
        """How much slower than usual the region is: the smaller of its hosts' figures, so both agree."""
        extras = [h.extra_ms for h in self.hosts if h.extra_ms is not None]
        return min(extras) if extras and all(h.state == "slow" for h in self.hosts) else None

    def describe(self) -> str:
        if self.state == "unreachable":
            return f"{self.name} unreachable"
        if self.state == "degraded":
            extra = self.extra_ms
            if extra is not None:
                return f"{self.name} {round(extra)} ms slower than usual"
            if any(h.state == "slow" for h in self.hosts):
                return f"{self.name} slower than usual and losing packets"
            return f"{self.name} losing packets"
        return f"{self.name} normal"


def judge_host(region: str, target: ReachHost, probes: Sequence[Probe], usual: float | None,
               s: ReachSettings) -> HostView:
    """probes are this host's results, oldest first."""
    if not probes:
        return HostView(region, target.name, target.host, None, "waiting")
    latest = probes[-1]
    view = HostView(region, target.name, target.host, latest.ip, "normal", usual_ms=usual,
                    tcp_ms=latest.tcp_ms, error=latest.error)
    recent = [p.tls_ms for p in probes[-s.recent_rounds:] if p.ok and p.tls_ms is not None]
    if len(recent) >= min(3, s.recent_rounds):
        view.now_ms = round(statistics.median(recent), 1)
    tries = list(probes[-s.loss_rounds:])
    view.loss = sum(p.lossy for p in tries) / len(tries)

    last = probes[-s.down_after_rounds:]
    if len(last) >= s.down_after_rounds and all(p.route_failure for p in last):
        view.state = "down"
    elif len(tries) >= min(5, s.loss_rounds) and view.loss >= s.loss_share:
        view.state = "lossy"
    elif view.extra_ms is not None and usual is not None and (
        view.extra_ms >= max(s.degraded_extra_ms, s.degraded_extra_share * usual)
    ):
        view.state = "slow"
    elif not latest.ok and not latest.route_failure:
        view.state = "unknown"  # sitrep couldn't check it (name lookup, certificate)
    elif view.now_ms is None and not any(p.ok for p in tries):
        view.state = "waiting"
    return view


def judge_region(region: ReachRegion, hosts: list[HostView]) -> RegionView:
    known = [h for h in hosts if h.state not in ("unknown", "waiting")]
    view = RegionView(region.name, region.home, "normal", hosts)
    if not known:
        view.state = "unknown"
    elif len(known) >= min(2, len(hosts)) and all(h.state == "down" for h in known):
        view.state = "unreachable"
    elif len(known) >= min(2, len(hosts)) and all(h.bad for h in known):
        view.state = "degraded"
    return view


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def assess(regions: list[RegionView], s: ReachSettings, *, learning_left: float, at: float) -> Assessment:
    home = next(r for r in regions if r.home)
    overseas = [r for r in regions if not r.home]
    troubled = [r for r in overseas if r.state in ("degraded", "unreachable")]
    signals: tuple[Signal, ...] = ()

    if home.state in ("degraded", "unreachable"):
        state, level = "local_trouble", Level.NORMAL
        reason = (f"{home.describe()}, so the trouble is close to home rather than overseas; "
                  "\"Your connection\" judges that")
    elif len(troubled) >= s.elevated_when_regions:
        state, level = "overseas_degraded", Level.ELEVATED
        reason = f"Overseas routes degraded, {home.name} fine: " + "; ".join(r.describe() for r in troubled)
        signals = (Signal("reach:overseas", Level.ELEVATED, (national("internet"),), title=reason, at=at),)
    elif troubled:
        state, level = "one_region", Level.NORMAL
        reason = f"{troubled[0].describe()}; one region alone isn't enough to count"
    elif learning_left > 0:
        state, level = "learning", Level.LEARNING
        reason = (f"Learning each host's usual speed: {format_duration(learning_left)} to go. Until then it "
                  "can only see regions becoming unreachable or losing packets")
    else:
        state, level = "normal", Level.NORMAL
        reached = [r.name for r in regions if r.state == "normal"]
        reason = f"{_join(reached)} all answering at their usual speed" if reached else "No results yet"
    unknown = [r.name for r in regions if r.state == "unknown"]
    if unknown and state != "learning":
        reason += f". Couldn't check {_join(unknown)}"
    if learning_left > 0 and state != "learning":
        reason += f". Still learning each host's usual speed: {format_duration(learning_left)} to go"

    details = {
        "state": state,
        "learning_left": max(0.0, learning_left),
        "regions": [
            {"name": r.name, "home": r.home, "state": r.state, "extra_ms": r.extra_ms,
             "hosts": [{**asdict(h), "extra_ms": h.extra_ms} for h in r.hosts]}
            for r in regions
        ],
    }
    return Assessment(level, reason, details, content_at=at, signals=signals)


# --- The adapter ---


class ReachAdapter(PollingAdapter):
    id = "reach"
    name = "Regional reachability"
    category = Category.CONNECTIVITY
    Settings = ReachSettings
    scope = national("internet")
    settings: ReachSettings

    def __init__(self, ctx: AdapterContext, settings: ReachSettings) -> None:
        super().__init__(ctx, settings)
        self.history: deque[Round] = deque(maxlen=max(settings.loss_rounds, settings.recent_rounds,
                                                      settings.down_after_rounds))
        self._ssl = ssl.create_default_context(cafile=certifi.where())
        self._hour: int | None = None
        self._samples: dict[str, list[float]] = {}  # host -> this hour's handshake times

    async def poll(self) -> Assessment:
        rnd = await self.run_round()
        self.history.append(rnd)
        self._record(rnd)
        return self.judge(rnd.ts)

    async def run_round(self) -> Round:
        s = self.settings
        started = self.ctx.clock()
        probes = await asyncio.gather(*(
            probe(region.name, target, s.timeout_seconds, self._ssl)
            for region in s.regions for target in region.hosts
        ))
        return Round(started, tuple(probes))

    def _record(self, rnd: Round) -> None:
        """Keep each host's median handshake time for the hour, to learn what's usual."""
        hour = int(rnd.ts // 3600 * 3600)
        if hour != self._hour:
            self._hour, self._samples = hour, {}
        db = self.ctx.db
        for p in rnd.probes:
            if p.ok and p.tls_ms is not None:
                samples = self._samples.setdefault(p.host, [])
                samples.append(p.tls_ms)
                db.set_gauge(self.id, f"tls:{p.host}", rnd.ts, statistics.median(samples))
        if db.kv_get(f"{self.id}:first_seen") is None:
            db.kv_set(f"{self.id}:first_seen", str(rnd.ts))

    def judge(self, now: float) -> Assessment:
        s = self.settings
        since = now - (s.baseline_weeks * 7 + 1) * 86400
        first_seen = float(self.ctx.db.kv_get(f"{self.id}:first_seen") or now)
        learning_left = self.ctx.config.scoring.learning_days * 86400 - (now - first_seen)
        regions = []
        for region in s.regions:
            hosts = []
            for target in region.hosts:
                mine = [p for r in self.history for p in r.probes if p.host == target.host and p.region == region.name]
                usual = None
                if learning_left <= 0:
                    hour_now = int(now // 3600 * 3600)
                    values = {t: v for t, v in self.ctx.db.gauges_since(self.id, f"tls:{target.host}", since).items()
                              if t < hour_now}
                    usual = usual_ms(values, now, s.baseline_weeks)
                hosts.append(judge_host(region.name, target, mine, usual, s))
            regions.append(judge_region(region, hosts))
        return assess(regions, s, learning_left=learning_left, at=now)
