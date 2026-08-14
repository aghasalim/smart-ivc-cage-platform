from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="researcher")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Cage(Base):
    __tablename__ = "cages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    location: Mapped[str] = mapped_column(String(128), default="")
    animal_strain: Mapped[str] = mapped_column(String(64), default="")
    animal_age_days: Mapped[int] = mapped_column(Integer, default=0)
    device_token_hash: Mapped[str] = mapped_column(String(255), default="")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    readings: Mapped[list["Reading"]] = relationship(back_populates="cage", cascade="all,delete")
    alerts: Mapped[list["Alert"]] = relationship(back_populates="cage", cascade="all,delete")
    schedule: Mapped[list["ScheduleWindow"]] = relationship(back_populates="cage", cascade="all,delete")


class Reading(Base):
    __tablename__ = "readings"
    __table_args__ = (
        UniqueConstraint("cage_id", "sensor", "seq", name="uq_reading_seq"),
        Index("ix_readings_cage_ts", "cage_id", "ts"),
        Index("ix_readings_cage_sensor_ts", "cage_id", "sensor", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cage_id: Mapped[str] = mapped_column(String(64), ForeignKey("cages.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sensor: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    cage: Mapped[Cage] = relationship(back_populates="readings")


class BehaviourLabel(Base):
    __tablename__ = "behaviour_labels"
    __table_args__ = (Index("ix_behaviour_cage_ts", "cage_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cage_id: Mapped[str] = mapped_column(String(64), ForeignKey("cages.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    label: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)


class MouseStateSample(Base):
    """Per-mouse behavioural state from the camera pipeline (BehavioralMonitor).

    One row per tracked mouse per persisted frame (throttled to
    settings.camera_sample_interval_s), so COUNT(*) * interval == seconds-in-state.
    Backs the camera-driven behaviour reports (time budget, ethogram, sleep bouts…).
    """
    __tablename__ = "mouse_state_samples"
    __table_args__ = (
        Index("ix_mss_cage_ts", "cage_id", "ts"),
        Index("ix_mss_cage_mouse_ts", "cage_id", "mouse_id", "ts"),
        Index("ix_mss_cage_state_ts", "cage_id", "state", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cage_id: Mapped[str] = mapped_column(String(64), ForeignKey("cages.id"))
    cam_id: Mapped[str] = mapped_column(String(32), default="")
    mouse_id: Mapped[str] = mapped_column(String(8), default="?")   # track id A–E
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(32))                  # Wake / Active | Quiet / Rest | Possible Sleep
    behavior: Mapped[str] = mapped_column(String(32), default="Normal")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CageSocialSample(Base):
    """Per-frame group/social state from the camera pipeline (group classifier)."""
    __tablename__ = "cage_social_samples"
    __table_args__ = (Index("ix_css_cage_ts", "cage_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cage_id: Mapped[str] = mapped_column(String(64), ForeignKey("cages.id"))
    cam_id: Mapped[str] = mapped_column(String(32), default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    social_state: Mapped[str] = mapped_column(String(32), default="Unknown")  # Huddling | Normal | Exploration | Unknown
    mean_interanimal_dist: Mapped[float] = mapped_column(Float, default=0.0)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    resting_count: Mapped[int] = mapped_column(Integer, default=0)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_cage_ts", "cage_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cage_id: Mapped[str] = mapped_column(String(64), ForeignKey("cages.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    severity: Mapped[str] = mapped_column(String(16))
    rule: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(512))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cage: Mapped[Cage] = relationship(back_populates="alerts")


class ScheduleWindow(Base):
    __tablename__ = "schedule_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cage_id: Mapped[str] = mapped_column(String(64), ForeignKey("cages.id"))
    start_local: Mapped[str] = mapped_column(String(5))  # "HH:MM" in the window's timezone
    end_local: Mapped[str] = mapped_column(String(5))    # "HH:MM" in the window's timezone
    days_of_week: Mapped[str] = mapped_column(String(32), default="0,1,2,3,4,5,6")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # IANA timezone name (e.g. "Europe/Brussels"). Defaults to Belgium since
    # that is where the lab and the original deployment live.
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Brussels", server_default="Europe/Brussels")

    cage: Mapped[Cage] = relationship(back_populates="schedule")


class ApiKey(Base):
    """Long-lived API key for programmatic access (alt to JWT for external apps).

    The plaintext key is shown to the user ONCE on creation, then stored only as
    a SHA-256 hash. Keys are scoped to a user (acts as that user). Optional label
    + expiry + last_used tracking for UX.
    """
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_user", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    prefix: Mapped[str] = mapped_column(String(16), index=True)  # first 8 chars, shown in UI
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
