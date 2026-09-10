from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy import select

from ..db import get_db, iso_utc
from ..models import File as FileModel, User
from ..core.security import get_current_user
from .upload_utils import (
    MAX_UPLOAD_SIZE,
    _storage_name,
    delete_file as delete_uploaded_file,
    public_url,
    process_chat_image,
    UPLOAD_DIR,
)

router = APIRouter()

# Only images and PDFs are shared in chat. Images are re-encoded to WebP on
# upload (see process_chat_image); PDFs are stored as-is.
IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/avif",
}
PDF_CONTENT_TYPES = {"application/pdf"}
ALLOWED_CONTENT_TYPES = IMAGE_CONTENT_TYPES | PDF_CONTENT_TYPES


def _store_raw(content: bytes, filename: str | None, *, prefix: str) -> tuple[str, int]:
    """Persist a non-image file (PDF) verbatim. Returns (storage_name, size)."""
    ext = ""
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
    storage_name = _storage_name(prefix, ext or ".pdf")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / storage_name).write_bytes(content)
    return storage_name, len(content)


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
    content = await file.read()
    if file.content_type in IMAGE_CONTENT_TYPES:
        # Re-encode as a resized WebP; the stored content_type becomes
        # image/webp regardless of the source format.
        storage_name, size = process_chat_image(content, prefix=str(user.id))
        stored_content_type = "image/webp"
    elif file.content_type in PDF_CONTENT_TYPES:
        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
        storage_name, size = _store_raw(content, file.filename, prefix=str(user.id))
        stored_content_type = "application/pdf"
    else:
        raise HTTPException(status_code=415, detail="Unsupported file type")

    record = FileModel(
        owner_id=user.id,
        filename=file.filename or "file",
        storage_name=storage_name,
        content_type=stored_content_type,
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
