"""NSW fuel prices, from FuelCheck (the NSW Government's API on API.NSW).

Every 2 hours sitrep downloads every NSW station's current prices (one call; about 2 MB)
and works out the median price of unleaded (U91) and diesel (DL):

- near home: stations within 10 km of home;
- across NSW: every station.

Each median is saved once an hour, so the dashboard knows the last 30 days. A fuel is
elevated when its median jumps more than 10 c/L above the highest it's been in the last
30 days (not counting the last day). Sydney's prices go up and down in a cycle every
few weeks, so a jump only counts when it goes past the cycle's usual peak. Fuel is
never critical by itself, and it's only about fuel, so it doesn't back up internet or
power trouble. It needs a week of history before it can judge (it's "learning" until
then; prices still show).

FuelCheck has no field for a station running out of fuel. A fuel that a station stops
listing might mean it's out, or might not, so for now the tile only shows how many
stations near home list each fuel.

API.NSW needs its own free key and secret (FUELCHECK_API_KEY and FUELCHECK_API_SECRET
in .env; see "FuelCheck key" in the README). Its free plan allows 2,500 calls a month;
this uses about 14 a day (12 price lists and 2 sign-ins). API.NSW locks an account
after 3 bad sign-ins in a row, so if it refuses the key and secret, sitrep stops
trying until it's restarted.

Data: NSW Government FuelCheck, CC BY-SA.
"""

from __future__ import annotations

import base64
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import Field, SecretStr

from ..geo import km_between
from ..models import Assessment, Category, Level, Signal, national
from ..util import format_duration
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError, retry_after_seconds

KEY_HELP = 'See "FuelCheck key" in the README.'
CREDIT = "Data: NSW Government FuelCheck, CC BY-SA"
DEFAULT_FUELS = {"U91": "Unleaded 91", "DL": "Diesel"}


class FuelSettings(PollingSettings):
    # The free plan allows 2,500 calls a month: every 2 hours uses about 430.
    interval_seconds: float = Field(7200, ge=3600)
    api_key: SecretStr = SecretStr("")
    api_secret: SecretStr = SecretStr("")
    url: str = "https://api.onegov.nsw.gov.au"
    fuels: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_FUELS))
    near_km: float = Field(10, gt=0)
    # A median this many cents a litre above its highest in the last baseline_days is elevated.
    jump_cents: float = Field(10, gt=0)
    baseline_days: int = Field(30, ge=7)
    learning_days: int = Field(7, ge=1)


@dataclass(frozen=True)
class Price:
    station: str
    fuel: str
    cents: float
    updated: float | None
    lat: float | None
    lon: float | None
    name: str = ""


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_updated(text: Any) -> float | None:
    """FuelCheck's "lastupdated", in one of a few formats. It's UTC, though it doesn't say
    so: read as Sydney time, the newest prices would be 11 hours old on a busy afternoon."""
    text = str(text or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text.split(".")[0], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def parse_prices(doc: Any, fuels: dict[str, str]) -> list[Price]:
    if not isinstance(doc, dict) or not isinstance(doc.get("prices"), list):
        raise SourceError("FuelCheck sent no price list")
    stations = {}
    for s in doc.get("stations") or []:
        if not isinstance(s, dict) or (s.get("state") or "NSW") != "NSW":
            continue
        loc = s.get("location") or {}
        stations[str(s.get("code"))] = (_float(loc.get("latitude")), _float(loc.get("longitude")),
                                        str(s.get("name") or ""))
    out = []
    for p in doc["prices"]:
        if not isinstance(p, dict) or p.get("fueltype") not in fuels or (p.get("state") or "NSW") != "NSW":
            continue
        cents, code = _float(p.get("price")), str(p.get("stationcode"))
        if cents is None or cents <= 0 or code not in stations:
            continue
        lat, lon, name = stations[code]
        out.append(Price(code, p["fueltype"], cents, parse_updated(p.get("lastupdated")), lat, lon, name))
    return out


@dataclass(frozen=True)
class Median:
    fuel: str
    area: str  # "home" or "nsw"
    cents: float | None
    stations: int
    usual: float | None = None  # the median of the last baseline_days of hourly medians
    highest: float | None = None  # the highest of them
    days: float = 0  # how many days of history there are

    @property
    def series(self) -> str:
        return f"{self.fuel}:{self.area}"

    def jumped(self, s: FuelSettings) -> bool:
        return (self.cents is not None and self.highest is not None and self.days >= s.learning_days
                and self.cents > self.highest + s.jump_cents)


def medians(prices: list[Price], home: tuple[float, float], s: FuelSettings) -> list[Median]:
    out = []
    for fuel in s.fuels:
        mine = [p for p in prices if p.fuel == fuel]
        near = [p for p in mine if p.lat is not None and p.lon is not None
                and km_between(*home, p.lat, p.lon) <= s.near_km]
        for area, group in (("home", near), ("nsw", mine)):
            middle = round(statistics.median(p.cents for p in group), 2) if group else None
            out.append(Median(fuel, area, middle, len(group)))
    return out


def with_history(m: Median, history: dict[int, float], now: float, s: FuelSettings) -> Median:
    """The median, with what it's usually been (the last baseline_days, not counting the last day)."""
    past = [v for ts, v in history.items() if ts < now - 86400]
    if not past:
        return m
    days = (now - min(history)) / 86400
    return Median(m.fuel, m.area, m.cents, m.stations, statistics.median(past), max(past), days)


def _where(m: Median, s: FuelSettings) -> str:
    return f"within {s.near_km:g} km of home" if m.area == "home" else "across NSW"


def assess(found: list[Median], s: FuelSettings, now: float, newest: float | None,
           cheapest: dict[str, Price] | None = None) -> Assessment:
    jumped = [m for m in found if m.jumped(s)]
    learning = max((m.days for m in found), default=0) < s.learning_days
    home = {m.fuel: m for m in found if m.area == "home"}
    nsw = {m.fuel: m for m in found if m.area == "nsw"}

    def price(m: Median | None) -> str:
        return f"{m.cents:.1f}" if m and m.cents is not None else "no price"

    now_text = "; ".join(f"{s.fuels[f]} {price(home.get(f))} near home, {price(nsw.get(f))} across NSW"
                         for f in s.fuels)
    if jumped:
        level = Level.ELEVATED
        reason = "; ".join(
            f"{s.fuels[m.fuel]} {_where(m, s)} is {m.cents:.1f} c/L, {m.cents - m.highest:.1f} c/L above its "
            f"highest in the last {s.baseline_days} days ({m.highest:.1f}; usually {m.usual:.1f})"
            for m in jumped
        )
    elif learning:
        days_left = s.learning_days - max((m.days for m in found), default=0)
        level = Level.LEARNING
        reason = f"Learning usual prices: {format_duration(days_left * 86400)} to go. Now (c/L): {now_text}"
    else:
        level = Level.NORMAL
        reason = f"Median prices (c/L): {now_text}. No sudden jump"
    signals = tuple(
        Signal(f"fuel:{m.series}", Level.ELEVATED, (national("fuel"),),
               title=f"{s.fuels[m.fuel]} {_where(m, s)} jumped to {m.cents:.1f} c/L", at=now)
        for m in jumped
    )
    details = {
        "fuels": [
            {"code": f, "name": s.fuels[f],
             **{area: {"cents": m.cents, "stations": m.stations, "usual": m.usual, "highest": m.highest,
                       "jumped": m.jumped(s)}
                for area, m in (("home", home.get(f)), ("nsw", nsw.get(f))) if m}}
            for f in s.fuels
        ],
        "cheapest": {f: {"name": p.name, "cents": p.cents} for f, p in (cheapest or {}).items()},
        "near_km": s.near_km,
        "jump_cents": s.jump_cents,
        "baseline_days": s.baseline_days,
        "credit": CREDIT,
    }
    return Assessment(level, reason, details, content_at=newest, signals=signals)


def request_timestamp(now: float) -> str:
    """API.NSW's requesttimestamp: UTC, "dd/MM/yyyy hh:mm:ss AM/PM"."""
    return datetime.fromtimestamp(now, timezone.utc).strftime("%d/%m/%Y %I:%M:%S %p")


class FuelAdapter(PollingAdapter):
    id = "fuel"
    name = "Fuel prices (FuelCheck)"
    category = Category.STRESS
    link = "https://www.fuelcheck.nsw.gov.au/app"
    Settings = FuelSettings
    scope = national("fuel")
    # Prices change all day; overnight a few hours can pass with none across NSW.
    content_budget_minutes = 8 * 60
    settings: FuelSettings

    def __init__(self, ctx: AdapterContext, settings: FuelSettings) -> None:
        super().__init__(ctx, settings)
        self._token: str | None = None
        self._token_until = 0.0
        self._refused = False  # API.NSW refused the key and secret: don't try again until restarted

    def _credentials(self) -> tuple[str, str]:
        key = self.settings.api_key.get_secret_value().strip()
        secret = self.settings.api_secret.get_secret_value().strip()
        if not key or not secret:
            raise SourceError(f"No FuelCheck key yet: add FUELCHECK_API_KEY and FUELCHECK_API_SECRET to .env. "
                              f"{KEY_HELP}")
        return key, secret

    async def token(self, now: float) -> str:
        """An OAuth access token, reused until shortly before it expires (about 12 hours)."""
        if self._refused:
            raise SourceError("API.NSW refused the FuelCheck key and secret, so sitrep has stopped trying (3 bad "
                              f"tries in a row lock the account). Fix them in .env and restart sitrep. {KEY_HELP}")
        if self._token and now < self._token_until:
            return self._token
        key, secret = self._credentials()
        basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
        response = await self.ctx.http.get(
            f"{self.settings.url.rstrip('/')}/oauth/client_credential/accesstoken",
            params={"grant_type": "client_credentials"}, headers={"Authorization": f"Basic {basic}"},
        )
        if response.status_code in (400, 401, 403):
            self._refused = True
            raise SourceError("API.NSW refused the FuelCheck key and secret. Check FUELCHECK_API_KEY and "
                              f"FUELCHECK_API_SECRET in .env, then restart sitrep. {KEY_HELP}")
        if response.status_code == 429:
            raise SourceError("API.NSW asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 400:
            raise SourceError(f"API.NSW's sign-in answered HTTP {response.status_code}")
        try:
            answer = response.json()
            token, lasts = str(answer["access_token"]), float(answer.get("expires_in") or 3600)
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceError("API.NSW's sign-in sent no access token") from exc
        self._token, self._token_until = token, now + max(60.0, lasts - 600)
        return token

    async def fetch_prices(self, now: float) -> Any:
        token = await self.token(now)
        key, _ = self._credentials()
        response = await self.ctx.http.get(
            f"{self.settings.url.rstrip('/')}/FuelPriceCheck/v2/fuel/prices", params={"states": "NSW"},
            headers={"Authorization": f"Bearer {token}", "apikey": key, "transactionid": str(uuid.uuid4()),
                     "requesttimestamp": request_timestamp(now), "Content-Type": "application/json; charset=utf-8"},
        )
        if response.status_code == 401:
            self._token = None  # it may have expired early: sign in again next time
            raise SourceError("FuelCheck didn't accept the access token (HTTP 401); sitrep will sign in again")
        if response.status_code == 429:
            raise SourceError("FuelCheck asked sitrep to slow down (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 400:
            raise SourceError(f"FuelCheck answered HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("FuelCheck sent something that isn't JSON") from exc

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        prices = parse_prices(await self.fetch_prices(now), self.settings.fuels)
        if not prices:
            raise SourceError("FuelCheck's price list had no unleaded or diesel prices in it")
        loc = self.ctx.config.home.location
        found = []
        since = now - self.settings.baseline_days * 86400
        for m in medians(prices, (loc.lat, loc.lon), self.settings):
            if m.cents is None:
                found.append(m)
                continue
            history = self.ctx.db.gauges_since(self.id, m.series, since)
            found.append(with_history(m, history, now, self.settings))
            self.ctx.db.set_gauge(self.id, m.series, now, m.cents)
            self.ctx.db.set_gauge(self.id, f"{m.series}:stations", now, m.stations)
        cheapest = {}
        for fuel in self.settings.fuels:
            near = [p for p in prices if p.fuel == fuel and p.lat is not None and p.lon is not None
                    and km_between(loc.lat, loc.lon, p.lat, p.lon) <= self.settings.near_km]
            if near:
                cheapest[fuel] = min(near, key=lambda p: p.cents)
        newest = max((p.updated for p in prices if p.updated and p.updated <= now + 600), default=None)
        result = assess(found, self.settings, now, newest, cheapest)
        for m in found:
            if m.jumped(self.settings):
                self.ctx.db.add_event(now, self.id, self.category.value, Level.ELEVATED.value,
                                      f"{self.settings.fuels[m.fuel]} {_where(m, self.settings)} jumped to "
                                      f"{m.cents:.1f} c/L", dedupe_key=f"fuel:{m.series}:{int(now // 86400)}")
        return result
