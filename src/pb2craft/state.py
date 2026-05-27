"""SQLite-backed sync state.

Three tables:

``synced_books``       one row per book we've synced to Craft
``synced_highlights``  one row per individual highlight UUID we've pushed
``metadata``           KV: ``last_sync_at``, ``folder_id``, …

SQLite is fine for this workload — single writer (the sync loop), occasional
reads from the web UI. WAL mode keeps the UI snappy while a sync is running.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pb2craft.models import Highlight


_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_books (
    book_id          TEXT PRIMARY KEY,
    craft_doc_id     TEXT NOT NULL,
    title            TEXT NOT NULL,
    highlight_count  INTEGER NOT NULL DEFAULT 0,
    last_synced_at   TEXT NOT NULL,
    last_known_mtime TEXT
);

CREATE TABLE IF NOT EXISTS synced_highlights (
    highlight_uuid   TEXT PRIMARY KEY,
    book_id          TEXT NOT NULL,
    synced_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_synced_highlights_book
    ON synced_highlights(book_id);

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Every book we've examined, with its server-side mtime at the time.
-- Synced or not — used to skip the entire per-book flow on unchanged books.
CREATE TABLE IF NOT EXISTS seen_books (
    book_id          TEXT PRIMARY KEY,
    last_known_mtime TEXT,
    examined_at      TEXT NOT NULL
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Idempotently add a column to an existing table (SQLite migration)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@dataclass(frozen=True)
class SyncedBook:
    book_id: str
    craft_doc_id: str
    title: str
    highlight_count: int
    last_synced_at: datetime
    last_known_mtime: str | None = None


class SyncStateStore:
    """File-backed sync state. Thread-safe via a local lock + WAL mode."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Migrations for installs created before mtime gating shipped
            _ensure_column(conn, "synced_books", "last_known_mtime", "TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # New connection per call — sqlite3 connections aren't thread-safe by default.
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------------- #
    # Books                                                                   #
    # ---------------------------------------------------------------------- #

    def get_book(self, book_id: str) -> SyncedBook | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM synced_books WHERE book_id = ?",
                (book_id,),
            ).fetchone()
        return _row_to_book(row) if row else None

    def mark_book_synced(
        self,
        *,
        book_id: str,
        craft_doc_id: str,
        title: str,
        highlight_count: int,
        last_known_mtime: str | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO synced_books (
                    book_id, craft_doc_id, title, highlight_count, last_synced_at, last_known_mtime
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id) DO UPDATE SET
                    craft_doc_id     = excluded.craft_doc_id,
                    title            = excluded.title,
                    highlight_count  = excluded.highlight_count,
                    last_synced_at   = excluded.last_synced_at,
                    last_known_mtime = excluded.last_known_mtime
                """,
                (book_id, craft_doc_id, title, highlight_count, now, last_known_mtime),
            )

    # ---------------------------------------------------------------------- #
    # Highlights                                                              #
    # ---------------------------------------------------------------------- #

    def get_synced_highlight_ids(self, book_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT highlight_uuid FROM synced_highlights WHERE book_id = ?",
                (book_id,),
            ).fetchall()
        return {row[0] for row in rows}

    def mark_highlights_synced(self, *, book_id: str, highlight_uuids: list[str]) -> None:
        if not highlight_uuids:
            return
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO synced_highlights (highlight_uuid, book_id, synced_at)
                VALUES (?, ?, ?)
                """,
                [(uuid, book_id, now) for uuid in highlight_uuids],
            )

    def filter_unsynced(self, *, book_id: str, highlights: list[Highlight]) -> list[Highlight]:
        """Return the subset of highlights whose UUIDs aren't recorded yet."""
        synced = self.get_synced_highlight_ids(book_id)
        return [h for h in highlights if h.uuid not in synced]

    # ---------------------------------------------------------------------- #
    # Examined books (mtime-based fast-skip, even for books with no content)  #
    # ---------------------------------------------------------------------- #

    def get_seen_mtime(self, book_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_known_mtime FROM seen_books WHERE book_id = ?",
                (book_id,),
            ).fetchone()
        return row[0] if row else None

    def mark_book_seen(self, book_id: str, mtime: str | None) -> None:
        """Record that we've examined ``book_id`` at the given ``mtime``.

        Called for every book the sync visits, regardless of whether anything
        was actually pushed to Craft — so subsequent runs can skip books with
        no highlights, not just synced books.
        """
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO seen_books (book_id, last_known_mtime, examined_at)
                VALUES (?, ?, ?)
                ON CONFLICT(book_id) DO UPDATE SET
                    last_known_mtime = excluded.last_known_mtime,
                    examined_at      = excluded.examined_at
                """,
                (book_id, mtime, now),
            )

    # ---------------------------------------------------------------------- #
    # Metadata (KV)                                                           #
    # ---------------------------------------------------------------------- #

    def _get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (key,),
            ).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str | None) -> None:
        with self._lock, self._connect() as conn:
            if value is None:
                conn.execute("DELETE FROM metadata WHERE key = ?", (key,))
            else:
                conn.execute(
                    """
                    INSERT INTO metadata (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )

    def get_folder_id(self) -> str | None:
        return self._get_meta("folder_id")

    def set_folder_id(self, folder_id: str | None) -> None:
        self._set_meta("folder_id", folder_id)

    def mark_sync_complete(self) -> None:
        self._set_meta("last_sync_at", datetime.now(tz=timezone.utc).isoformat())

    def get_last_sync_at(self) -> datetime | None:
        raw = self._get_meta("last_sync_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    # ---------------------------------------------------------------------- #
    # Reset                                                                   #
    # ---------------------------------------------------------------------- #

    def reset(self) -> None:
        """Drop all sync state. Folder id is preserved so we don't lose the Craft folder linkage."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM synced_books")
            conn.execute("DELETE FROM synced_highlights")
            conn.execute("DELETE FROM seen_books")
            conn.execute("DELETE FROM metadata WHERE key = 'last_sync_at'")

    # ---------------------------------------------------------------------- #
    # Diagnostics for the web UI                                              #
    # ---------------------------------------------------------------------- #

    def summary(self) -> dict[str, int | str | None]:
        with self._connect() as conn:
            books = conn.execute("SELECT COUNT(*) FROM synced_books").fetchone()[0]
            highlights = conn.execute("SELECT COUNT(*) FROM synced_highlights").fetchone()[0]
        last = self.get_last_sync_at()
        return {
            "books": books,
            "highlights": highlights,
            "last_sync_at": last.isoformat() if last else None,
        }


def _row_to_book(row: sqlite3.Row) -> SyncedBook:
    return SyncedBook(
        book_id=row["book_id"],
        craft_doc_id=row["craft_doc_id"],
        title=row["title"],
        highlight_count=row["highlight_count"],
        last_synced_at=datetime.fromisoformat(row["last_synced_at"]),
        last_known_mtime=row["last_known_mtime"] if "last_known_mtime" in row.keys() else None,
    )
