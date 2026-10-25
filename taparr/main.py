"""
Taparr FastAPI Application Entrypoint & CLI Handler.
"""

import argparse
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from taparr import __version__, TAPE_FORMAT_VERSION
from taparr.config import load_bootstrap_config
from taparr.database import init_db, SessionLocal
from taparr.api.router import router as api_router
from taparr.services.catalog_backup import CatalogBackupExporter

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Taparr",
    description="LTO-1..LTO-10 (LA) Tape Archival & Catalog Manager for Sonarr, Radarr, and Plex on HP MSL4048",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(api_router)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def serve_spa_index():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "taparr_version": __version__,
        "tape_format_version": TAPE_FORMAT_VERSION,
    }


def cli() -> None:
    parser = argparse.ArgumentParser(description="Taparr Server & Maintenance CLI")
    parser.add_argument("--init-db", action="store_true", help="Initialize database schema and seed pools/drives")
    parser.add_argument("--export-backup", action="store_true", help="Export a Disaster Recovery Catalog Backup bundle")
    parser.add_argument("--host", type=str, default=None, help="Bind host override")
    parser.add_argument("--port", type=int, default=None, help="Bind port override")
    args = parser.parse_args()

    if args.init_db:
        init_db()
        print(f"[Taparr v{__version__}] Database initialized.")
        return

    if args.export_backup:
        init_db()
        with SessionLocal() as db:
            res = CatalogBackupExporter(db).export_backup_bundle(reason="cli")
            print(f"[Taparr v{__version__}] Exported backup bundle: {res['bundle_name']}")
        return

    cfg = load_bootstrap_config()
    host = args.host or cfg["server"]["host"]
    port = args.port or cfg["server"]["port"]
    uvicorn.run("taparr.main:app", host=host, port=port, log_level=cfg["server"]["log_level"])


if __name__ == "__main__":
    cli()
