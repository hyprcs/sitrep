"""BoM warnings: reading AMOC files, which cover Sydney, and the FTP routine.

BoM's terms don't allow passing its data on, so every file here is made up, in BoM's
AMOC format (the same element names and codes BoM uses).
"""

from __future__ import annotations

import ftplib

import pytest

from sitrep.adapters import bom
from sitrep.adapters.base import AdapterContext, SourceError
from sitrep.adapters.bom import BomAdapter, BomSettings, assess, judge, parse_amoc
from sitrep.config import Config
from sitrep.db import Database
from sitrep.models import Level
from sitrep.state import StatusBoard

S = BomSettings()
NOW = 1_793_329_200.0  # 3:00 UTC (2 pm in Sydney) on 30 October 2026, an hour after the made-up warnings were issued


def amoc(identifier: str, headline: str, areas: list[tuple[str, str]], *, service="WSW", product="W", phase="NEW",
         issued="2026-10-30T02:00:00Z", expires="2026-10-30T08:00:00Z") -> bytes:
    area_xml = "".join(f'<area aac="{code}" phase="{phase}" description="{name}" type="public-district"/>'
                       for code, name in areas)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<amoc version="1.7">
  <source><sender>Australian Government Bureau of Meteorology</sender><region>New South Wales</region></source>
  <identifier>{identifier}</identifier>
  <issue-time-utc>{issued}</issue-time-utc>
  <expiry-time>{expires}</expiry-time>
  <service>{service}</service>
  <product-type>{product}</product-type>
  <phase>{phase}</phase>
  <hazard index="1" phase="{phase}" type="XXX" severity="STD">
    <area-list>{area_xml}</area-list>
    <headline>{headline}</headline>
  </hazard>
</amoc>""".encode()


SYDNEY = [("NSW_PW005", "Sydney Metropolitan")]
SNOWY = [("NSW_PW010", "Snowy Mountains")]


def warning(*args, **kwargs):
    return parse_amoc(amoc(*args, **kwargs))[1]


# --- Reading ---


def test_a_warning_file_is_read_and_a_forecast_is_skipped():
    w = warning("IDN21033", "Severe Thunderstorm Warning for people in Sydney Metropolitan", SYDNEY)
    assert (w.id, w.service, w.phase, w.codes, w.areas) == ("IDN21033", "WSW", "NEW", ["NSW_PW005"], ["Sydney Metropolitan"])
    assert w.issued == 1793325600.0 and w.expires == 1793347200.0
    assert parse_amoc(amoc("IDN10064", "Sydney forecast", SYDNEY, product="F")) == ("F", None)


def test_unsafe_or_broken_xml_is_an_error():
    with pytest.raises(SourceError, match="isn't valid XML"):
        parse_amoc(b"<amoc><unclosed>")


# --- Judging ---


def test_what_counts_for_sydney():
    cases = [
        ("Severe Thunderstorm Warning for very dangerous thunderstorms for people in Sydney Metropolitan", SYDNEY, "WSW",
         "critical", True),
        ("Severe Thunderstorm Warning for people in Sydney Metropolitan", SYDNEY, "WSW", "elevated", True),
        ("Severe Weather Warning for Damaging Winds for Sydney Metropolitan", SYDNEY, "WSW", "elevated", True),
        ("Fire Weather Warning for Greater Sydney Region", [("NSW_FW005", "Greater Sydney Region")], "WFW", "elevated", False),
        ("Major Flood Warning for the Hawkesbury River", [("NSW_RA123", "Hawkesbury River")], "WFL", "elevated", True),
        ("Minor Flood Warning for the Georges River", [("NSW_RA124", "Georges River")], "WFL", "info", False),
        ("Hazardous Surf Warning for Sydney Coast", [("NSW_MW004", "Sydney Coast: Broken Bay to Port Hacking")], "WSM",
         "info", False),
        ("Severe Weather Warning for Damaging Winds for Snowy Mountains forecast district", SNOWY, "WSW", "info", False),
    ]
    for headline, areas, service, kind, emergency in cases:
        w = judge(warning("IDN1", headline, areas, service=service), S)
        assert (w.kind, w.emergency) == (kind, emergency), headline
    assert judge(warning("IDN1", "Hazardous Surf Warning for Sydney Coast",
                         [("NSW_MW004", "Sydney Coast: Broken Bay to Port Hacking")], service="WSM"), S).sydney


def test_levels_and_reasons():
    severe = warning("IDN21033", "Severe Thunderstorm Warning for people in Sydney Metropolitan", SYDNEY)
    snowy = warning("IDN21037", "Severe Weather Warning for Damaging Winds for Snowy Mountains forecast district", SNOWY)
    result = assess([severe, snowy], S, NOW, {"IDN21033": False})
    assert result.level == Level.ELEVATED
    assert result.reason == ("Severe Thunderstorm Warning for people in Sydney Metropolitan. "
                             "1 more warning elsewhere in NSW")
    (signal,) = result.signals
    assert (signal.key, signal.scopes, signal.emergency, signal.alertable) == ("bom:IDN21033", ("sydney",), True, False)
    assert result.details["storm"] is True
    calm = assess([snowy], S, NOW)
    assert calm.level == Level.NORMAL and calm.reason.startswith("No severe weather")


def test_expired_and_cancelled_warnings_are_ignored():
    old = warning("IDN1", "Severe Weather Warning for Sydney Metropolitan", SYDNEY, expires="2026-10-30T01:00:00Z")
    cancelled = warning("IDN2", "Severe Weather Warning for Sydney Metropolitan", SYDNEY, phase="CAN")
    assert assess([old, cancelled], S, NOW).level == Level.NORMAL


# --- The FTP routine, against a pretend server ---


class FakeFTP:
    """Just enough of ftplib.FTP: a folder of files with modification times."""

    files: dict[str, tuple[str, bytes]] = {}
    log: list[str] = []

    def __init__(self, host, timeout):
        self.log.append(f"connect {host}")

    def login(self):
        self.log.append("login")

    def cwd(self, folder):
        self.log.append(f"cwd {folder}")

    def nlst(self, pattern):
        return sorted(self.files)

    def sendcmd(self, command):
        name = command.split()[1]
        self.log.append(command)
        return f"213 {self.files[name][0]}"

    def retrbinary(self, command, callback):
        self.log.append(command)
        callback(self.files[command.split()[1]][1])

    def quit(self):
        self.log.append("quit")

    def close(self):
        pass


async def test_only_new_or_changed_warning_files_are_downloaded(monkeypatch):
    FakeFTP.files = {
        "IDN10064.amoc.xml": ("20261030010000", amoc("IDN10064", "Sydney forecast", SYDNEY, product="F")),
        "IDN21033.amoc.xml": ("20261030020000", amoc("IDN21033", "Severe Thunderstorm Warning for people in "
                                                                  "Sydney Metropolitan", SYDNEY)),
    }
    FakeFTP.log = []
    monkeypatch.setattr(bom.ftplib, "FTP", FakeFTP)
    db = Database(":memory:")
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: NOW), None, lambda: NOW)
    adapter = BomAdapter(ctx, BomSettings())
    adapter.register()
    first = await adapter.poll()
    assert first.level == Level.ELEVATED
    assert adapter.next_interval() == 300  # a thunderstorm warning covers Sydney: look more often
    assert [c for c in FakeFTP.log if c.startswith("RETR")] == ["RETR IDN10064.amoc.xml", "RETR IDN21033.amoc.xml"]
    FakeFTP.log = []
    second = await adapter.poll()
    assert [c for c in FakeFTP.log if c.startswith(("RETR", "MDTM"))] == ["MDTM IDN21033.amoc.xml"]  # forecast skipped
    assert second.level == Level.ELEVATED
    del FakeFTP.files["IDN21033.amoc.xml"]  # BoM takes a warning's file away once it's over
    third = await adapter.poll()
    assert third.level == Level.NORMAL and adapter.next_interval() == 600
    assert [e["title"] for e in db.events_since(0)] == ["BoM: Severe Thunderstorm Warning for people in Sydney Metropolitan"]
    db.close()


async def test_ftp_trouble_is_a_plain_error(monkeypatch):
    class Refused(FakeFTP):
        def login(self):
            raise ftplib.error_perm("530 Login incorrect")

    monkeypatch.setattr(bom.ftplib, "FTP", Refused)
    db = Database(":memory:")
    ctx = AdapterContext(Config(), db, StatusBoard(db, lambda: NOW), None, lambda: NOW)
    adapter = BomAdapter(ctx, BomSettings())
    adapter.register()
    with pytest.raises(SourceError, match="BoM's FTP service had a problem: 530 Login incorrect"):
        await adapter.poll()
    db.close()
