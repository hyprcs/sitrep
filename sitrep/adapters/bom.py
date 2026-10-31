"""Bureau of Meteorology: NSW weather warnings that cover Sydney.

BoM's website blocks automated access ("does not support web scraping"), so sitrep uses
the anonymous FTP service BoM provides for programs: ftp://ftp.bom.gov.au/anon/gen/fwo/.
BoM's terms allow personal use; passing its data on to others isn't allowed, so keep the
dashboard private (on your tailnet) and don't publish what it shows.

Every 10 minutes (every 5 while a thunderstorm warning covers Sydney) sitrep lists the NSW
files in BoM's machine-readable "AMOC" format and asks when each warning file last changed;
it downloads only new or changed ones (a few kilobytes each). Forecasts in the same folder
are recognised on first sight and skipped from then on.

Each warning names the areas it covers by code, and says what it is in its headline. How
it's judged, for warnings that cover Sydney (Sydney Metropolitan, NSW_PW005, and the other
areas and river names under sources.bom in config.yaml):

- critical: a warning about very dangerous thunderstorms;
- elevated: a Severe Weather, Severe Thunderstorm, Fire Weather or Major Flood warning.
  These are "emergency-level" items too (except Fire Weather, which is a forecast of
  conditions rather than an event), which can make a story or a convergence alert critical;
- everything else (minor and moderate floods, marine and surf warnings, frost) is shown,
  but doesn't count. Warnings elsewhere in NSW are listed, but don't count.

BoM doesn't issue Emergency Warnings under the Australian Warning System; the NSW SES
does, and it has no public feed. Warnings are quiet by nature (days can pass without one),
so this source is never "frozen"; it shows a problem only when the FTP service can't be
reached.
"""

from __future__ import annotations

import asyncio
import ftplib
import io
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from defusedxml import ElementTree
from pydantic import Field

from ..models import SYDNEY, Assessment, Category, Level, Signal
from .base import AdapterContext, PollingAdapter, PollingSettings, SourceError

CRITICAL_WORDS = re.compile(r"very dangerous thunderstorm", re.IGNORECASE)
SEVERE = {  # headline phrase -> (counts as an emergency-level item?)
    "severe weather warning": True,
    "severe thunderstorm warning": True,
    "major flood warning": True,
    "fire weather warning": False,
}


class BomSettings(PollingSettings):
    interval_seconds: float = 600
    # While a thunderstorm warning covers Sydney, look this often instead.
    storm_interval_seconds: float = 300
    host: str = "ftp.bom.gov.au"
    folder: str = "/anon/gen/fwo"
    # NSW's warnings and forecasts in BoM's AMOC format.
    pattern: str = "IDN*.amoc.xml"
    timeout_seconds: float = 30
    # A warning covers Sydney when it lists one of these area codes...
    areas: list[str] = Field(default_factory=lambda: ["NSW_PW005", "NSW_ME001"])
    # ...or an area whose name contains one of these (flood warnings name rivers).
    area_names: list[str] = Field(default_factory=lambda: [
        "Sydney Metropolitan", "Greater Sydney", "Sydney Coast", "Hawkesbury", "Nepean", "Georges River",
        "Parramatta River", "Cooks River", "Lane Cove River", "Woronora",
    ])


@dataclass
class Warning:
    id: str  # the product id, such as IDN21037
    headline: str
    service: str  # BoM's service code: WSW is land weather, WSM marine, and so on
    issued: float | None
    expires: float | None
    phase: str  # NEW, UPD, REN or CAN (cancelled)
    areas: list[str] = field(default_factory=list)  # area names
    codes: list[str] = field(default_factory=list)  # area codes
    sydney: bool = False
    kind: str = "info"  # critical, elevated or info, for Sydney
    emergency: bool = False

    def current(self, now: float) -> bool:
        return self.phase != "CAN" and (self.expires is None or now <= self.expires)


def _time(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def parse_amoc(data: bytes) -> tuple[str, Warning | None]:
    """(product type, the warning) from one AMOC file; the warning is None for a forecast."""
    try:
        root = ElementTree.fromstring(data)
    except Exception as exc:  # defusedxml raises its own errors for unsafe XML, plus ParseError
        raise SourceError(f"A BoM file isn't valid XML ({type(exc).__name__})") from exc
    product = (root.findtext("product-type") or "").strip()
    if product != "W":
        return product, None
    hazards = root.findall("hazard")
    headlines = [h.findtext("headline") or "" for h in hazards]
    areas = [(a.get("aac") or "", a.get("description") or "") for h in hazards for a in h.iter("area")]
    return product, Warning(
        id=(root.findtext("identifier") or "").strip(),
        headline=re.sub(r"\s+", " ", next((x for x in headlines if x), "")).strip() or "BoM warning",
        service=(root.findtext("service") or "").strip(),
        issued=_time(root.findtext("issue-time-utc")),
        expires=_time(root.findtext("expiry-time")),
        phase=(root.findtext("phase") or "").strip().upper(),
        areas=list(dict.fromkeys(name for _, name in areas if name)),
        codes=list(dict.fromkeys(code for code, _ in areas if code)),
    )


def judge(w: Warning, s: BomSettings) -> Warning:
    names = [n.lower() for n in s.area_names]
    w.sydney = any(c in s.areas for c in w.codes) or any(n in a.lower() for a in w.areas for n in names)
    headline = w.headline.lower()
    w.kind, w.emergency = "info", False
    if w.sydney and w.service != "WSM":  # marine warnings (strong wind, surf) never count
        if CRITICAL_WORDS.search(w.headline):
            w.kind, w.emergency = "critical", True
        else:
            for phrase, emergency in SEVERE.items():
                if phrase in headline:
                    w.kind, w.emergency = "elevated", emergency
                    break
    return w


def assess(warnings: list[Warning], s: BomSettings, now: float, alertable: dict[str, bool] | None = None,
           feed_at: float | None = None) -> Assessment:
    current = [judge(w, s) for w in warnings if w.current(now)]
    counted = sorted((w for w in current if w.kind in ("critical", "elevated")), key=lambda w: w.kind != "critical")
    for_sydney = [w for w in current if w.sydney]
    if counted:
        level = Level.CRITICAL if counted[0].kind == "critical" else Level.ELEVATED
        reason = "; ".join(w.headline for w in counted)
    else:
        level = Level.NORMAL
        reason = "No severe weather, thunderstorm, fire weather or major flood warning for Sydney"
        if for_sydney:
            reason += ". In force for Sydney: " + "; ".join(w.headline for w in for_sydney[:3])
    elsewhere = len(current) - len(for_sydney)
    if elsewhere:
        reason += f". {elsewhere} more warning{'s' if elsewhere != 1 else ''} elsewhere in NSW"

    signals = tuple(
        Signal(f"bom:{w.id}", Level.CRITICAL if w.kind == "critical" else Level.ELEVATED, (SYDNEY,),
               title=w.headline, alertable=(alertable or {}).get(w.id, True), at=w.issued, emergency=w.emergency)
        for w in counted
    )
    details = {
        "warnings": [asdict(w) for w in sorted(current, key=lambda w: (not w.sydney, w.kind != "critical",
                                                                         w.kind != "elevated", w.id))],
        "storm": any("thunderstorm" in w.headline.lower() for w in for_sydney),
    }
    issued = [w.issued for w in current if w.issued is not None and w.issued <= now + 600]
    return Assessment(level, reason, details, content_at=max(issued, default=None), feed_at=feed_at, signals=signals)


@dataclass
class _Seen:
    modified: str  # the FTP server's MDTM answer
    warning: Warning | None


class BomAdapter(PollingAdapter):
    id = "bom"
    name = "BoM warnings"
    category = Category.OFFICIAL
    link = "http://www.bom.gov.au/nsw/warnings/"
    Settings = BomSettings
    scope = SYDNEY
    # Warnings reach the FTP folder a few minutes after they're issued.
    publish_delay_minutes = 10
    settings: BomSettings

    def __init__(self, ctx: AdapterContext, settings: BomSettings) -> None:
        super().__init__(ctx, settings)
        self._files: dict[str, _Seen] = {}  # file name -> what sitrep last read
        self._forecasts: set[str] = set()  # files that turned out to be forecasts: never read again
        self._storm = False

    def next_interval(self) -> float:
        return self.settings.storm_interval_seconds if self._storm else self.settings.interval_seconds

    def _read_folder(self) -> tuple[dict[str, Warning], float | None]:
        """One FTP session: list the files, ask which changed, download those. Runs in a thread."""
        s = self.settings
        newest: float | None = None
        try:
            ftp = ftplib.FTP(s.host, timeout=s.timeout_seconds)
        except OSError as exc:
            raise SourceError(f"Couldn't connect to BoM's FTP service ({exc.__class__.__name__})") from exc
        try:
            ftp.login()  # anonymous
            ftp.cwd(s.folder)
            names = [n.rsplit("/", 1)[-1] for n in ftp.nlst(s.pattern)]
            for name in names:
                if name in self._forecasts:
                    continue
                modified = ftp.sendcmd(f"MDTM {name}").split()[-1]
                if (changed := _mdtm(modified)) is not None:
                    newest = max(newest or changed, changed)
                seen = self._files.get(name)
                if seen and seen.modified == modified:
                    continue
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {name}", buf.write)
                _, warning = parse_amoc(buf.getvalue())
                if warning is None:
                    self._forecasts.add(name)
                    continue
                self._files[name] = _Seen(modified, warning)
            for gone in set(self._files) - set(names):  # BoM removes a warning's file once it expires
                del self._files[gone]
        except ftplib.all_errors as exc:
            raise SourceError(f"BoM's FTP service had a problem: {str(exc)[:120]}") from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
        return {name: seen.warning for name, seen in self._files.items() if seen.warning}, newest

    async def poll(self) -> Assessment:
        now = self.ctx.clock()
        warnings, newest = await asyncio.to_thread(self._read_folder)
        found = list(warnings.values())
        alertable = {
            w.id: self.see_item(w.id, w.issued, title=f"BoM: {w.headline}", now=now,
                                level=judge(w, self.settings).kind,
                                details={"sydney": w.sydney, "emergency": w.emergency, "areas": w.areas[:10],
                                         "expires": w.expires}).alertable
            for w in found if w.current(now)
        }
        result = assess(found, self.settings, now, alertable, newest)
        self._storm = bool(result.details["storm"])
        for w in found:
            if w.current(now) and w.sydney:
                self.ctx.db.add_event(
                    w.issued or now, self.id, self.category.value, w.kind, f"BoM: {w.headline}", url=self.link,
                    details={"reason": ", ".join(w.areas[:5])}, dedupe_key=f"bom:{w.id}:{w.issued}",
                )
        return result


def _mdtm(text: str) -> float | None:
    try:
        return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
