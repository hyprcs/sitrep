"""SQLite storage: one file, a few small tables, bounded retention.

Calls are synchronous. At sitrep's volumes each one takes well under a
millisecond, so they run directly on the event loop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
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
"""


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
    ) -> None:
        self.execute(
            """
            INSERT INTO source_status (source_id, level, reason, details, flags,
                                       observed_at, last_attempt_at, last_success_at, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id) DO UPDATE SET
                level = excluded.level, reason = excluded.reason,
                details = excluded.details, flags = excluded.flags,
                observed_at = excluded.observed_at, last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at, last_error = excluded.last_error
            """,
            (
                source_id, level, reason, json.dumps(details), json.dumps(sorted(flags)),
                observed_at, last_attempt_at, last_success_at, last_error,
            ),
        )

    def load_source_status(self, source_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM source_status WHERE source_id = ?", (source_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["details"] = json.loads(row["details"])
        row["flags"] = frozenset(json.loads(row["flags"]))
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

    # --- Retention ---

    def cleanup(self, now: float, storage: StorageSettings) -> dict[str, int]:
        """Delete data older than the retention settings. Returns rows deleted per table."""
        short = now - storage.keep_minute_counts_days * 86400
        long = now - storage.keep_aggregates_days * 86400
        return {
            "counts_minute": self.execute("DELETE FROM counts_minute WHERE ts < ?", (short,)),
            "counts_hour": self.execute("DELETE FROM counts_hour WHERE ts < ?", (long,)),
            "events": self.execute("DELETE FROM events WHERE ts < ?", (long,)),
            "status_history": self.execute("DELETE FROM status_history WHERE ts < ?", (long,)),
            "overall_history": self.execute("DELETE FROM overall_history WHERE ts < ?", (long,)),
            "sample_posts": self.execute(
                "DELETE FROM sample_posts WHERE ts < ?", (now - storage.keep_sample_posts_days * 86400,)
            ),
        }
