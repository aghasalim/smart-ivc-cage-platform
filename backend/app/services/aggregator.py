import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..ai.anomaly import detector as anomaly_detector
from ..ai.classifier import classifier
from ..config import get_settings
from ..db import SessionLocal
from ..models import Alert, BehaviourLabel, Cage, Reading
from .broadcaster import broadcaster

log = logging.getLogger(__name__)
settings = get_settings()


def _build_features(db: Session, cage_id: str, window_end: datetime) -> dict | None:
    window_start = window_end - timedelta(seconds=settings.aggregator_window_seconds)
    rows = (
        db.query(Reading)
        .filter(
            Reading.cage_id == cage_id,
            Reading.ts >= window_start,
            Reading.ts <= window_end,
            Reading.sensor.in_(["behaviour", "feeding", "weighing"]),
        )
        .all()
    )
    if not rows:
        return None

    grid = [0] * 16
    movement = 0.0
    weight = 0.0
    gate_open = 0
    for r in rows:
        if r.sensor == "behaviour":
            cells = r.payload.get("grid_cells") or [0] * 16
            for i in range(min(16, len(cells))):
                grid[i] += int(cells[i])
            movement += float(r.payload.get("movement_cm", 0))
        elif r.sensor == "weighing":
            weight = max(weight, float(r.payload.get("animal_g", 0)))
        elif r.sensor == "feeding":
            if r.payload.get("gate_state") == "open":
                gate_open = 1

    hour = window_end.hour
    minute = window_end.minute
    return {
        "grid": grid,
        "movement": movement,
        "weight": weight,
        "gate_open": gate_open,
        "hour": hour,
        "minute": minute,
    }


def _latest_metric_values(db: Session, cage_id: str, window_end: datetime) -> dict[str, float]:
    """Pull the most recent RER / movement / water-flow values for the cage,
    used by the anomaly detector. Returns only the metrics that exist."""
    window_start = window_end - timedelta(seconds=settings.aggregator_window_seconds)
    rows = (
        db.query(Reading)
        .filter(
            Reading.cage_id == cage_id,
            Reading.ts >= window_start,
            Reading.ts <= window_end,
            Reading.sensor.in_(["metabolic", "behaviour", "water"]),
        )
        .order_by(Reading.ts.desc())
        .all()
    )
    metrics: dict[str, float] = {}
    for r in rows:
        if r.sensor == "metabolic" and "rer" not in metrics:
            rer = r.payload.get("rer")
            if rer is not None:
                metrics["rer"] = float(rer)
        elif r.sensor == "behaviour" and "movement_cm" not in metrics:
            mv = r.payload.get("movement_cm")
            if mv is not None:
                metrics["movement_cm"] = float(mv)
        elif r.sensor == "water" and "water_flow_ml" not in metrics:
            wf = r.payload.get("flow_ml")
            if wf is not None:
                metrics["water_flow_ml"] = float(wf)
        if len(metrics) == 3:
            break
    return metrics


def _open_anomaly_alert(db: Session, cage_id: str, rule: str) -> Alert | None:
    return (
        db.query(Alert)
        .filter(Alert.cage_id == cage_id, Alert.rule == rule, Alert.acknowledged_at.is_(None))
        .first()
    )


async def aggregator_loop() -> None:
    log.info({"event": "aggregator.start"})
    while True:
        try:
            db = SessionLocal()
            try:
                now = datetime.now(tz=timezone.utc)
                cages = db.query(Cage).all()
                for cage in cages:
                    features = _build_features(db, cage.id, now)
                    if not features:
                        continue
                    # --- Model 1: behaviour classifier --------------------
                    label, confidence = classifier.predict(features)
                    row = BehaviourLabel(
                        cage_id=cage.id,
                        ts=now,
                        label=label,
                        confidence=confidence,
                    )
                    db.add(row)
                    db.commit()
                    await broadcaster.publish({
                        "type": "label",
                        "cage_id": cage.id,
                        "ts": now.isoformat().replace("+00:00", "Z"),
                        "behaviour": label,
                        "confidence": confidence,
                    })

                    # --- Model 2: streaming anomaly detector --------------
                    new_anomalies: list[Alert] = []
                    for metric, value in _latest_metric_values(db, cage.id, now).items():
                        score = anomaly_detector.observe(cage.id, metric, value)
                        if score.severity == "ok":
                            continue
                        rule_name = f"anomaly_{metric}"
                        # Don't spam — keep one open alert per (cage, metric)
                        if _open_anomaly_alert(db, cage.id, rule_name):
                            continue
                        alert = Alert(
                            cage_id=cage.id,
                            severity="warning" if score.severity == "warning" else "info",
                            rule=rule_name,
                            message=f"Anomaly: {score.reason}",
                        )
                        db.add(alert)
                        new_anomalies.append(alert)
                    if new_anomalies:
                        db.commit()
                        for a in new_anomalies:
                            db.refresh(a)
                            await broadcaster.publish({
                                "type": "alert",
                                "cage_id": a.cage_id,
                                "severity": a.severity,
                                "rule": a.rule,
                                "message": a.message,
                            })
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            log.exception({"event": "aggregator.error", "error": str(exc)})

        await asyncio.sleep(settings.aggregator_tick_seconds)
