from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cage, User
from ..schemas import CageIn, CageOut, CagePatch
from ..security import get_current_user, require_admin

router = APIRouter(prefix="/api/v1/cages", tags=["cages"])


@router.get("", response_model=list[CageOut])
def list_cages(
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> list[Cage]:
    return db.query(Cage).order_by(Cage.id).all()


@router.post("", response_model=CageOut, status_code=status.HTTP_201_CREATED)
def create_cage(
    body: CageIn,
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
) -> Cage:
    cage_id = body.id or f"cage-{uuid4().hex[:8]}"
    if db.query(Cage).filter(Cage.id == cage_id).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CONFLICT", "message": "Cage id already exists."},
        )
    cage = Cage(
        id=cage_id,
        name=body.name,
        location=body.location,
        animal_strain=body.animal_strain,
        animal_age_days=body.animal_age_days,
    )
    db.add(cage)
    db.commit()
    db.refresh(cage)
    return cage


@router.get("/{cage_id}", response_model=CageOut)
def get_cage(
    cage_id: str,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> Cage:
    cage = db.query(Cage).filter(Cage.id == cage_id).one_or_none()
    if not cage:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Unknown cage."})
    return cage


@router.patch("/{cage_id}", response_model=CageOut)
def patch_cage(
    cage_id: str,
    patch: CagePatch,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> Cage:
    cage = db.query(Cage).filter(Cage.id == cage_id).one_or_none()
    if not cage:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Unknown cage."})
    for field, value in patch.model_dump(exclude_unset=True).items():
        setattr(cage, field, value)
    db.commit()
    db.refresh(cage)
    return cage


@router.delete("/{cage_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cage(
    cage_id: str,
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
) -> None:
    cage = db.query(Cage).filter(Cage.id == cage_id).one_or_none()
    if not cage:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Unknown cage."})
    db.delete(cage)
    db.commit()
