import asyncio

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import get_db, iso_utc
from ..models import Family, User
from ..core.security import get_current_user, require_admin
from .upload_utils import delete_file, IMAGE_CONTENT_TYPES, process_avatar, public_url

router = APIRouter()


class FamilyMember(BaseModel):
    id: int
    display_name: str
    handle: str
    avatar_url: str | None
    bio: str | None
    phone: str | None
    email: str | None


class FamilyOut(BaseModel):
    id: int
    name: str
    description: str | None
    avatar_url: str | None
    member_count: int
    members: list[FamilyMember]
    created_at: str


class FamilyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=280)


class FamilyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=280)


def serialize(db, family: Family) -> FamilyOut:
    members = (
        db.query(User)
        .filter(User.family_id == family.id, User.is_active == True)
        .order_by(User.display_name)
        .all()
    )
    return FamilyOut(
        id=family.id,
        name=family.name,
        description=family.description,
        avatar_url=family.avatar_url,
        member_count=len(members),
        members=[
            FamilyMember(
                id=u.id,
                display_name=u.display_name or u.handle,
                handle=u.handle,
                avatar_url=u.avatar_url,
                bio=u.bio,
                phone=u.phone,
                email=u.email,
            )
            for u in members
        ],
        created_at=iso_utc(family.created_at),
    )


@router.get("", response_model=list[FamilyOut])
async def list_families(
    user: User = Depends(get_current_user), db=Depends(get_db)
):
    rows = db.execute(select(Family).order_by(Family.name)).scalars().all()
    return [serialize(db, f) for f in rows]


@router.post("", response_model=FamilyOut, status_code=201)
async def create_family(
    req: FamilyCreateRequest,
    user: User = Depends(require_admin),
    db=Depends(get_db),
):
    name = req.name.strip()
    existing = db.execute(select(Family).where(Family.name == name)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="A family with that name already exists")

    family = Family(name=name, description=req.description, created_by=user.id)
    db.add(family)
    db.commit()
    db.refresh(family)
    return serialize(db, family)


@router.put("/{family_id}", response_model=FamilyOut)
async def update_family(
    family_id: int,
    req: FamilyUpdateRequest,
    user: User = Depends(require_admin),
    db=Depends(get_db),
):
    family = db.get(Family, family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="Family not found")

    if req.name is not None:
        name = req.name.strip()
        clash = db.execute(
            select(Family).where(Family.name == name, Family.id != family_id)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status_code=409, detail="A family with that name already exists")
        family.name = name
    if req.description is not None:
        family.description = req.description
    db.commit()
    db.refresh(family)
    return serialize(db, family)


@router.delete("/{family_id}")
async def delete_family(
    family_id: int,
    user: User = Depends(require_admin),
    db=Depends(get_db),
):
    family = db.get(Family, family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="Family not found")

    members = db.query(User).filter(User.family_id == family_id).count()
    if members > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete: {members} member(s) are still in this family",
        )

    # Unassign any pending invites pointing at this family.
    from ..models import Invite

    for inv in db.execute(select(Invite).where(Invite.family_id == family_id)).scalars().all():
        inv.family_id = None

    db.delete(family)
    db.commit()
    return {"ok": True}


@router.post("/{family_id}/avatar", response_model=FamilyOut)
async def upload_family_avatar(
    family_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Upload (or replace) the family's avatar image.

    Any authenticated member may set it. Only one image is kept: the
    previous file is deleted from disk so no revisions are retained.
    """
    family = db.get(Family, family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="Family not found")

    if file.content_type not in IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported file type")

    content = await file.read()
    # PIL work is CPU-bound; run it off the event loop.
    storage_name, _size = await asyncio.to_thread(
        process_avatar, content, prefix=f"family{family_id}"
    )
    # Replace the existing avatar and remove its file (no revision history).
    delete_file(family.avatar_url)
    family.avatar_url = public_url(storage_name)
    db.commit()
    db.refresh(family)
    return serialize(db, family)
