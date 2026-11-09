"""SQLite storage for image metadata.

Connections are short-lived (one per operation) so the app works safely with
multiple uvicorn worker processes: WAL journaling plus a busy timeout let
concurrent writers queue instead of failing.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

#: How often init_db() retries while another worker initialises a new database.
_INIT_ATTEMPTS = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT NOT NULL UNIQUE,
    size       INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cleanup_runs (
    run_date TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS app_secrets (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection that commits on success and rolls back on error."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A connection that holds the database's write lock for the whole block.

    BEGIN IMMEDIATE takes the lock up front, waiting out busy_timeout while
    another connection (in any worker process) holds it. So work done inside
    the block, file changes included, never interleaves with another write
    transaction. Commits on success, rolls back on error.
    """
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def init_db(db_path: Path) -> None:
    """Create the schema (idempotent: every worker runs this at startup).

    When several workers start on a brand-new database at once, switching it
    to WAL needs an exclusive lock, and SQLite reports that contention as
    "database is locked" immediately instead of waiting out busy_timeout.
    So retry briefly until the worker that got there first has finished.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, _INIT_ATTEMPTS + 1):
        try:
            with connect(db_path) as conn:
                conn.executescript(_SCHEMA)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == _INIT_ATTEMPTS:
                raise
            time.sleep(0.05 * attempt)


@contextmanager
def adding_image(db_path: Path, filename: str, size: int, created_at: int) -> Iterator[None]:
    """Insert an image row that is committed only if the block succeeds.

    Write the image file inside the block: the row and the file then appear
    together under the write lock, so delete_all_images() can never see one
    without the other. Raises sqlite3.IntegrityError, before the block runs,
    if the filename is already taken.
    """
    with write_transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO images (filename, size, created_at) VALUES (?, ?, ?)",
            (filename, size, created_at),
        )
        yield


def get_image(db_path: Path, filename: str) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT id, filename, size, created_at FROM images WHERE filename = ?",
            (filename,),
        ).fetchone()


def list_images(db_path: Path, limit: int, offset: int) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, filename, size, created_at FROM images "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return list(rows)


def count_images(db_path: Path) -> int:
    with connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM images").fetchone()
    return int(row[0])


def total_size(db_path: Path) -> int:
    with connect(db_path) as conn:
        row = conn.execute("SELECT COALESCE(SUM(size), 0) FROM images").fetchone()
    return int(row[0])


def delete_image(db_path: Path, filename: str) -> bool:
    """Delete one metadata row. Returns True when a row was removed."""
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM images WHERE filename = ?", (filename,))
    return cur.rowcount > 0


def delete_all_images(db_path: Path, delete_file: Callable[[str], bool]) -> int:
    """Delete every image: ``delete_file(filename)`` for each, then its row.

    Runs as one write transaction, so an upload can't add an image halfway
    through and lose its row while keeping its file. A file that
    ``delete_file`` reports as not deleted keeps its row, so it stays listed
    and can be retried. Returns the number of images deleted.
    """
    with write_transaction(db_path) as conn:
        filenames = [row["filename"] for row in conn.execute("SELECT filename FROM images")]
        deleted = [(filename,) for filename in filenames if delete_file(filename)]
        conn.executemany("DELETE FROM images WHERE filename = ?", deleted)
    return len(deleted)


def find_expired(db_path: Path, cutoff: int) -> list[str]:
    """Return filenames created at or before ``cutoff`` (unix seconds)."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT filename FROM images WHERE created_at <= ?", (cutoff,)
        ).fetchall()
    return [row["filename"] for row in rows]


def claim_cleanup_run(db_path: Path, run_date: str) -> bool:
    """Try to claim the cleanup run for a given YYYY-MM-DD date.

    With multiple uvicorn workers (and restarts), only the caller that wins
    the INSERT performs the cleanup that day.
    """
    with connect(db_path) as conn:
        cur = conn.execute("INSERT OR IGNORE INTO cleanup_runs (run_date) VALUES (?)", (run_date,))
    return cur.rowcount > 0


def release_cleanup_run(db_path: Path, run_date: str) -> None:
    """Give back a claimed run (after a failure) so a later poll retries it."""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM cleanup_runs WHERE run_date = ?", (run_date,))


def get_or_create_secret(db_path: Path, name: str, candidate: str) -> str:
    """Return the secret stored under ``name``, storing ``candidate`` first if unset.

    INSERT OR IGNORE makes this race-free across uvicorn workers: the first
    one to start stores its candidate and every other worker reads it back.
    """
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO app_secrets (name, value) VALUES (?, ?)",
            (name, candidate),
        )
        row = conn.execute(
            "SELECT value FROM app_secrets WHERE name = ?", (name,)
        ).fetchone()
    return row["value"]
