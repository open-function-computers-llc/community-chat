from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy import select

from ..db import get_db, iso_utc
from ..models import File as FileModel, User
from ..core.security import get_current_user
from .upload_utils import delete_file as delete_uploaded_file, public_url, save_upload

router = APIRouter()

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "image/avif",
    "video/mp4",
    "video/webm",
    "text/plain",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@router.get("/me")
async def my_files(user: User = Depends(get_current_user), db=Depends(get_db)):
    rows = db.execute(select(FileModel).where(FileModel.owner_id == user.id).order_by(FileModel.created_at.desc())).scalars().all()
    return [
        {
            "id": f.id,
            "filename": f.filename,
            "content_type": f.content_type,
            "size": f.size,
            "url": public_url(f.storage_name),
            "created_at": iso_utc(f.created_at),
        }
        for f in rows
    ]


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    storage_name, size = await save_upload(
        file,
        prefix=str(user.id),
        allowed_content_types=ALLOWED_CONTENT_TYPES,
    )

    record = FileModel(
        owner_id=user.id,
        filename=file.filename or "file",
        storage_name=storage_name,
        content_type=file.content_type,
        size=size,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "filename": record.filename,
        "content_type": record.content_type,
        "size": record.size,
        "url": public_url(storage_name),
    }


@router.delete("/{file_id}")
async def delete_file(
    file_id: int,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    record = db.get(FileModel, file_id)
    if record is None or record.owner_id != user.id:
        raise HTTPException(status_code=404, detail="File not found")
    delete_uploaded_file(public_url(record.storage_name))
    db.delete(record)
    db.commit()
    return {"ok": True}
