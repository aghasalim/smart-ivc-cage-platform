import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Cage, ScheduleWindow, User
from .security import hash_password

log = logging.getLogger(__name__)
settings = get_settings()

DEMO_CAGES = [
    {
        "id": "cage-001",
        "name": "Cohort A — male B6 #1",
        "location": "Rack 3, shelf 2",
        "animal_strain": "C57BL/6J",
        "animal_age_days": 84,
    },
    {
        "id": "cage-002",
        "name": "Cohort A — male B6 #2",
        "location": "Rack 3, shelf 3",
        "animal_strain": "C57BL/6J",
        "animal_age_days": 84,
    },
    {
        "id": "cage-003",
        "name": "Cohort B — female B6 #1",
        "location": "Rack 4, shelf 1",
        "animal_strain": "C57BL/6J",
        "animal_age_days": 90,
    },
]


def seed_initial_data(db: Session) -> None:
    if not db.query(User).first():
        db.add(User(
            username=settings.default_admin_user,
            password_hash=hash_password(settings.default_admin_password),
            role="admin",
        ))
        db.add(User(
            username=settings.default_researcher_user,
            password_hash=hash_password(settings.default_researcher_password),
            role="researcher",
        ))
        log.info({"event": "seed.users_created"})

    if not db.query(Cage).first():
        for spec in DEMO_CAGES:
            cage = Cage(**spec, created_at=datetime.now(tz=timezone.utc))
            db.add(cage)
            db.flush()
            db.add(ScheduleWindow(
                cage_id=cage.id,
                start_local="23:00",
                end_local="00:30",
                days_of_week="0,1,2,3,4,5,6",
                active=True,
            ))
        log.info({"event": "seed.cages_created", "count": len(DEMO_CAGES)})

    db.commit()
