from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT
from .database import connect
from .indexer import start_paper_watch_observer
from .local_api import router as local_router
from .mcp_api import router as mcp_router
from .oauth_api import router as oauth_router


app = FastAPI(
    title="Private Literature MCP Site",
    description="Local private PDF reader plus MCP-safe search and draft tools.",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(local_router)
app.include_router(oauth_router)
app.include_router(mcp_router)

FRONTEND_DIR = PROJECT_ROOT / "frontend"
ASSETS_DIR = FRONTEND_DIR / "src"

if ASSETS_DIR.exists():
    app.mount("/src", StaticFiles(directory=ASSETS_DIR), name="src")


@app.on_event("startup")
def startup() -> None:
    connection = connect()
    connection.close()
    app.state.paper_watch_observer = start_paper_watch_observer()


@app.on_event("shutdown")
def shutdown() -> None:
    observer = getattr(app.state, "paper_watch_observer", None)
    if observer is None:
        return
    observer.stop()
    observer.join()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/{path:path}")
def spa_fallback(path: str) -> FileResponse:
    requested = FRONTEND_DIR / path
    if requested.exists() and requested.is_file():
        return FileResponse(requested)
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    from .config import settings

    uvicorn.run("backend.main:app", host=settings.app_host, port=settings.app_port, reload=False)
