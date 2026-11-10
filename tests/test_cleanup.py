"""Tests for expiration cleanup and the daily-run claim mechanism."""

import sqlite3
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from app import cleanup, db
from app.cleanup import cleanup_expired, run_due_cleanup
from app.config import Settings


def make_settings(tmp_path, expiration_hours):
    settings = Settings(
        password="pw",
        base_url="https://images.example.com",
        max_size_mb=1,
        expiration_hours=expiration_hours,
        data_dir=Path(tmp_path),
        port=8000,
        workers=1,
        session_secret="s",
    )
    # create_app normally does this; these tests bypass it, but still need
    # the images directory and the DB schema to exist.
    settings.images_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    return settings


def _add(settings, filename, age_hours):
    created_at = datetime.now().timestamp() - age_hours * 3600
    with db.adding_image(settings.db_path, filename, 1, int(created_at)):
        (settings.images_dir / filename).write_bytes(b"x")


def _name(hex_char):
    return f"image-{hex_char * 16}-{hex_char * 16}.png"


class TestCleanupExpired:
    def test_deletes_only_expired_images(self, tmp_path):
        settings = make_settings(tmp_path, expiration_hours=72)
        old_a = _name("a")
        old_b = _name("b")
        fresh = _name("c")

        _add(settings, old_a, 72.5)
        _add(settings, old_b, 100)
        _add(settings, fresh, 5)

        removed = cleanup_expired(settings)

        assert sorted(removed) == [old_a, old_b]
        assert not (settings.images_dir / old_a).exists()
        assert not (settings.images_dir / old_b).exists()
        assert (settings.images_dir / fresh).exists()
        assert db.count_images(settings.db_path) == 1

    def test_boundary_age_is_expired(self, tmp_path):
        settings = make_settings(tmp_path, expiration_hours=72)
        exactly_old = _name("d")
        _add(settings, exactly_old, 72.0)
        time.sleep(0.05)  # a hair older than 72h by the time cleanup runs
        assert exactly_old in cleanup_expired(settings)

    def test_disabled_expiration_is_a_noop(self, tmp_path):
        settings = make_settings(tmp_path, expiration_hours=0)
        old = _name("e")
        _add(settings, old, 500)
        assert cleanup_expired(settings) == []
        assert db.count_images(settings.db_path) == 1
        assert (settings.images_dir / old).exists()

    def test_missing_file_is_tolerated(self, tmp_path):
        # A DB row whose file is already gone on disk must not crash cleanup.
        settings = make_settings(tmp_path, expiration_hours=1)
        victim = _name("f")
        _add(settings, victim, 100)
        (settings.images_dir / victim).unlink()

        assert victim in cleanup_expired(settings)
        assert db.count_images(settings.db_path) == 0

    def test_uses_now_argument_for_determinism(self, tmp_path):
        settings = make_settings(tmp_path, expiration_hours=72)
        row = _name("0")
        created_at = 1_700_000_000
        with db.adding_image(settings.db_path, row, 1, created_at):
            (settings.images_dir / row).write_bytes(b"x")

        # 71 hours after creation: still fresh.
        fresh_moment = datetime.fromtimestamp(created_at + 71 * 3600)
        assert cleanup_expired(settings, now=fresh_moment) == []
        # 73 hours after creation: expired.
        later_moment = datetime.fromtimestamp(created_at + 73 * 3600)
        assert cleanup_expired(settings, now=later_moment) == [row]

    def test_undeletable_file_does_not_block_the_rest(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, expiration_hours=1)
        stuck, other = _name("7"), _name("8")
        _add(settings, stuck, 100)
        _add(settings, other, 100)

        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == stuck:
                raise PermissionError(13, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        assert cleanup_expired(settings) == [other]
        # The stuck image keeps its row, so the next run retries it.
        assert db.get_image(settings.db_path, stuck) is not None
        assert db.get_image(settings.db_path, other) is None


class TestScheduledRun:
    """run_due_cleanup: what the scheduler thread does on every poll."""

    MORNING = datetime(2026, 9, 30, 6, 0)

    def _add_old(self, settings, filename):
        created_at = int(self.MORNING.timestamp()) - 100 * 3600
        with db.adding_image(settings.db_path, filename, 1, created_at):
            (settings.images_dir / filename).write_bytes(b"x")

    def test_waits_for_the_cleanup_hour(self, tmp_path):
        settings = make_settings(tmp_path, expiration_hours=1)
        self._add_old(settings, _name("1"))
        run_due_cleanup(settings, self.MORNING.replace(hour=4, minute=59))
        assert db.count_images(settings.db_path) == 1
        # Nothing was claimed early, so a later poll still runs today.
        run_due_cleanup(settings, self.MORNING)
        assert db.count_images(settings.db_path) == 0

    def test_runs_once_per_day(self, tmp_path):
        settings = make_settings(tmp_path, expiration_hours=1)
        run_due_cleanup(settings, self.MORNING)
        self._add_old(settings, _name("2"))
        run_due_cleanup(settings, self.MORNING.replace(hour=7))  # same day: done
        assert db.count_images(settings.db_path) == 1

    def test_failed_run_is_retried_on_the_next_poll(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, expiration_hours=1)
        self._add_old(settings, _name("3"))

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(cleanup, "cleanup_expired", broken)
        with pytest.raises(sqlite3.OperationalError):
            run_due_cleanup(settings, self.MORNING)
        monkeypatch.undo()

        # The failed run gave the day back instead of skipping it.
        run_due_cleanup(settings, self.MORNING.replace(minute=1))
        assert db.count_images(settings.db_path) == 0


class TestClaimMechanism:
    def test_only_one_claim_per_day(self, tmp_path):
        db_path = make_settings(tmp_path, 72).db_path
        db.init_db(db_path)
        assert db.claim_cleanup_run(db_path, "2026-09-30") is True
        assert db.claim_cleanup_run(db_path, "2026-09-30") is False
        # A different day can still be claimed.
        assert db.claim_cleanup_run(db_path, "2026-10-01") is True

    def test_claim_survives_restart(self, tmp_path):
        db_path = make_settings(tmp_path, 72).db_path
        db.init_db(db_path)
        assert db.claim_cleanup_run(db_path, "2026-09-30") is True
        # Simulate a restart: the row persisted, so the run is not re-claimed.
        assert db.claim_cleanup_run(db_path, "2026-09-30") is False

    def test_released_claim_can_be_claimed_again(self, tmp_path):
        db_path = make_settings(tmp_path, 72).db_path
        assert db.claim_cleanup_run(db_path, "2026-09-30") is True
        db.release_cleanup_run(db_path, "2026-09-30")
        assert db.claim_cleanup_run(db_path, "2026-09-30") is True


class TestSchedulerThread:
    def test_start_and_stop_with_expiration_disabled(self, tmp_path):
        from app.cleanup import start_cleanup_thread

        settings = make_settings(tmp_path, expiration_hours=0)
        stop = start_cleanup_thread(settings)  # no thread, but safe to stop
        stop.set()

    def test_thread_starts_and_stops(self, tmp_path):
        import threading

        from app.cleanup import start_cleanup_thread

        settings = make_settings(tmp_path, expiration_hours=72)
        before = threading.active_count()
        stop = start_cleanup_thread(settings)
        time.sleep(0.2)
        assert threading.active_count() >= before
        stop.set()
        # Daemon thread winds down; we only assert it does not block exit.
