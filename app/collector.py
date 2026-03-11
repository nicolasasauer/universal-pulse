"""
collector.py — Background API-polling worker for Universal-Pulse.

Architecture
------------
  APScheduler's BackgroundScheduler runs inside the same Python process as the
  FastAPI app (via a lifespan context manager in main.py). Each Tracker gets its
  own IntervalTrigger job.  When a Tracker is added, edited, or deleted through
  the API, the scheduler's job list is patched accordingly.

Key design choices:
  - httpx (async-compatible, modern) is used for outbound HTTP requests so we
    can set reasonable timeouts and handle redirects cleanly.
  - JSON-path resolution is intentional vanilla Python: we walk the dot-separated
    key list so there are no extra dependencies.  If a key looks like an int we
    try it as a list index as well.
  - All errors (network, JSON, path-not-found) are logged and swallowed so a
    single broken tracker never takes down the whole scheduler.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import (
    SessionLocal,
    create_reading,
    get_tracker,
    list_trackers,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level scheduler instance — imported by main.py
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone="UTC")


# ---------------------------------------------------------------------------
# JSON-path resolver
# ---------------------------------------------------------------------------
def resolve_json_path(data: Any, path: str) -> float:
    """
    Walk *data* along a dot-separated *path* and return the leaf value as float.

    Example
    -------
    >>> resolve_json_path({"data": {"price": {"usd": 42.0}}}, "data.price.usd")
    42.0

    Raises
    ------
    KeyError  : if a key is not found in a dict.
    IndexError: if an integer index is out of range.
    ValueError: if the leaf cannot be coerced to float.
    TypeError : if an intermediate node is neither dict nor list.
    """
    parts = path.split(".")
    node: Any = data
    for part in parts:
        if isinstance(node, dict):
            node = node[part]
        elif isinstance(node, list):
            node = node[int(part)]
        else:
            raise TypeError(
                f"Cannot traverse into {type(node).__name__!r} with key {part!r}"
            )
    return float(node)


# ---------------------------------------------------------------------------
# Per-tracker poll function
# ---------------------------------------------------------------------------
def _poll_tracker(tracker_id: int) -> None:
    """
    Fetch the tracker's URL, extract the value at json_path, and store a Reading.
    Designed to be called by APScheduler — any exception is caught and logged.
    """
    db = SessionLocal()
    try:
        tracker = get_tracker(db, tracker_id)
        if tracker is None or not tracker.is_active:
            # The tracker was deleted or paused between when the job was
            # scheduled and when it actually ran.  Removing the job from inside
            # the job callback is safe in APScheduler — the job is already
            # running so the scheduler just won't fire it again.
            _remove_job(tracker_id)
            return

        headers = tracker.get_headers()

        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            response = client.get(tracker.url, headers=headers)
            response.raise_for_status()
            payload = response.json()

        value = resolve_json_path(payload, tracker.json_path)
        create_reading(db, tracker_id=tracker.id, value=value)
        logger.info(
            "Collected tracker=%d name=%r value=%s",
            tracker.id,
            tracker.name,
            value,
        )

    except httpx.HTTPStatusError as exc:
        logger.error(
            "HTTP error for tracker=%d: %s %s",
            tracker_id,
            exc.response.status_code,
            exc.response.text[:200],
        )
    except httpx.RequestError as exc:
        logger.error("Request error for tracker=%d: %s", tracker_id, exc)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.error(
            "JSON path resolution failed for tracker=%d path=%r: %s",
            tracker_id,
            getattr(get_tracker(db, tracker_id), "json_path", "?"),
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — intentional catch-all
        logger.exception("Unexpected error for tracker=%d: %s", tracker_id, exc)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scheduler helpers (called from main.py)
# ---------------------------------------------------------------------------
def _job_id(tracker_id: int) -> str:
    return f"tracker_{tracker_id}"


def _remove_job(tracker_id: int) -> None:
    job_id = _job_id(tracker_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def add_or_update_job(tracker_id: int, interval_minutes: int) -> None:
    """
    Upsert a scheduler job for *tracker_id* with the given *interval_minutes*.
    A new job fires immediately (next_run_time=None lets APScheduler decide) then
    runs on the interval.
    """
    job_id = _job_id(tracker_id)
    trigger = IntervalTrigger(minutes=interval_minutes)

    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.reschedule_job(job_id, trigger=trigger)
        logger.debug("Rescheduled job %s to every %dm", job_id, interval_minutes)
    else:
        scheduler.add_job(
            _poll_tracker,
            trigger=trigger,
            id=job_id,
            args=[tracker_id],
            replace_existing=True,
            misfire_grace_time=60,
        )
        logger.info(
            "Scheduled new job %s every %dm", job_id, interval_minutes
        )


def remove_job(tracker_id: int) -> None:
    """Public wrapper used by the API layer."""
    _remove_job(tracker_id)


# ---------------------------------------------------------------------------
# Bootstrap — load all active trackers from DB on startup
# ---------------------------------------------------------------------------
def bootstrap_scheduler() -> None:
    """
    Seed the scheduler with jobs for every active Tracker already in the DB.
    Called once during app startup so that existing trackers survive restarts.
    """
    db = SessionLocal()
    try:
        active = list_trackers(db, active_only=True)
        for tracker in active:
            add_or_update_job(tracker.id, tracker.interval)
        logger.info("Bootstrapped %d tracker job(s)", len(active))
    finally:
        db.close()
