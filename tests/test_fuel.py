"""NSW fuel prices from FuelCheck. Made-up prices, in the shape API.NSW's documentation gives."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr

from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.fuel import (
    FuelAdapter, FuelSettings, Median, assess, medians, parse_prices, parse_updated, request_timestamp, with_history,
)
from sitrep.config import Config
from sitrep.db import Database
from sitrep.models import Level, national
from sitrep.state import StatusBoard

NOW = 1_793_412_000.0  # 2026-10-31 02:00 UTC, 1 pm in Sydney
HOME = (-33.77878, 151.12425)  # Macquarie Park
S = FuelSettings()


def station(code, lat, lon, name=None, state="NSW"):
    return {"brandid": "1", "stationid": f"s-{code}", "brand": "Example", "code": str(code),
            "name": name or f"Example {code}", "address": "1 Example St", "state": state,
            "location": {"latitude": lat, "longitude": lon}}


def price(code, fuel, cents, updated="31/10/2026 13:05:00", state="NSW"):
    return {"stationcode": str(code), "fueltype": fuel, "price": cents, "lastupdated": updated, "state": state}


def doc(near=(189.9, 191.9, 199.9), far=(179.9, 181.9), diesel=(201.9,)):
    """Stations near home (Ryde, ~3 km away) and far away (Newcastle), with their prices."""
    stations, prices = [], []
    for i, cents in enumerate(near):
        stations.append(station(100 + i, -33.80, 151.10))
        prices.append(price(100 + i, "U91", cents))
    for i, cents in enumerate(far):
        stations.append(station(200 + i, -32.93, 151.78))
        prices.append(price(200 + i, "U91", cents))
    for i, cents in enumerate(diesel):
        prices.append(price(100 + i, "DL", cents))
    stations.append(station(900, -42.88, 147.33, state="TAS"))
    prices += [price(900, "U91", 99.9, state="TAS"), price(100, "P98", 230.9), price(555, "U91", 150.0),
               price(101, "DL", 0)]
    return {"stations": stations, "prices": prices}


# --- Reading the prices ---


def test_last_updated_is_utc_in_either_format():
    expected = datetime(2026, 10, 31, 13, 5, tzinfo=timezone.utc).timestamp()
    assert parse_updated("31/10/2026 13:05:00") == expected
    assert parse_updated("31/10/2026 01:05:00 PM") == expected
    assert parse_updated("yesterday") is None


def test_prices_keep_nsw_stations_and_the_tracked_fuels():
    prices = parse_prices(doc(), S.fuels)
    assert {p.fuel for p in prices} == {"U91", "DL"}  # not P98
    assert all(p.cents != 99.9 and p.station != "555" for p in prices)  # not Tasmania, not an unknown station
    assert len(prices) == 6  # the free (0 c/L) diesel isn't a price
    with pytest.raises(SourceError, match="no price list"):
        parse_prices({"stations": []}, S.fuels)


def test_medians_near_home_and_across_nsw():
    found = {m.series: m for m in medians(parse_prices(doc(), S.fuels), HOME, S)}
    assert (found["U91:home"].cents, found["U91:home"].stations) == (191.9, 3)
    assert (found["U91:nsw"].cents, found["U91:nsw"].stations) == (189.9, 5)
    assert found["DL:home"].cents == 201.9


# --- Judging ---


def cycle(days=30, low=180.0, high=210.0):
    """Hourly medians going round Sydney's price cycle: a slow fall, then a sudden rise."""
    out = {}
    for h in range(days * 24):
        ts = int((NOW - (days * 24 - h) * 3600) // 3600 * 3600)
        step = (h % (14 * 24)) / (14 * 24)
        out[ts] = high - (high - low) * step
    return out


def test_a_jump_must_pass_the_cycles_usual_peak():
    history = cycle()
    peak = with_history(Median("U91", "home", 215.0, 40), history, NOW, S)
    assert round(peak.highest, 1) == 210.0 and not peak.jumped(S)  # a high point of the cycle
    jump = with_history(Median("U91", "home", 225.0, 40), history, NOW, S)
    assert jump.jumped(S)
    result = assess([jump, Median("U91", "nsw", 205.0, 2000, 195.0, 209.0, 30)], S, NOW, NOW - 600)
    assert result.level is Level.ELEVATED
    assert result.reason.startswith("Unleaded 91 within 10 km of home is 225.0 c/L, 15.0 c/L above its highest in the "
                                    "last 30 days (210.0; usually")
    (signal,) = result.signals
    assert signal.scopes == (national("fuel"),) and signal.key == "fuel:U91:home"


def test_it_learns_for_a_week_first():
    short = {ts: v for ts, v in cycle().items() if ts > NOW - 3 * 86400}
    m = with_history(Median("U91", "home", 260.0, 40), short, NOW, S)
    assert not m.jumped(S)
    result = assess([m], S, NOW, NOW)
    assert result.level is Level.LEARNING and result.reason.startswith("Learning usual prices: 4 days to go")
    assert assess([Median("U91", "home", 189.9, 40)], S, NOW, NOW).level is Level.LEARNING  # no history at all


def test_a_quiet_day_says_the_prices():
    found = [with_history(Median(f, a, c, 40), cycle(), NOW, S)
             for f, a, c in (("U91", "home", 189.9), ("U91", "nsw", 186.5), ("DL", "home", 199.9), ("DL", "nsw", 197.9))]
    result = assess(found, S, NOW, NOW - 300)
    assert result.level is Level.NORMAL
    assert result.reason == ("Median prices (c/L): Unleaded 91 189.9 near home, 186.5 across NSW; Diesel 199.9 near "
                             "home, 197.9 across NSW. No sudden jump")


def test_the_request_timestamp_is_utc_with_am_pm():
    assert request_timestamp(NOW) == "31/10/2026 02:00:00 AM"


# --- The adapter ---


class Server:
    def __init__(self):
        self.prices = doc()
        self.token_status = 200
        self.price_status = 200
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "accesstoken" in request.url.path:
            self.asked.append("token")
            assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"my-key:my-secret").decode()
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"ErrorCode": "invalid_client", "Error": "Bad"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": "43199", "token_type": "BearerToken"})
        self.asked.append("prices")
        assert request.headers["Authorization"] == "Bearer tok" and request.headers["apikey"] == "my-key"
        assert request.headers["transactionid"] and request.headers["requesttimestamp"].endswith(("AM", "PM"))
        assert request.url.params["states"] == "NSW"
        return httpx.Response(self.price_status, json=self.prices)


def make(server, now, key="my-key", secret="my-secret"):
    db = Database(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: now[0]), http, lambda: now[0])
    adapter = FuelAdapter(ctx, FuelSettings(api_key=SecretStr(key), api_secret=SecretStr(secret)))
    adapter.register()
    return adapter, db, http


async def test_it_signs_in_once_and_saves_each_median():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    first = await adapter.poll()
    assert first.level is Level.LEARNING and "Unleaded 91 191.9 near home" in first.reason
    assert first.details["cheapest"]["U91"] == {"name": "Example 100", "cents": 189.9}
    now[0] += 7200
    await adapter.poll()
    assert server.asked == ["token", "prices", "prices"]  # the token lasts about 12 hours
    assert list(db.gauges_since("fuel", "U91:home", 0).values()) == [191.9, 191.9]
    assert list(db.gauges_since("fuel", "U91:home:stations", 0).values()) == [3, 3]
    await http.aclose()
    db.close()


async def test_refused_credentials_stop_it_trying():
    server, now = Server(), [NOW]
    server.token_status = 401
    adapter, db, http = make(server, now)
    with pytest.raises(SourceError, match="refused the FuelCheck key and secret"):
        await adapter.poll()
    with pytest.raises(SourceError, match="has stopped trying"):
        await adapter.poll()
    assert server.asked == ["token"]  # API.NSW locks an account after 3 bad tries
    await http.aclose()
    db.close()


async def test_a_refused_token_means_signing_in_again():
    server, now = Server(), [NOW]
    adapter, db, http = make(server, now)
    await adapter.poll()
    server.price_status = 401
    with pytest.raises(SourceError, match="sign in again"):
        await adapter.poll()
    server.price_status = 200
    await adapter.poll()
    assert server.asked == ["token", "prices", "prices", "token", "prices"]
    await http.aclose()
    db.close()


async def test_a_missing_key_says_what_to_do():
    adapter, db, http = make(Server(), [NOW], key="", secret="")
    with pytest.raises(SourceError, match="add FUELCHECK_API_KEY and FUELCHECK_API_SECRET to .env"):
        await adapter.poll()
    await http.aclose()
    db.close()
