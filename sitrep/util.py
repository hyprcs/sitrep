"""Small helpers shared across sitrep."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def format_duration(seconds: float) -> str:
    """Plain-English duration: '45 seconds', '3 minutes', '2.5 hours', '4 days'."""
    s = max(0.0, float(seconds))
    if s < 120:
        n = round(s)
        return "1 second" if n == 1 else f"{n} seconds"
    if s < 90 * 60:
        return f"{round(s / 60)} minutes"
    if s < 48 * 3600:
        return _plural(s / 3600, "hour")
    return _plural(s / 86400, "day")


def sydney_timestamp(wall: datetime) -> float:
    """A Sydney wall-clock time (with no zone attached) as a Unix timestamp.

    Sydney is on AEDT (UTC+11) from 2 am on the first Sunday in October until 3 am on
    the first Sunday in April, and AEST (UTC+10) otherwise. Worked out here rather than
    with a time-zone database, which Windows Python and slim Docker images may not have.
    """

    def first_sunday(month: int) -> datetime:
        day = datetime(wall.year, month, 1)
        return day + timedelta(days=(6 - day.weekday()) % 7)

    summer_ends = first_sunday(4).replace(hour=3)
    summer_starts = first_sunday(10).replace(hour=2)
    daylight = wall < summer_ends or wall >= summer_starts
    return wall.replace(tzinfo=timezone(timedelta(hours=11 if daylight else 10))).timestamp()


def sydney_datetime(ts: float) -> datetime:
    """A Unix timestamp as Sydney time. Both clock changes happen at 16:00 UTC on the
    Saturday before the first Sunday (in October and in April), Sydney time."""
    utc = datetime.fromtimestamp(ts, timezone.utc)
    year = (utc + timedelta(hours=10)).year

    def change(month: int) -> datetime:
        day = datetime(year, month, 1, tzinfo=timezone.utc)
        return day + timedelta(days=(6 - day.weekday()) % 7) - timedelta(hours=8)

    daylight = utc < change(4) or utc >= change(10)
    return utc.astimezone(timezone(timedelta(hours=11 if daylight else 10)))


def sydney_clock(ts: float, now: float) -> str:
    """'2:05 pm' today, 'Thu 2:05 pm' within the last week, '29 Oct 2:05 pm' before that."""
    when, today = sydney_datetime(ts), sydney_datetime(now)
    clock = f"{when.hour % 12 or 12}:{when.minute:02d} {'am' if when.hour < 12 else 'pm'}"
    if when.date() == today.date():
        return clock
    if abs(now - ts) < 6 * 86400:
        return f"{when:%a} {clock}"
    return f"{when.day} {when:%b} {clock}"


def _plural(value: float, unit: str) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text} {unit}" + ("" if text == "1" else "s")
