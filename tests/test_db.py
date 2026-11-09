"""Tests for the SQLite layer: startup with several workers, stored secrets."""

import multiprocessing
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from app import db


class TestInitDb:
    def test_retries_while_another_worker_initialises(self, tmp_path, monkeypatch):
        # Switching a new database to WAL fails with "database is locked"
        # (without waiting for busy_timeout) while another worker does it.
        real_connect = db.connect
        attempts = []

        @contextmanager
        def contended_connect(path):
            attempts.append(path)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")
            with real_connect(path) as conn:
                yield conn

        monkeypatch.setattr(db, "connect", contended_connect)
        monkeypatch.setattr(db.time, "sleep", lambda seconds: None)
        db.init_db(tmp_path / "img-to-url.db")
        assert len(attempts) == 3

        monkeypatch.undo()
        assert db.count_images(tmp_path / "img-to-url.db") == 0  # schema exists

    def test_other_errors_are_not_retried(self, tmp_path, monkeypatch):
        attempts = []

        @contextmanager
        def broken_connect(path):
            attempts.append(path)
            raise sqlite3.OperationalError("unable to open database file")
            yield  # pragma: no cover - makes this a generator

        monkeypatch.setattr(db, "connect", broken_connect)
        with pytest.raises(sqlite3.OperationalError, match="unable to open"):
            db.init_db(tmp_path / "img-to-url.db")
        assert len(attempts) == 1


def _boot_worker(db_path, barrier, results):
    """What every uvicorn worker does at startup: init_db, then fetch the
    session key. Errors are reported to the parent instead of lost."""
    barrier.wait()  # start all workers at the same instant
    try:
        db.init_db(Path(db_path))
        results.put(db.get_or_create_secret(Path(db_path), "session_key", secrets.token_hex(32)))
    except Exception as exc:  # noqa: BLE001
        results.put(f"error: {exc!r}")


def test_workers_booting_together_agree_on_one_key(tmp_path):
    ctx = multiprocessing.get_context("spawn")  # separate processes, like uvicorn
    workers = 4
    for trial in range(3):
        db_path = tmp_path / f"trial-{trial}" / "img-to-url.db"
        barrier, results = ctx.Barrier(workers), ctx.Queue()
        procs = [
            ctx.Process(target=_boot_worker, args=(str(db_path), barrier, results))
            for _ in range(workers)
        ]
        for proc in procs:
            proc.start()
        keys = [results.get(timeout=60) for _ in procs]
        for proc in procs:
            proc.join(timeout=10)

        assert not [key for key in keys if key.startswith("error")], keys
        assert len(set(keys)) == 1


class TestSecrets:
    def test_first_value_wins(self, tmp_path):
        db_path = tmp_path / "img-to-url.db"
        db.init_db(db_path)
        assert db.get_or_create_secret(db_path, "session_key", "first") == "first"
        assert db.get_or_create_secret(db_path, "session_key", "second") == "first"

    def test_names_are_independent(self, tmp_path):
        db_path = tmp_path / "img-to-url.db"
        db.init_db(db_path)
        assert db.get_or_create_secret(db_path, "a", "one") == "one"
        assert db.get_or_create_secret(db_path, "b", "two") == "two"
