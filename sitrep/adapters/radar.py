"""Cloudflare Radar: what Cloudflare sees of Australia's internet.

Cloudflare carries a large share of the world's web traffic, so it notices when a
country or a network goes quiet. Every 10 minutes sitrep asks Radar for:

- outages Cloudflare's staff have reported anywhere, keeping those that involve
  Australia or a watched carrier;
- confirmed traffic anomalies (Cloudflare's automatic detector, checked by a person);
- route hijacks involving Australia: another network announcing a carrier's
  addresses as its own. Route leaks are counted and shown, but not scored;
- traffic for Australia and each carrier over the last day, compared with the
  same time a week earlier. Radar's traffic numbers run 1 to 1.5 hours behind.

How it's judged (the numbers are in config.yaml):

- critical: Cloudflare reports a nationwide Australian outage, or Australian
  traffic stays below 50% of last week for 30 minutes;
- elevated: any other Cloudflare outage report for Australia or a carrier, a
  confirmed anomaly, a high-confidence hijack of a carrier's addresses, or traffic
  below 60% of last week for 30 minutes.

On 8 November 2023 Optus's traffic fell to 7-13% of the week before for six hours,
which would have made Radar elevated within about two hours of the start (Radar's
delay plus 30 minutes). Australia as a whole only fell to 90%.

Needs a free API token in .env (CLOUDFLARE_API_TOKEN); see the README.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import Field, SecretStr

from ..config import Carrier
from ..models import Assessment, Category, Level
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, retry_after_seconds

CONFIRMED = ("VERIFIED", "TP")  # TP: Cloudflare marked the anomaly a true positive
TOKEN_HELP = 'See "Cloudflare token" in the README'


class RadarSettings(PollingSettings):
    interval_seconds: float = 600
    api_token: SecretStr = SecretStr("")
    base_url: str = "https://api.cloudflare.com/client/v4"
    country: str = "AU"
    country_name: str = "Australia"
    carriers: list[Carrier] = Field(default_factory=list)  # from the top of config.yaml
    # Outage reports and anomalies from this far back are listed on the dashboard.
    events_days: int = Field(7, ge=1, le=30)
    traffic_elevated_below: float = Field(0.6, gt=0, le=1)
    country_critical_below: float = Field(0.5, gt=0, le=1)
    low_traffic_minutes: float = Field(30, gt=0)
    # Traffic numbers older than this are too stale to judge by.
    traffic_max_age_hours: float = 4
    hijack_min_confidence: int = 8
    hijack_window_hours: float = 6
    token_check_hours: float = 24
    token_warn_days: float = 14


# --- Reading Radar's answers ---


def parse_time(text: str | None) -> float | None:
    """Radar's timestamps, with or without a trailing Z, are UTC."""
    if not text:
        return None
    moment = datetime.fromisoformat(text.rstrip("Z"))
    return moment.replace(tzinfo=timezone.utc).timestamp()


def result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SourceError("Cloudflare sent something sitrep doesn't understand")
    if not payload.get("success", False):
        errors = payload.get("errors") or [{}]
        raise SourceError(f"Cloudflare said: {errors[0].get('message', 'unknown error')}")
    res = payload.get("result")
    if not isinstance(res, dict):
        raise SourceError("Cloudflare's answer had no result in it")
    return res


@dataclass(frozen=True)
class Report:
    """An outage Cloudflare reported, or an anomaly it confirmed."""

    kind: str  # "outage" or "anomaly"
    id: str
    who: str  # "Optus (AS4804)", "Australia", "New South Wales"
    description: str
    start: float
    end: float | None
    nationwide: bool = False
    url: str | None = None

    def ongoing(self, now: float) -> bool:
        return self.end is None or self.end > now


def _carrier_names(carriers: list[Carrier]) -> dict[int, str]:
    return {asn: c.name for c in carriers for asn in c.asns}


def _asn_label(asn: int, name: str | None, by_asn: dict[int, str]) -> str:
    return f"{by_asn.get(asn) or name or 'network'} (AS{asn})"


def outage_reports(payload: Any, country: str, country_name: str, carriers: list[Carrier]) -> list[Report]:
    by_asn = _carrier_names(carriers)
    reports = []
    for item in result(payload).get("annotations", []):
        try:
            locations = item.get("locations") or []
            details = {int(d["asn"]): d for d in item.get("asnsDetails") or []}
            asns = [int(a) for a in item.get("asns") or []]
            places = [
                e.get("entityName") for e in item.get("entities") or []
                if e.get("entityType") == "GEOLOCATION" and e.get("entityLocation") == country
            ]
            ours = [a for a in asns if a in by_asn or (details.get(a, {}).get("location") or {}).get("code") == country]
            if country not in locations and not ours and not places:
                continue
            if ours:
                who = ", ".join(_asn_label(a, details.get(a, {}).get("name"), by_asn) for a in ours)
            elif places:
                who = ", ".join(p for p in places if p)
            else:
                who = country_name
            cause = ((item.get("outage") or {}).get("outageCause") or "").replace("_", " ").lower()
            reports.append(Report(
                kind="outage",
                id=str(item["id"]),
                who=who,
                description=item.get("description") or cause or "Outage",
                start=parse_time(item["startDate"]),
                end=parse_time(item.get("endDate")),
                nationwide=country in locations and not asns and not item.get("geoIds"),
                url=item.get("linkedUrl"),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"Cloudflare sent an outage report sitrep can't read ({exc})") from exc
    return reports


def anomaly_reports(payload: Any, country: str, country_name: str, carriers: list[Carrier]) -> list[Report]:
    by_asn = _carrier_names(carriers)
    reports = []
    for item in result(payload).get("trafficAnomalies", []):
        try:
            if item.get("status") not in CONFIRMED:
                continue
            location = item.get("locationDetails") or {}
            asn = item.get("asnDetails") or {}
            if item.get("type") == "LOCATION" and location.get("code") == country:
                who = country_name
            elif item.get("type") == "AS" and asn and (
                int(asn["asn"]) in by_asn or (asn.get("location") or {}).get("code") == country
            ):
                who = _asn_label(int(asn["asn"]), asn.get("name"), by_asn)
            else:
                continue
            reports.append(Report(
                kind="anomaly",
                id=str(item["uuid"]),
                who=who,
                description="Traffic drop confirmed by Cloudflare",
                start=parse_time(item["startDate"]),
                end=parse_time(item.get("endDate")),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"Cloudflare sent an anomaly sitrep can't read ({exc})") from exc
    return reports


@dataclass(frozen=True)
class Hijack:
    victim: str  # "Telstra (AS1221)"
    hijacker_asn: int
    confidence: int
    prefixes: tuple[str, ...]
    last_seen: float


def parse_hijacks(payload: Any, carriers: list[Carrier], min_confidence: int, since: float) -> tuple[int, list[Hijack]]:
    """(how many hijack events involve the country, the serious ones against a carrier)."""
    by_asn = _carrier_names(carriers)
    events = result(payload).get("events", [])
    serious = []
    for event in events:
        try:
            last_seen = parse_time(event.get("max_hijack_ts") or event.get("max_msg_ts"))
            confidence = int(event.get("confidence_score") or 0)
            victims = [int(a) for a in event.get("victim_asns") or [] if int(a) in by_asn]
            if victims and confidence >= min_confidence and last_seen is not None and last_seen >= since:
                serious.append(Hijack(
                    victim=", ".join(_asn_label(a, None, by_asn) for a in victims),
                    hijacker_asn=int(event["hijacker_asn"]),
                    confidence=confidence,
                    prefixes=tuple(event.get("prefixes") or ()),
                    last_seen=last_seen,
                ))
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"Cloudflare sent a hijack event sitrep can't read ({exc})") from exc
    return len(events), serious


def parse_leaks(payload: Any, carriers: list[Carrier]) -> tuple[int, list[str]]:
    """(how many route leaks involve the country, the carriers on a leaked path)."""
    by_asn = _carrier_names(carriers)
    events = result(payload).get("events", [])
    involved = {by_asn[int(a)] for e in events for a in e.get("leak_seg") or [] if int(a) in by_asn}
    return len(events), sorted(involved)


@dataclass(frozen=True)
class Traffic:
    """Today against the same time last week. Values share one scale within a call."""

    times: tuple[float, ...]
    today: tuple[float | None, ...]
    lastweek: tuple[float | None, ...]

    def ratios(self) -> list[float | None]:
        return [t / w if t is not None and w else None for t, w in zip(self.today, self.lastweek)]

    def latest(self) -> tuple[float | None, float | None]:
        """(the newest ratio, when it was measured)."""
        for ratio, ts in zip(reversed(self.ratios()), reversed(self.times)):
            if ratio is not None:
                return ratio, ts
        return None, None

    def minutes_below(self, threshold: float) -> float:
        """How long the newest readings have stayed below threshold, in minutes."""
        known = [r for r in self.ratios() if r is not None]
        run = 0
        for ratio in reversed(known):
            if ratio >= threshold:
                break
            run += 1
        step = self.times[1] - self.times[0] if len(self.times) > 1 else 900
        return run * step / 60

    def chart(self) -> dict[str, Any]:
        def rounded(values: tuple[float | None, ...]) -> list[float | None]:
            return [None if v is None else round(v, 4) for v in values]

        step = self.times[1] - self.times[0] if len(self.times) > 1 else 900
        return {"from": self.times[0], "step": step, "today": rounded(self.today), "lastweek": rounded(self.lastweek)}


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_traffic(payload: Any) -> Traffic:
    res = result(payload)
    try:
        today, lastweek = res["today"], res["lastweek"]
        times = tuple(parse_time(t) for t in today["timestamps"])
        n = min(len(times), len(today["values"]), len(lastweek["values"]))
        return Traffic(
            times[:n],
            tuple(_float(v) for v in today["values"][:n]),
            tuple(_float(v) for v in lastweek["values"][:n]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(f"Cloudflare sent traffic numbers sitrep can't read ({exc})") from exc


# --- Judging ---


@dataclass
class TrafficStatus:
    name: str
    kind: str  # "country" or "carrier"
    ratio: float | None
    as_of: float | None
    minutes_low: float = 0
    level: str = "normal"
    home: bool = False


def _pct(ratio: float) -> str:
    return f"{round(ratio * 100)}%"


def judge_traffic(name: str, kind: str, traffic: Traffic | None, s: RadarSettings, now: float) -> TrafficStatus:
    if traffic is None:
        return TrafficStatus(name, kind, None, None)
    ratio, as_of = traffic.latest()
    status = TrafficStatus(name, kind, ratio, as_of)
    if ratio is None or as_of is None or now - as_of > s.traffic_max_age_hours * 3600:
        return status
    enough = s.low_traffic_minutes
    if kind == "country" and traffic.minutes_below(s.country_critical_below) >= enough:
        status.level, status.minutes_low = "critical", traffic.minutes_below(s.country_critical_below)
    elif traffic.minutes_below(s.traffic_elevated_below) >= enough:
        status.level, status.minutes_low = "elevated", traffic.minutes_below(s.traffic_elevated_below)
    return status


def _traffic_name(t: TrafficStatus) -> str:
    return f"Traffic in {t.name}" if t.kind == "country" else f"{t.name} traffic"


def _traffic_phrase(t: TrafficStatus) -> str:
    return f"{_traffic_name(t)} at {_pct(t.ratio or 0)} of last week for {format_duration(t.minutes_low * 60)}"


def assess(
    reports: list[Report],
    hijacks: list[Hijack],
    traffic: list[TrafficStatus],
    s: RadarSettings,
    now: float,
    extra: dict[str, Any] | None = None,
) -> Assessment:
    ongoing = [r for r in reports if r.ongoing(now)]
    critical: list[str] = []
    elevated: list[str] = []
    for report in sorted(ongoing, key=lambda r: r.start):
        if report.kind == "outage":
            where = f"a nationwide outage in {report.who}" if report.nationwide else f"an outage on {report.who}"
            phrase = f"Cloudflare reports {where}: {report.description.rstrip('.')}"
            (critical if report.nationwide else elevated).append(phrase)
        else:
            elevated.append(f"Cloudflare confirmed a traffic drop on {report.who} ({format_duration(now - report.start)})")
    for hijack in hijacks:
        elevated.append(
            f"Possible hijack of {hijack.victim}'s addresses by AS{hijack.hijacker_asn} "
            f"(confidence {hijack.confidence}), seen {format_duration(now - hijack.last_seen)} ago"
        )
    for t in sorted(traffic, key=lambda t: (t.kind != "country", not t.home)):
        if t.level == "critical":
            critical.append(_traffic_phrase(t))
        elif t.level == "elevated":
            elevated.append(_traffic_phrase(t))

    if critical:
        level, parts = Level.CRITICAL, critical + elevated
    elif elevated:
        level, parts = Level.ELEVATED, elevated
    else:
        level, parts = Level.NORMAL, [_normal_summary(traffic, s, now)]
    reason = "; ".join(parts)

    details: dict[str, Any] = {
        "traffic": [asdict(t) for t in traffic],
        "reports": [
            {**asdict(r), "ongoing": r.ongoing(now)} for r in sorted(reports, key=lambda r: r.start, reverse=True)
        ],
        "hijacks": [asdict(h) for h in hijacks],
        **(extra or {}),
    }
    return Assessment(level, reason, details)


def _normal_summary(traffic: list[TrafficStatus], s: RadarSettings, now: float) -> str:
    country = next((t for t in traffic if t.kind == "country"), None)
    carriers = [t.ratio for t in traffic if t.kind == "carrier" and t.ratio is not None]
    parts = []
    if country and country.ratio is not None and country.as_of is not None:
        if now - country.as_of > s.traffic_max_age_hours * 3600:
            parts.append(f"Traffic numbers are {format_duration(now - country.as_of)} old")
        else:
            parts.append(f"{_traffic_name(country)} at {_pct(country.ratio)} of last week")
    if carriers:
        low, high = _pct(min(carriers)), _pct(max(carriers))
        parts.append(f"carriers {low}" if low == high else f"carriers {low[:-1]}–{high}")
    parts.append("no outages reported")
    text = "; ".join(parts)
    return text[:1].upper() + text[1:]


# --- The adapter ---


class RadarAdapter(PollingAdapter):
    id = "radar"
    name = "Cloudflare Radar"
    category = Category.NETWORK
    link = "https://radar.cloudflare.com/au"
    Settings = RadarSettings
    uses_carriers = True
    settings: RadarSettings

    def __init__(self, ctx: AdapterContext, settings: RadarSettings) -> None:
        super().__init__(ctx, settings)
        self._token_checked_at: float | None = None
        self._token_expires: float | None = None

    async def _get(self, path: str, params: list[tuple[str, Any]] | dict[str, Any]) -> Any:
        token = self.settings.api_token.get_secret_value().strip()
        if not token:
            raise SourceError(f"No Cloudflare token yet: add CLOUDFLARE_API_TOKEN to .env. {TOKEN_HELP}")
        response = await self.ctx.http.get(
            self.settings.base_url.rstrip("/") + path,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code in (401, 403):
            raise SourceError(
                "Cloudflare refused the token: it may have expired, been deleted, or lack Radar read "
                f"access. Make a new one. {TOKEN_HELP}"
            )
        if response.status_code == 429:
            raise SourceError("Cloudflare asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 500:
            response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise SourceError("Cloudflare sent something that isn't JSON") from exc

    async def check_token(self, now: float) -> None:
        """Once a day, ask Cloudflare whether the token is still valid and when it expires."""
        if self._token_checked_at is not None and now - self._token_checked_at < self.settings.token_check_hours * 3600:
            return
        res = result(await self._get("/user/tokens/verify", {}))
        if res.get("status") != "active":
            raise SourceError(f"Cloudflare says the token is {res.get('status', 'not active')}. Make a new one. {TOKEN_HELP}")
        self._token_expires = parse_time(res.get("expires_on"))
        self._token_checked_at = now

    async def _traffic(self, where: tuple[str, str]) -> Traffic:
        params: list[tuple[str, Any]] = []
        for name, date_range in (("today", "1d"), ("lastweek", "1dcontrol")):
            params += [("name", name), where, ("dateRange", date_range)]
        params += [("aggInterval", "15m"), ("format", "json")]
        return parse_traffic(await self._get("/radar/netflows/timeseries", params))

    async def poll(self) -> Assessment:
        s = self.settings
        now = self.ctx.clock()
        await self.check_token(now)
        days = f"{s.events_days}d"
        targets: list[tuple[str, str, tuple[str, str]]] = [(s.country_name, "country", ("location", s.country))]
        targets += [(c.name, "carrier", ("asn", ",".join(map(str, c.asns)))) for c in s.carriers]
        answers = await asyncio.gather(
            self._get("/radar/annotations/outages", {"dateRange": days, "limit": 100, "format": "json"}),
            self._get("/radar/traffic_anomalies",
                      {"dateRange": days, "status": "VERIFIED", "limit": 100, "format": "json"}),
            self._get("/radar/bgp/hijacks/events", {"involvedCountry": s.country, "dateRange": "1d",
                                                   "sortBy": "TIME", "sortOrder": "DESC", "per_page": 100, "format": "json"}),
            self._get("/radar/bgp/leaks/events", {"involvedCountry": s.country, "dateRange": "1d",
                                                 "sortBy": "TIME", "sortOrder": "DESC", "per_page": 100, "format": "json"}),
            *(self._traffic(where) for _, _, where in targets),
        )
        outages, anomalies, hijack_answer, leak_answer, *traffic_answers = answers

        reports = outage_reports(outages, s.country, s.country_name, s.carriers)
        reports += anomaly_reports(anomalies, s.country, s.country_name, s.carriers)
        hijack_count, hijacks = parse_hijacks(
            hijack_answer, s.carriers, s.hijack_min_confidence, now - s.hijack_window_hours * 3600
        )
        leak_count, leak_carriers = parse_leaks(leak_answer, s.carriers)
        home = self.ctx.config.home.carrier.strip().casefold()
        traffic = []
        for (name, kind, _), answer in zip(targets, traffic_answers):
            status = judge_traffic(name, kind, answer, s, now)
            status.home = kind == "carrier" and bool(home) and name.casefold() == home
            traffic.append(status)

        country_traffic = traffic_answers[0]
        extra = {
            "chart": country_traffic.chart() if country_traffic.times else None,
            "hijack_count": hijack_count,
            "leak_count": leak_count,
            "leak_carriers": leak_carriers,
            "token_expires": self._token_expires,
        }
        assessment = assess(reports, hijacks, traffic, s, now, extra)
        if self._token_expires and self._token_expires - now < s.token_warn_days * 86400:
            when = datetime.fromtimestamp(self._token_expires).strftime("%d %b %Y")
            note = f"Your Cloudflare token expires on {when}, so make a new one soon. {TOKEN_HELP}"
            assessment = Assessment(assessment.level, f"{assessment.reason}. {note}", assessment.details)
        return assessment
