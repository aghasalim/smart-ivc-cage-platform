from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Alert, User
from ..schemas import AlertOut
from ..security import get_current_user

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
def list_alerts(
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
    cage_id: Optional[str] = None,
    acknowledged: Optional[bool] = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[Alert]:
    q = db.query(Alert)
    if cage_id:
        q = q.filter(Alert.cage_id == cage_id)
    if acknowledged is True:
        q = q.filter(Alert.acknowledged_at.is_not(None))
    elif acknowledged is False:
        q = q.filter(Alert.acknowledged_at.is_(None))
    return q.order_by(Alert.ts.desc()).limit(limit).all()


@router.post("/{alert_id}/ack", response_model=AlertOut)
def ack(
    alert_id: int,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> Alert:
    alert = db.query(Alert).filter(Alert.id == alert_id).one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Alert not found."})
    alert.acknowledged_at = datetime.now(tz=timezone.utc)
    db.commit()
    db.refresh(alert)
    return alert
