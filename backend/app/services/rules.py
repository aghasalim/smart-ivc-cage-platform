import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import Alert, Reading

log = logging.getLogger(__name__)

INTAKE_LOW_24H_GRAMS = 2.0
RER_LOW = 0.65
RER_HIGH = 1.10
NO_ACTIVITY_MINUTES = 240


def _open_alert(db: Session, cage_id: str, rule: str) -> Alert | None:
    return (
        db.query(Alert)
        .filter(Alert.cage_id == cage_id, Alert.rule == rule, Alert.acknowledged_at.is_(None))
        .first()
    )


def evaluate_rules(
    db: Session,
    cage_id: str,
    sensors: dict[str, dict],
    ts: datetime,
) -> list[Alert]:
    new_alerts: list[Alert] = []

    if "metabolic" in sensors:
        rer = sensors["metabolic"].get("rer", 1.0)
        if rer < RER_LOW or rer > RER_HIGH:
            if not _open_alert(db, cage_id, "rer_out_of_range"):
                alert = Alert(
                    cage_id=cage_id,
                    severity="warning",
                    rule="rer_out_of_range",
                    message=f"RER = {rer:.2f}, outside normal range [{RER_LOW}, {RER_HIGH}].",
                )
                db.add(alert)
                new_alerts.append(alert)

    if "feeding" in sensors:
        end = ts
        start = end - timedelta(hours=24)
        rows = (
            db.query(Reading)
            .filter(
                Reading.cage_id == cage_id,
                Reading.sensor == "feeding",
                Reading.ts >= start,
                Reading.ts <= end,
            )
            .all()
        )
        delivered = sum(float(r.payload.get("delivered_g", 0)) for r in rows)
        wasted = sum(float(r.payload.get("wasted_g", 0)) for r in rows)
        intake = max(delivered - wasted, 0.0)
        if rows and intake < INTAKE_LOW_24H_GRAMS:
            if not _open_alert(db, cage_id, "intake_low_24h"):
                alert = Alert(
                    cage_id=cage_id,
                    severity="warning",
                    rule="intake_low_24h",
                    message=f"24h intake {intake:.2f} g is below threshold of {INTAKE_LOW_24H_GRAMS} g.",
                )
                db.add(alert)
                new_alerts.append(alert)

    if "behaviour" in sensors:
        end = ts
        start = end - timedelta(minutes=NO_ACTIVITY_MINUTES)
        rows = (
            db.query(Reading)
            .filter(
                Reading.cage_id == cage_id,
                Reading.sensor == "behaviour",
                Reading.ts >= start,
                Reading.ts <= end,
            )
            .all()
        )
        total_movement = sum(float(r.payload.get("movement_cm", 0)) for r in rows)
        if rows and total_movement < 10.0:
            if not _open_alert(db, cage_id, "no_activity"):
                alert = Alert(
                    cage_id=cage_id,
                    severity="critical",
                    rule="no_activity",
                    message=f"No activity detected in the last {NO_ACTIVITY_MINUTES // 60} hours.",
                )
                db.add(alert)
                new_alerts.append(alert)

    if new_alerts:
        db.commit()
        for a in new_alerts:
            db.refresh(a)

    return new_alerts
