"""
database.py — SQLAlchemy models and database-layer helpers for Universal-Pulse.

Design decisions:
  - SQLite is perfect for a single-node Raspberry Pi deployment: zero additional
    services, file-based, and trivially mountable as a Docker volume.
  - SQLAlchemy 2.x "mapped-column" style keeps model definitions explicit and
    type-safe without the overhead of a full ORM session factory per request.
  - A single get_db() generator is compatible with both FastAPI's dependency
    injection and direct calls from the background collector.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    MappedColumn,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

# ---------------------------------------------------------------------------
# Database URL — respects the DATA_DIR environment variable so the path can be
# overridden at runtime (e.g. a Docker volume at /data/pulse.db).
# ---------------------------------------------------------------------------
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = f"sqlite:///{DATA_DIR}/pulse.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # required for SQLite + threading
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Tracker(Base):
    """
    Represents a single API-tracking configuration.

    Fields
    ------
    url         : The HTTP(S) endpoint to poll.
    json_path   : Dot-separated path into the JSON response, e.g. "data.price.usd".
    interval    : Poll interval in **minutes**.
    headers     : Optional JSON string of request headers (e.g. {"X-API-Key": "…"}).
    is_active   : Soft-delete / pause flag — False means the collector skips it.
    """

    __tablename__ = "trackers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    json_path: Mapped[str] = mapped_column(String(255), nullable=False)
    interval: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    headers: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON string
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False
    )

    readings: Mapped[list["Reading"]] = relationship(
        "Reading", back_populates="tracker", cascade="all, delete-orphan"
    )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------
    def get_headers(self) -> dict:
        """Parse the stored JSON headers string; return {} on error/empty."""
        if not self.headers:
            return {}
        try:
            return json.loads(self.headers)
        except json.JSONDecodeError:
            return {}

    def set_headers(self, headers_dict: dict) -> None:
        """Serialise a dict into the headers column."""
        self.headers = json.dumps(headers_dict) if headers_dict else None

    def __repr__(self) -> str:
        return f"<Tracker id={self.id} name={self.name!r} interval={self.interval}m>"


class Reading(Base):
    """
    A single time-stamped data point collected from a Tracker.

    We store only the extracted *numeric* value so we can draw clean
    line charts without any additional parsing downstream.
    """

    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tracker_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trackers.id"), nullable=False, index=True
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, index=True
    )

    tracker: Mapped["Tracker"] = relationship("Tracker", back_populates="readings")

    def __repr__(self) -> str:
        return f"<Reading tracker_id={self.tracker_id} value={self.value} ts={self.timestamp}>"


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Create all tables if they don't exist yet. Safe to call on every startup."""
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Dependency helper (for FastAPI's Depends)
# ---------------------------------------------------------------------------
def get_db():
    """
    Yield a SQLAlchemy session and guarantee it is closed afterwards.

    Usage in a FastAPI route:
        def my_route(db: Session = Depends(get_db)): ...
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CRUD helpers — kept here so both the API *and* the collector can import them
# without touching FastAPI-specific code.
# ---------------------------------------------------------------------------
def create_tracker(
    db: Session,
    name: str,
    url: str,
    json_path: str,
    interval: int,
    headers: dict | None = None,
) -> Tracker:
    tracker = Tracker(
        name=name,
        url=url,
        json_path=json_path,
        interval=interval,
        headers=json.dumps(headers) if headers else None,
    )
    db.add(tracker)
    db.commit()
    db.refresh(tracker)
    return tracker


def get_tracker(db: Session, tracker_id: int) -> Tracker | None:
    return db.get(Tracker, tracker_id)


def list_trackers(db: Session, active_only: bool = False) -> list[Tracker]:
    q = db.query(Tracker)
    if active_only:
        q = q.filter(Tracker.is_active == True)  # noqa: E712
    return q.order_by(Tracker.id).all()


def delete_tracker(db: Session, tracker_id: int) -> bool:
    tracker = get_tracker(db, tracker_id)
    if not tracker:
        return False
    db.delete(tracker)
    db.commit()
    return True


def create_reading(db: Session, tracker_id: int, value: float) -> Reading:
    reading = Reading(tracker_id=tracker_id, value=value)
    db.add(reading)
    db.commit()
    db.refresh(reading)
    return reading


def get_readings(
    db: Session,
    tracker_id: int,
    limit: int = 500,
) -> list[Reading]:
    """Return the most-recent *limit* readings for a given tracker, oldest first."""
    return (
        db.query(Reading)
        .filter(Reading.tracker_id == tracker_id)
        .order_by(Reading.timestamp.asc())
        .limit(limit)
        .all()
    )


def get_latest_reading(db: Session, tracker_id: int) -> Reading | None:
    return (
        db.query(Reading)
        .filter(Reading.tracker_id == tracker_id)
        .order_by(Reading.timestamp.desc())
        .first()
    )
