"""SQLite storage: one file, a few small tables, bounded retention.

Calls are synchronous. At sitrep's volumes each one takes well under a
millisecond, so they run directly on the event loop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import StorageSettings

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_status (
    source_id       TEXT PRIMARY KEY,
    level           TEXT NOT NULL,
    reason          TEXT NOT NULL,
    details         TEXT NOT NULL DEFAULT '{}',
    flags           TEXT NOT NULL DEFAULT '[]',
    observed_at     REAL,
    last_attempt_at REAL,
    last_success_at REAL,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS status_history (
    ts        REAL NOT NULL,
    source_id TEXT NOT NULL,
    level     TEXT NOT NULL,
    reason    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS status_history_ts ON status_history (ts);

CREATE TABLE IF NOT EXISTS overall_history (
    ts     REAL NOT NULL,
    level  TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS overall_history_ts ON overall_history (ts);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    source_id  TEXT NOT NULL,
    category   TEXT NOT NULL,
    level      TEXT NOT NULL,
    title      TEXT NOT NULL,
    url        TEXT,
    details    TEXT NOT NULL DEFAULT '{}',
    dedupe_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS counts_minute (
    source_id TEXT NOT NULL,
    series    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (source_id, series, ts)
);

CREATE TABLE IF NOT EXISTS counts_hour (
    source_id TEXT NOT NULL,
    series    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (source_id, series, ts)
);

-- Measured values, one per hour per series (latency to a host, say), for learning what's
-- usual. Unlike counts, a new value for the same hour replaces the old one.
CREATE TABLE IF NOT EXISTS gauges (
    source_id TEXT NOT NULL,
    series    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (source_id, series, ts)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A few example posts that matched a keyword group, so the dashboard can show what
-- people are saying. Deleted when the author deletes the post or their account.
CREATE TABLE IF NOT EXISTS sample_posts (
    uri       TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    series    TEXT NOT NULL,
    author    TEXT NOT NULL,
    ts        REAL NOT NULL,
    text      TEXT NOT NULL,
    url       TEXT
);
CREATE INDEX IF NOT EXISTS sample_posts_series ON sample_posts (source_id, series, ts);
CREATE INDEX IF NOT EXISTS sample_posts_author ON sample_posts (author);

-- Every item a source has seen (an ACSC advisory, an AEMO notice, a headline, ...):
-- what it is, its own timestamp, and when sitrep first saw it. An item first seen long
-- after its own time is backlog, which can be shown but can't raise an alert.
-- Never article text.
CREATE TABLE IF NOT EXISTS items (
    source_id    TEXT NOT NULL,
    item_id      TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    ts           REAL,               -- the item's own time; NULL if it has none
    first_seen   REAL NOT NULL,      -- when sitrep first saw the item
    version_seen REAL NOT NULL,      -- when sitrep first saw this version of it (ts)
    alertable    INTEGER NOT NULL,
    level        TEXT NOT NULL DEFAULT 'info',
    title        TEXT NOT NULL,
    url          TEXT,
    details      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_id, item_id)
);
CREATE INDEX IF NOT EXISTS items_version_seen ON items (version_seen);

-- Story clusters with more than one kind of source (see stories.py), kept for 7 days.
CREATE TABLE IF NOT EXISTS clusters (
    id         TEXT PRIMARY KEY,
    first_at   REAL NOT NULL,
    last_at    REAL NOT NULL,
    title      TEXT NOT NULL,
    place      TEXT,
    level      TEXT NOT NULL,
    kinds      TEXT NOT NULL,
    members    TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Emerging terms: one row per term per mention, for 8 days. "who" is the outlet, or a
-- one-way code for a Bluesky account (never its handle); no text is kept.
CREATE TABLE IF NOT EXISTS term_hits (
    term  TEXT NOT NULL,
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL,   -- news or social
    who   TEXT NOT NULL,
    fresh INTEGER NOT NULL -- new enough when seen to raise an alert
);
CREATE INDEX IF NOT EXISTS term_hits_ts ON term_hits (ts);
CREATE INDEX IF NOT EXISTS term_hits_term ON term_hits (term, ts);

-- Which events have alerted, and when, so the same one can't alert again too soon.
CREATE TABLE IF NOT EXISTS alert_keys (
    key        TEXT PRIMARY KEY,
    alerted_at REAL NOT NULL
);
"""

# Columns added after the first release. Added on start-up if an older database lacks them.
MIGRATIONS = {
    "source_status": {
        "content_at": "REAL",
        "feed_at": "REAL",
        "signals": "TEXT NOT NULL DEFAULT '[]'",
    },
}


@dataclass(frozen=True)
class Seen:
    """What the item store knows about an item after seeing it again."""

    first_seen: float
    alertable: bool
    new: bool  # first time sitrep has seen this item at all
    updated: bool  # sitrep had seen it, but this is a new version (its own time changed)


class Database:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                have = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
                for column, kind in columns.items():
                    if column not in have:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- Source status: survives restarts so the dashboard shows last-known state ---

    def save_source_status(
        self,
        source_id: str,
        *,
        level: str,
        reason: str,
        details: dict[str, Any],
        flags: frozenset[str],
        observed_at: float | None,
        last_attempt_at: float | None,
        last_success_at: float | None,
        last_error: str | None,
        content_at: float | None = None,
        feed_at: float | None = None,
        signals: list[dict[str, Any]] | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO source_status (source_id, level, reason, details, flags,
                                       observed_at, last_attempt_at, last_success_at, last_error,
                                       content_at, feed_at, signals)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id) DO UPDATE SET
                level = excluded.level, reason = excluded.reason,
                details = excluded.details, flags = excluded.flags,
                observed_at = excluded.observed_at, last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at, last_error = excluded.last_error,
                content_at = excluded.content_at, feed_at = excluded.feed_at, signals = excluded.signals
            """,
            (
                source_id, level, reason, json.dumps(details), json.dumps(sorted(flags)),
                observed_at, last_attempt_at, last_success_at, last_error,
                content_at, feed_at, json.dumps(signals or []),
            ),
        )

    def load_source_status(self, source_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM source_status WHERE source_id = ?", (source_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["details"] = json.loads(row["details"])
        row["flags"] = frozenset(json.loads(row["flags"]))
        row["signals"] = json.loads(row.get("signals") or "[]")
        return row

    # --- History and the timeline ---

    def add_status_change(self, ts: float, source_id: str, level: str, reason: str) -> None:
        self.execute(
            "INSERT INTO status_history (ts, source_id, level, reason) VALUES (?, ?, ?, ?)",
            (ts, source_id, level, reason),
        )

    def add_overall(self, ts: float, level: str, reason: str) -> None:
        self.execute(
            "INSERT INTO overall_history (ts, level, reason) VALUES (?, ?, ?)", (ts, level, reason)
        )

    def add_event(
        self,
        ts: float,
        source_id: str,
        category: str,
        level: str,
        title: str,
        *,
        url: str | None = None,
        details: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        """Add a timeline event. Returns False if one with the same dedupe_key exists."""
        inserted = self.execute(
            """
            INSERT OR IGNORE INTO events (ts, source_id, category, level, title, url, details, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, source_id, category, level, title, url, json.dumps(details or {}), dedupe_key),
        )
        return inserted == 1

    def events_since(self, since: float, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT ts, source_id, category, level, title, url, details FROM events
            WHERE ts >= ? ORDER BY ts DESC LIMIT ?
            """,
            (since, limit),
        )
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    # --- Counters, kept in minute buckets (short-term) and hour buckets (long-term) ---

    def add_count(self, source_id: str, series: str, ts: float, value: float = 1.0) -> None:
        with self._lock:
            for table, size in (("counts_minute", 60), ("counts_hour", 3600)):
                self._conn.execute(
                    f"""
                    INSERT INTO {table} (source_id, series, ts, value) VALUES (?, ?, ?, ?)
                    ON CONFLICT (source_id, series, ts) DO UPDATE SET value = value + excluded.value
                    """,
                    (source_id, series, int(ts // size * size), value),
                )

    def counts_since(
        self, source_id: str, series: str, since: float, *, hourly: bool = False
    ) -> list[tuple[int, float]]:
        table = "counts_hour" if hourly else "counts_minute"
        rows = self.query(
            f"SELECT ts, value FROM {table} WHERE source_id = ? AND series = ? AND ts >= ? ORDER BY ts",
            (source_id, series, int(since)),
        )
        return [(row["ts"], row["value"]) for row in rows]

    def bucketed_counts(
        self, source_id: str, since: float, bucket_seconds: int
    ) -> dict[tuple[int, str], float]:
        """Sum minute counts into bigger buckets: {(bucket_start, series): total}."""
        rows = self.query(
            """
            SELECT (ts / ?) * ? AS bucket, series, SUM(value) AS total FROM counts_minute
            WHERE source_id = ? AND ts >= ? GROUP BY bucket, series
            """,
            (bucket_seconds, bucket_seconds, source_id, int(since)),
        )
        return {(row["bucket"], row["series"]): row["total"] for row in rows}

    # --- Gauges: one measured value per hour per series ---

    def set_gauge(self, source_id: str, series: str, ts: float, value: float) -> None:
        """Store the value for the hour that ts falls in, replacing any earlier one."""
        self.execute(
            "INSERT INTO gauges (source_id, series, ts, value) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (source_id, series, ts) DO UPDATE SET value = excluded.value",
            (source_id, series, int(ts // 3600 * 3600), value),
        )

    def gauges_since(self, source_id: str, series: str, since: float) -> dict[int, float]:
        rows = self.query(
            "SELECT ts, value FROM gauges WHERE source_id = ? AND series = ? AND ts >= ?",
            (source_id, series, int(since)),
        )
        return {row["ts"]: row["value"] for row in rows}

    # --- Small key/value store (stream cursors, ETags, ...) ---

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        rows = self.query("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- Sample posts: a handful per keyword group, never the whole stream ---

    def add_sample(
        self, source_id: str, series: str, *, uri: str, author: str, ts: float, text: str,
        url: str | None = None, keep: int = 20,
    ) -> None:
        """Store one example post, keeping only the newest `keep` for its series."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sample_posts (uri, source_id, series, author, ts, text, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uri, source_id, series, author, ts, text, url),
            )
            self._conn.execute(
                """
                DELETE FROM sample_posts WHERE source_id = ? AND series = ? AND uri NOT IN (
                    SELECT uri FROM sample_posts WHERE source_id = ? AND series = ? ORDER BY ts DESC LIMIT ?
                )
                """,
                (source_id, series, source_id, series, keep),
            )

    def samples(self, source_id: str, series: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT uri, author, ts, text, url FROM sample_posts "
            "WHERE source_id = ? AND series = ? ORDER BY ts DESC LIMIT ?",
            (source_id, series, limit),
        )
        return [dict(row) for row in rows]

    def sample_keys(self, source_id: str) -> tuple[set[str], set[str]]:
        """The URIs and authors of every stored sample, for spotting deletions quickly."""
        rows = self.query("SELECT uri, author FROM sample_posts WHERE source_id = ?", (source_id,))
        return {row["uri"] for row in rows}, {row["author"] for row in rows}

    def delete_samples(self, source_id: str, *, uri: str | None = None, author: str | None = None) -> int:
        if uri is not None:
            return self.execute("DELETE FROM sample_posts WHERE source_id = ? AND uri = ?", (source_id, uri))
        if author is not None:
            return self.execute("DELETE FROM sample_posts WHERE source_id = ? AND author = ?", (source_id, author))
        return 0

    # --- Items: what each source has seen, and whether it can still raise an alert ---

    def see_item(
        self,
        source_id: str,
        item_id: str,
        *,
        ts: float | None,
        now: float,
        fresh: bool,
        source_type: str,
        title: str,
        level: str = "info",
        url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Seen:
        """Record that a source has (still) got an item, and say whether it can alert.

        fresh is whether this version was new enough when seen to raise an alert. An
        item that could alert once keeps that, even if a later version arrives late:
        otherwise a warning still in force would quietly stop counting after an outage.
        """
        payload = json.dumps(details or {})
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, first_seen, alertable FROM items WHERE source_id = ? AND item_id = ?",
                (source_id, item_id),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO items (source_id, item_id, source_type, ts, first_seen, version_seen,
                                       alertable, level, title, url, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source_id, item_id, source_type, ts, now, now, int(fresh), level, title, url, payload),
                )
                return Seen(now, fresh, new=True, updated=False)
            changed = row["ts"] != ts
            alertable = bool(row["alertable"]) or (changed and fresh)
            self._conn.execute(
                """
                UPDATE items SET ts = ?, alertable = ?, level = ?, title = ?, url = ?, details = ?,
                                 version_seen = CASE WHEN ? THEN ? ELSE version_seen END
                WHERE source_id = ? AND item_id = ?
                """,
                (ts, int(alertable), level, title, url, payload, int(changed), now, source_id, item_id),
            )
            return Seen(row["first_seen"], alertable, new=False, updated=changed)

    def items_seen_between(self, start: float, end: float) -> list[dict[str, Any]]:
        """Items first seen (or updated) between start and end whose own time is in that span too."""
        rows = self.query(
            """
            SELECT source_id, item_id, source_type, ts, first_seen, version_seen, alertable, level,
                   title, url, details FROM items
            WHERE version_seen >= ? AND version_seen <= ? AND (ts IS NULL OR (ts >= ? AND ts <= ?))
            ORDER BY version_seen
            """,
            (start, end, start, end),
        )
        return [{**dict(row), "alertable": bool(row["alertable"]), "details": json.loads(row["details"])}
                for row in rows]

    # --- Emerging terms ---

    def add_term_hits(self, rows: list[tuple[str, float, str, str, int]]) -> None:
        """(term, ts, kind, who, fresh) for each term in one headline or post."""
        with self._lock:
            self._conn.executemany("INSERT INTO term_hits (term, ts, kind, who, fresh) VALUES (?, ?, ?, ?, ?)", rows)

    # --- Alert keys: which events have alerted, for "the same event can't alert twice" ---

    def mark_alerted(self, keys: list[str] | set[str], now: float) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO alert_keys (key, alerted_at) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET alerted_at = excluded.alerted_at",
                [(key, now) for key in keys],
            )

    # --- Retention ---

    def cleanup(self, now: float, storage: StorageSettings) -> dict[str, int]:
        """Delete data older than the retention settings. Returns rows deleted per table."""
        short = now - storage.keep_minute_counts_days * 86400
        long = now - storage.keep_aggregates_days * 86400
        return {
            "counts_minute": self.execute("DELETE FROM counts_minute WHERE ts < ?", (short,)),
            "counts_hour": self.execute("DELETE FROM counts_hour WHERE ts < ?", (long,)),
            "gauges": self.execute("DELETE FROM gauges WHERE ts < ?", (long,)),
            "events": self.execute("DELETE FROM events WHERE ts < ?", (long,)),
            "status_history": self.execute("DELETE FROM status_history WHERE ts < ?", (long,)),
            "overall_history": self.execute("DELETE FROM overall_history WHERE ts < ?", (long,)),
            "sample_posts": self.execute(
                "DELETE FROM sample_posts WHERE ts < ?", (now - storage.keep_sample_posts_days * 86400,)
            ),
            "items": self.execute(
                "DELETE FROM items WHERE version_seen < ?", (now - storage.keep_items_days * 86400,)
            ),
            "alert_keys": self.execute("DELETE FROM alert_keys WHERE alerted_at < ?", (now - 2 * 86400,)),
            "term_hits": self.execute("DELETE FROM term_hits WHERE ts < ?", (now - 8 * 86400,)),
            "clusters": self.execute(
                "DELETE FROM clusters WHERE last_at < ?", (now - storage.keep_items_days * 86400,)
            ),
        }
