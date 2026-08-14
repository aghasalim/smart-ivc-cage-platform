import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cage, Reading, User
from ..schemas import IngestIn, IngestOut
from ..security import get_current_user
from ..services.broadcaster import broadcaster
from ..services.rules import evaluate_rules


def _utc_iso(dt: datetime) -> str:
    """Force a UTC-aware ISO string so the browser doesn't interpret it as local time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")

router = APIRouter(prefix="/api/v1", tags=["ingest"])

log = logging.getLogger(__name__)


@router.post("/ingest", response_model=IngestOut, status_code=status.HTTP_202_ACCEPTED)
async def ingest(
    payload: IngestIn,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> IngestOut:
    cage = db.query(Cage).filter(Cage.id == payload.cage_id).one_or_none()
    if not cage:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"Unknown cage {payload.cage_id}."},
        )

    stored = 0
    sensors_dict = payload.sensors.model_dump(exclude_none=True)
    for sensor_name, sensor_payload in sensors_dict.items():
        reading = Reading(
            cage_id=cage.id,
            ts=payload.ts,
            sensor=sensor_name,
            seq=payload.seq,
            payload=sensor_payload,
        )
        try:
            with db.begin_nested():
                db.add(reading)
                db.flush()
        except IntegrityError:
            # Savepoint is rolled back; the outer transaction and all prior readings survive.
            log.info({"event": "ingest.dup", "cage": cage.id, "sensor": sensor_name, "seq": payload.seq})
            continue
        stored += 1
        await broadcaster.publish({
            "type": "reading",
            "cage_id": cage.id,
            "sensor": sensor_name,
            "ts": _utc_iso(payload.ts),
            "payload": sensor_payload,
        })

    cage.last_seen = datetime.now(tz=timezone.utc)
    db.commit()

    new_alerts = evaluate_rules(db, cage.id, sensors_dict, payload.ts)
    for alert in new_alerts:
        db.refresh(alert)
        await broadcaster.publish({
            "type": "alert",
            "cage_id": cage.id,
            "alert_id": alert.id,
            "severity": alert.severity,
            "rule": alert.rule,
            "message": alert.message,
            "ts": _utc_iso(alert.ts),
        })

    return IngestOut(received=True, stored=stored)
