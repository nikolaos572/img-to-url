"""Daily cleanup of expired images.

Each uvicorn worker starts a lightweight scheduler thread. When the local
time has passed 05:00, workers race to claim that day's run in SQLite; the
winner deletes every image older than IMG_EXPIRATION_HOURS (files and rows).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from app import db, images
from app.config import Settings

log = logging.getLogger("img-to-url.cleanup")

CLEANUP_HOUR = 5  # local time
POLL_SECONDS = 60


def cleanup_expired(settings: Settings, now: datetime | None = None) -> list[str]:
    """Delete images older than the configured expiration. Returns filenames removed."""
    if settings.expiration_hours <= 0:
        return []

    now = now or datetime.now()
    cutoff = now.timestamp() - settings.expiration_hours * 3600
    filenames = db.find_expired(settings.db_path, int(cutoff))

    removed = []
    for filename in filenames:
        # A file that can't be deleted keeps its row, so the next run
        # retries it, and doesn't block the other expired images.
        if images.delete_file(settings.images_dir, filename):
            db.delete_image(settings.db_path, filename)
            removed.append(filename)

    if removed:
        log.info("Cleanup removed %d expired image(s)", len(removed))
    return removed


def run_due_cleanup(settings: Settings, now: datetime) -> None:
    """Run today's cleanup if it is due and no worker has claimed it yet."""
    if now.hour < CLEANUP_HOUR:
        return
    run_date = now.strftime("%Y-%m-%d")
    if not db.claim_cleanup_run(settings.db_path, run_date):
        return
    try:
        cleanup_expired(settings, now)
    except Exception:
        # Un-claim the day so the next poll (in any worker) retries it,
        # rather than silently skipping expiration until tomorrow.
        db.release_cleanup_run(settings.db_path, run_date)
        raise


def _run_daily_cleanup(settings: Settings, stop: threading.Event) -> None:
    while not stop.wait(POLL_SECONDS):
        try:
            run_due_cleanup(settings, datetime.now())
        except Exception:  # noqa: BLE001 - a scheduler thread must never die
            log.exception("Cleanup run failed; will retry next poll")


def start_cleanup_thread(settings: Settings) -> threading.Event:
    """Start the background scheduler. Returns the stop event for shutdown."""
    stop = threading.Event()
    if settings.expiration_hours <= 0:
        return stop  # nothing scheduled; keep the event for a no-op shutdown

    thread = threading.Thread(
        target=_run_daily_cleanup,
        args=(settings, stop),
        name="img-to-url-cleanup",
        daemon=True,
    )
    thread.start()
    return stop
