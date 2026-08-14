from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cage, ScheduleWindow, User
from ..schemas import ScheduleOut, ScheduleWindowIn
from ..security import get_current_user

router = APIRouter(prefix="/api/v1/cages", tags=["schedule"])


def _ensure_cage(db: Session, cage_id: str) -> Cage:
    cage = db.query(Cage).filter(Cage.id == cage_id).one_or_none()
    if not cage:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Unknown cage."})
    return cage


def _window_to_schema(w: ScheduleWindow) -> ScheduleWindowIn:
    return ScheduleWindowIn(
        start_local=w.start_local,
        end_local=w.end_local,
        days=[int(d) for d in w.days_of_week.split(",") if d.strip()],
        active=w.active,
        timezone=w.timezone or "Europe/Brussels",
    )


@router.get("/{cage_id}/schedule", response_model=ScheduleOut)
def get_schedule(
    cage_id: str,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> ScheduleOut:
    _ensure_cage(db, cage_id)
    windows = db.query(ScheduleWindow).filter(ScheduleWindow.cage_id == cage_id).all()
    return ScheduleOut(cage_id=cage_id, windows=[_window_to_schema(w) for w in windows])


@router.put("/{cage_id}/schedule", response_model=ScheduleOut)
def put_schedule(
    cage_id: str,
    body: list[ScheduleWindowIn],
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> ScheduleOut:
    _ensure_cage(db, cage_id)
    # Validate the timezone string against the system tz database so callers
    # can't poison the schedule with a bogus value.
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        for w in body:
            try:
                ZoneInfo(w.timezone)
            except ZoneInfoNotFoundError:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "BAD_TIMEZONE", "message": f"Unknown IANA timezone: {w.timezone}"},
                )
    except ImportError:
        pass  # zoneinfo unavailable; skip validation

    db.query(ScheduleWindow).filter(ScheduleWindow.cage_id == cage_id).delete()
    for w in body:
        db.add(ScheduleWindow(
            cage_id=cage_id,
            start_local=w.start_local,
            end_local=w.end_local,
            days_of_week=",".join(str(d) for d in w.days),
            active=w.active,
            timezone=w.timezone,
        ))
    db.commit()
    return get_schedule(cage_id, db, _user)
