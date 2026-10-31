"""Settings: config.yaml for everything you might tune, .env for secrets and
per-home values (like your router's address).

config.yaml can refer to environment variables as ${NAME} or ${NAME:-default}.
Values come from .env or the real environment (the real environment wins).
That keeps config.yaml free of anything personal, so it can be shared as-is.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from . import __version__
from .models import THEMES


class Settings(BaseModel):
    """Base for every settings block. Unknown keys are errors, so typos get caught."""

    model_config = ConfigDict(extra="forbid")


class ServerSettings(Settings):
    host: str = "0.0.0.0"
    port: int = 8080


class HomeLocation(Settings):
    """Where home is, for "how far away" and the map. The default is the middle of
    Macquarie Park (the ABS suburb point), not anyone's address."""

    name: str = "Macquarie Park"
    lat: float = Field(-33.77878, ge=-90, le=90)
    lon: float = Field(151.12425, ge=-180, le=180)


class HomeSettings(Settings):
    # Used in plain-English messages, e.g. "it could be your NBN line or Belong".
    isp_name: str = "your ISP"
    # The carrier whose network your ISP uses (Belong uses Telstra's, for example).
    # The network tiles mark it as "your network". Must match a carrier's name.
    carrier: str = ""
    location: HomeLocation = Field(default_factory=HomeLocation)


class MapSettings(Settings):
    # Where `python -m sitrep map` saves the basemap, and where the map page reads it from.
    tiles_path: str = "data/map/sydney.pmtiles"
    # The area to download: west, south, east, north. The default covers Greater Sydney,
    # from the Blue Mountains to the Central Coast.
    bbox: list[float] = Field(default_factory=lambda: [149.97, -34.34, 151.64, -32.99], min_length=4, max_length=4)
    # The most zoomed-in level to keep. 14 shows every street; each extra level about doubles the size.
    max_zoom: int = Field(14, ge=6, le=15)
    builds_url: str = "https://build-metadata.protomaps.dev/builds.json"
    build_url: str = "https://build.protomaps.com/{build}"


class TermsSettings(Settings):
    """Emerging terms: words and two-word phrases suddenly much busier than usual."""

    enabled: bool = True
    # A term is flagged when, over the last window_hours...
    window_hours: float = Field(2, gt=0)
    # ...it has more than this many mentions,
    min_mentions: int = Field(5, ge=1)
    # more than this many times its usual for that long (from the last baseline_days days),
    ratio: float = Field(3, gt=1)
    baseline_days: float = Field(7, gt=0)
    # from at least this many different sources (outlets, or Bluesky accounts),
    min_sources: int = Field(2, ge=1)
    # and it wasn't flagged in the last repeat_minutes.
    repeat_minutes: float = Field(30, ge=0)
    # Words never to treat as terms, on top of everyday English ones. Edit freely.
    stopwords: list[str] = Field(default_factory=lambda: [
        "sydney", "nsw", "new south wales", "australia", "australian", "australians", "aussie",
        "people", "today", "tonight", "week", "news", "says", "said", "just", "like", "going", "really",
        "know", "think", "good", "great", "time", "year", "years", "day", "days", "back", "want",
    ])


class ConvergenceSettings(Settings):
    """Geographic convergence: several kinds of event in one small area at once."""

    enabled: bool = True
    # This many different kinds of event in one 2 km square or the squares around it...
    min_kinds: int = Field(3, ge=2)
    # ...within this many hours is a convergence.
    window_hours: float = Field(2, gt=0)
    # Posts only count as an event when this many different people post about the area.
    social_min_people: int = Field(2, ge=1)


class StorageSettings(Settings):
    db_path: str = "data/sitrep.db"
    keep_minute_counts_days: float = 7
    keep_aggregates_days: float = 90
    keep_sample_posts_days: float = 7
    # Headlines, warnings and other items each source has seen (never article text).
    keep_items_days: float = 7


class HttpSettings(Settings):
    contact: str = ""
    timeout_seconds: float = 10
    retry_first_seconds: float = 30
    retry_max_seconds: float = 1800

    @property
    def user_agent(self) -> str:
        about = "self-hosted situational-awareness dashboard"
        if self.contact.strip():
            about += f"; {self.contact.strip()}"
        return f"sitrep-bot/{__version__} ({about})"


class ScoringSettings(Settings):
    evaluate_every_seconds: float = 30
    raise_after_checks: int = Field(2, ge=1)
    lower_after_minutes: float = Field(15, ge=0)
    learning_days: float = 7
    elevated_when_categories: int = Field(2, ge=1)
    category_critical_when_sources_elevated: dict[str, int] = Field(
        default_factory=lambda: {"social": 2}
    )
    # A source whose last tries in a row have all failed shows as "fetch failing".
    failing_after_tries: int = Field(2, ge=1)
    # Two national signals only back each other up when their themes are the same or
    # are listed here together. Set national_needs_related_themes to false to let any
    # two national signals back each other up.
    national_needs_related_themes: bool = True
    related_themes: list[list[str]] = Field(
        default_factory=lambda: [["internet", "cyber"], ["internet", "power"]]
    )

    @model_validator(mode="after")
    def _themes_are_known(self) -> ScoringSettings:
        for pair in self.related_themes:
            unknown = [t for t in pair if t not in THEMES]
            if len(pair) != 2 or unknown:
                raise ValueError(
                    f"related_themes entries are pairs of themes ({', '.join(THEMES)}); {pair} isn't"
                )
        return self


class TelegramSettings(Settings):
    # Both come from .env. Telegram alerts stay off until both are set.
    bot_token: SecretStr = SecretStr("")
    chat_id: str = ""
    # Telegram's own servers. A self-run Bot API server (or a fake one in tests) works too.
    api_url: str = "https://api.telegram.org"

    @property
    def configured(self) -> bool:
        return bool(self.bot_token.get_secret_value().strip() and self.chat_id.strip())


class AlertsSettings(Settings):
    # Worse news goes out at once. Better news waits until the level has stayed lower
    # this long, so a flare-up that comes straight back isn't announced twice.
    all_clear_after_minutes: float = Field(30, ge=0)
    # After the internet comes back, say how long it was down (if it was down this long).
    report_internet_outages: bool = True
    internet_outage_min_minutes: float = Field(5, ge=0)
    # An item first seen more than this long after its own timestamp is backlog: it's
    # shown, but can't raise an alert. Slow sources get their poll interval plus their
    # usual publishing delay instead, if that's longer.
    recency_limit_minutes: float = Field(15, ge=0)
    # No alerts this soon after sitrep starts, or after the internet comes back.
    startup_grace_minutes: float = Field(2, ge=0)
    # The same event can't alert again within this long.
    repeat_after_minutes: float = Field(30, ge=0)
    # If sitrep itself was down at least this long, say what came in meanwhile once it's back.
    summary_after_restart_minutes: float = Field(5, ge=0)
    summary_top_items: int = Field(5, ge=0)
    # Elevated messages arrive silently unless this is true. Critical always makes a sound.
    elevated_makes_sound: bool = False
    # A link to the dashboard to put in each message, such as http://ds920:8080.
    dashboard_url: str = ""
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)

    @model_validator(mode="after")
    def _dashboard_url_is_web(self) -> AlertsSettings:
        url = self.dashboard_url.strip()
        if url and not re.match(r"https?://[^\s\"<>]+$", url):
            raise ValueError("dashboard_url must start with http:// or https:// (check SITREP_DASHBOARD_URL in .env)")
        return self


class Carrier(Settings):
    """A telco, and the networks (ASNs) it runs. Shared by every source that looks at carriers."""

    name: str
    asns: list[int] = Field(min_length=1)


class Config(Settings):
    log_level: str = "INFO"
    home: HomeSettings = Field(default_factory=HomeSettings)
    carriers: list[Carrier] = Field(default_factory=list)
    server: ServerSettings = Field(default_factory=ServerSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    map: MapSettings = Field(default_factory=MapSettings)
    terms: TermsSettings = Field(default_factory=TermsSettings)
    convergence: ConvergenceSettings = Field(default_factory=ConvergenceSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    alerts: AlertsSettings = Field(default_factory=AlertsSettings)
    # Each source's block is checked by that source's own settings class.
    sources: dict[str, dict[str, Any] | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _home_carrier_is_listed(self) -> Config:
        wanted = self.home.carrier.strip()
        names = [c.name for c in self.carriers]
        if wanted and wanted.casefold() not in (n.casefold() for n in names):
            raise ValueError(
                f"home.carrier is {wanted!r}, which isn't one of the carriers: {', '.join(names) or 'none listed'}. "
                "Check HOME_CARRIER in .env"
            )
        return self


class ConfigError(Exception):
    """config.yaml is missing or has a mistake. The message says where."""


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Replace ${NAME} and ${NAME:-default} in every string inside value."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from .env into the environment. Real env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def format_errors(exc: ValidationError, prefix: str = "") -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"])
        lines.append(f"  - {prefix}{where}: {err['msg']}")
    return "\n".join(lines)


def load_config(
    path: str | os.PathLike[str] | None = None,
    env_file: str | os.PathLike[str] | None = ".env",
) -> Config:
    if env_file:
        load_dotenv(Path(env_file))
    path = Path(path or os.environ.get("SITREP_CONFIG", "config.yaml"))
    if not path.is_file():
        raise ConfigError(f"Can't find {path}. Run sitrep from the folder that contains config.yaml.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} isn't valid YAML: {exc}") from exc
    try:
        return Config.model_validate(expand_env(raw))
    except ValidationError as exc:
        raise ConfigError(f"{path} has a problem:\n{format_errors(exc)}") from exc
