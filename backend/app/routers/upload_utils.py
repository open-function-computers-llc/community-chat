"""Shared file-upload helpers.

Used by the general files router and the family-avatar endpoint. Files are
stored flat under UPLOAD_DIR with a collision-safe name and served from
/uploads/<storage_name>.
"""
import io
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, UploadFile
from PIL import Image

# Single source of truth for where uploaded files live. main.py imports this
# so the write path and the /uploads serve path always agree.
UPLOAD_DIR = Path(
    os.environ.get(
        "UPLOAD_DIR",
        str(Path(__file__).resolve().parent.parent.parent / "uploads"),
    )
)

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB — general file uploads (PDFs, videos, etc.)

# Avatars are shrunk to 500x500 WebP on upload, so the *source* image can be
# much larger (a phone photo) without costing much in storage. Allow up to
# 25 MB for avatar uploads; nginx client_max_body_size must be >= this.
MAX_AVATAR_SIZE = 25 * 1024 * 1024  # 25 MB

# Families only allow images (this is an avatar). The general files router has
# its own broader allow-list; this one is image-only.
IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/avif",
}

# Extension -> MIME map used by the image proxy to set a correct Content-Type
# when the authoritative DB record isn't available (e.g. avatars / family
# photos that live in UPLOAD_DIR but are not FileModel rows).
EXT_CONTENT_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}

# Tiny inline placeholder returned (with HTTP 200) when a requested upload no
# longer exists. Returning 200 instead of 404 keeps the WAF from counting the
# request as a missing resource and avoids the browser's broken-image icon.
# A neutral "image unavailable" card is shown in its place.
BROKEN_IMAGE_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='240'>"
    "<rect width='320' height='240' fill='#e9ecef'/>"
    "<rect x='1' y='1' width='318' height='238' fill='none' stroke='#ced4da' stroke-width='2'/>"
    "<circle cx='112' cy='96' r='16' fill='#adb5bd'/>"
    "<path d='M60 188 L110 128 L150 168 L186 132 L240 188 Z' fill='#adb5bd'/>"
    "<text x='160' y='212' font-family='sans-serif' font-size='15' fill='#6c757d' "
    "text-anchor='middle'>Image unavailable</text>"
    "</svg>"
).encode("utf-8")


def public_url(filename: str) -> str:
    return f"/uploads/{filename}"


def content_type_for(filename: str, db_content_type: str | None = None) -> str:
    """Best-effort MIME type for a stored upload.

    Prefer the authoritative DB content_type; fall back to extension inference;
    last resort is octet-stream (the caller decides how to render it).
    """
    if db_content_type:
        return db_content_type
    ext = os.path.splitext(filename)[1].lower()
    return EXT_CONTENT_TYPES.get(ext, "application/octet-stream")


def _storage_name(prefix: str, ext: str = "") -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{prefix}-{timestamp}-{secrets.token_hex(6)}{ext}"


async def save_upload(
    file: UploadFile,
    *,
    prefix: str,
    allowed_content_types: set[str],
    max_size: int = MAX_UPLOAD_SIZE,
) -> tuple[str, int]:
    """Persist an uploaded file.

    Validates content type and size, writes to disk, and returns
    (storage_name, size). The caller decides how to reference it (via
    public_url) and is responsible for deleting the old file when replacing
    (see delete_file).
    """
    if file.content_type not in allowed_content_types:
        raise HTTPException(status_code=415, detail="Unsupported file type")

    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()
    storage_name = _storage_name(prefix, ext)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / storage_name).write_bytes(content)
    return storage_name, len(content)


# Avatars are only ever rendered as small circular previews, so we shrink
# them aggressively on upload: center-crop to a square, resize to 500x500,
# and re-encode as WebP. This keeps disk + transfer size tiny.
AVATAR_SIZE = 500
AVATAR_WEBP_QUALITY = 85


def process_avatar(content: bytes, *, prefix: str) -> tuple[str, int]:
    """Crop/resize an uploaded avatar image and store it as a 500x500 WebP.

    Center-crops to a square, resizes to AVATAR_SIZE x AVATAR_SIZE, and
    encodes as WebP. Returns (storage_name, size). Raises 415 for a
    non-decodable image and 413 for an oversized upload.
    """
    if len(content) > MAX_AVATAR_SIZE:
        raise HTTPException(
            status_code=413, detail="Image too large (max 25 MB for avatars)"
        )

    try:
        img = Image.open(io.BytesIO(content))
        img.load()
        # Normalize palette/alpha images so the square crop + resize work
        # cleanly (e.g. palette PNGs, animated GIFs -> first frame).
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad image
        raise HTTPException(status_code=415, detail="Could not read that image") from exc

    # Center-crop to a square.
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))

    # Resize to the target square (high-quality downscale).
    img = img.resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)

    # Drop the alpha channel for a clean WebP (solid backgrounds are filled
    # with black only if the source had transparency; most avatars are opaque).
    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        img = background

    buffer = io.BytesIO()
    img.save(buffer, format="WEBP", quality=AVATAR_WEBP_QUALITY)
    data = buffer.getvalue()

    storage_name = _storage_name(prefix, ".webp")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / storage_name).write_bytes(data)
    return storage_name, len(data)


def delete_file(public_file_url: str | None) -> None:
    """Delete a previously uploaded file from disk (best-effort)."""
    if not public_file_url:
        return
    filename = public_file_url.rsplit("/", 1)[-1]
    if not filename:
        return
    path = (UPLOAD_DIR / filename).resolve()
    if UPLOAD_DIR.resolve() not in path.parents:
        return
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
