import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .db import create_all
from .seed import seed_admin
from .routers import auth, dms, email, families, files, invites, messages, push
from .routers.upload_utils import UPLOAD_DIR
from .ws import hub, ws_endpoint

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("chat")

def _resolve_frontend_dir() -> Path:
    env = os.environ.get("FRONTEND_DIR")
    if env:
        return Path(env)
    # Docker image layout
    docker_dir = Path(__file__).resolve().parent.parent / "frontend_dist"
    if docker_dir.exists():
        return docker_dir
    # Local dev layout
    return Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


FRONTEND_DIR = _resolve_frontend_dir()


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_admin()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    await hub.start()
    logger.info("Server ready")
    yield
    await hub.stop()


app = FastAPI(title="Community Chat", version="1.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(invites.router, prefix="/api/invites", tags=["invites"])
app.include_router(families.router, prefix="/api/families", tags=["families"])
app.include_router(messages.router, prefix="/api/messages", tags=["messages"])
app.include_router(dms.router, prefix="/api/dms", tags=["dms"])
app.include_router(files.router, prefix="/api/files", tags=["files"])
app.include_router(push.router, prefix="/api/push", tags=["push"])
app.include_router(email.router, prefix="/api/email", tags=["email"])


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = ""):
    await ws_endpoint(ws, token)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/uploads/{filename}")
async def serve_upload(filename: str):
    path = (UPLOAD_DIR / filename).resolve()
    if UPLOAD_DIR.resolve() not in path.parents:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


class ImmutableFiles(StaticFiles):
    """StaticFiles that serves hashed build assets with a long, immutable
    cache. Vite emits content-hashed filenames (e.g. index-<hash>.js), so a
    URL never changes — safe to cache forever."""

    def file_response(self, *args, **kwargs) -> Response:  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# Headers for the app shell (index.html). It must never be cached: it is the
# one file that references the hashed bundles, so serving a stale copy would
# pin the user to an old JS/CSS build. `no-cache` forces a revalidation.
_SHELL_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def _shell_response() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html", headers=_SHELL_CACHE_HEADERS)


if FRONTEND_DIR.exists():
    app.mount("/assets", ImmutableFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith(("api/", "ws")):
            return JSONResponse({"error": "not found"}, status_code=404)
        index = FRONTEND_DIR / "index.html"
        if full_path:
            candidate = (FRONTEND_DIR / full_path).resolve()
            if FRONTEND_DIR.resolve() in candidate.parents and candidate.is_file():
                # Static files the SPA references directly (e.g. manifest,
                # icons) — cacheable, but not forever.
                return FileResponse(
                    candidate,
                    headers={"Cache-Control": "public, max-age=3600"},
                )
        return _shell_response()
