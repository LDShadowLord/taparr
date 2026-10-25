"""
Unified REST API router for Taparr (`/api/v1/...`).
Exposes all endpoints for the WebUI:
  - Dashboard & LTO-1..LTO-10 (LA) compatibility matrix
  - HP MSL4048 48-Slot Library, Drives, Mailslot & Power Manager
  - Tape Pools (including 'Unassigned' & protected 'Used By Other') & Cartridge Management
  - Media Browser (Sonarr, Radarr, Unmanaged), Queueing, Archive & Single-Pass Restore
  - Tape Cloning, Dual-Drive Compaction, and Close/Seal Tape
  - Catalog Search & Disaster Recovery Bundle Exporter
  - Seamless In-WebUI Updater & CHANGELOG.md Viewer
  - All WebUI Settings & Incoming Plex Webhook
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from taparr import (
    __version__,
    TAPE_FORMAT_VERSION,
    LTO_MEDIA_SPECS,
    LTO_DRIVE_COMPATIBILITY,
    detect_media_generation_from_barcode,
    get_media_spec,
    check_drive_media_compatibility,
)
from taparr.config import load_bootstrap_config
from taparr.database import get_db, get_setting, update_setting, DEFAULT_SETTINGS
from taparr.models import (
    ArrInstance,
    TapePool,
    TapeDrive,
    Tape,
    MediaItem,
    TapeFilemark,
    ArchivedFile,
    TapeJob,
)
from taparr.services.hardware import (
    MSL4048HardwareManager,
    ForeignTapeProtectedError,
    DriveMediaIncompatibleError,
)
from taparr.services.scratch import ScratchPipelineManager
from taparr.services.power import LibraryPowerManager
from taparr.services.arr_client import ArrService
from taparr.services.plex_client import PlexService
from taparr.services.scheduler import TapeJobScheduler
from taparr.services.catalog_backup import CatalogBackupExporter
from taparr.services.updater import UpdateManager

router = APIRouter(prefix="/api/v1")


# ==============================================================================
# Pydantic Request Schemas
# ==============================================================================


class PoolCreateRequest(BaseModel):
    name: str
    description: str = ""
    color: str = "#3b82f6"
    barcode_regex: str = ""
    close_threshold_gb: int = 35
    verify_after_write: bool = False


class TapeCreateRequest(BaseModel):
    barcode: str
    pool_id: Optional[int] = None
    media_code: Optional[str] = None
    slot_number: Optional[int] = None


class AssignPoolRequest(BaseModel):
    pool_id: int


class CloneTapeRequest(BaseModel):
    target_barcode: str


class CompactTapeRequest(BaseModel):
    target_barcode: Optional[str] = None


class VaultTapeRequest(BaseModel):
    vault_note: str = "Offsite Vault"


class ReturnVaultRequest(BaseModel):
    slot_number: Optional[int] = None


class LoadDriveRequest(BaseModel):
    barcode: str
    drive_index: int = 0


class UnloadDriveRequest(BaseModel):
    drive_index: int = 0
    target_slot: Optional[int] = None


class DriveConfigUpdateRequest(BaseModel):
    name: Optional[str] = None
    generation: Optional[str] = None
    device_path: Optional[str] = None
    enabled: Optional[bool] = None


class UnmanagedFolderRequest(BaseModel):
    folder_path: str
    title: Optional[str] = None
    media_category: str = "Unmanaged"
    target_pool_id: Optional[int] = None
    year: Optional[int] = None


class QueueArchiveRequest(BaseModel):
    media_item_ids: List[int]
    target_pool_id: Optional[int] = None
    post_archive_mode: Optional[str] = None


class QueueRestoreRequest(BaseModel):
    media_item_ids: List[int]


class RunArchiveBatchRequest(BaseModel):
    pool_id: Optional[int] = None
    post_archive_mode: Optional[str] = None


class RunRestoreBatchRequest(BaseModel):
    remonitor_in_arr: bool = True


class ArrInstanceRequest(BaseModel):
    name: str
    arr_type: str = Field(..., pattern="^(sonarr|radarr)$")
    url: str
    api_key: str
    enabled: bool = True
    default_pool_id: Optional[int] = None
    remote_path_prefix: str = ""
    local_path_prefix: str = ""
    to_tape_tag: str = "to-tape"
    on_tape_tag: str = "on-tape"
    auto_unmonitor: bool = True


class TriggerUpdateRequest(BaseModel):
    dry_run: bool = False
    force: bool = False


class MaintenanceLockRequest(BaseModel):
    locked: bool


# ==============================================================================
# 1. Dashboard & LTO Generation Matrix
# ==============================================================================


@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)) -> Dict[str, Any]:
    hw = MSL4048HardwareManager(db)
    scratch = ScratchPipelineManager(db)
    power = LibraryPowerManager(db)

    tapes = db.scalars(select(Tape)).all()
    pools = db.scalars(select(TapePool)).all()
    media = db.scalars(select(MediaItem)).all()
    jobs = db.scalars(select(TapeJob).order_by(TapeJob.id.desc()).limit(10)).all()

    foreign_pool_ids = {p.id for p in pools if p.is_protected_foreign}
    managed_tapes = [t for t in tapes if t.pool_id not in foreign_pool_ids and t.media_code != "CU"]

    total_tape_capacity = sum(t.native_capacity_bytes for t in managed_tapes)
    total_tape_used = sum(t.used_bytes for t in managed_tapes)
    total_tape_dead = sum(t.dead_bytes for t in managed_tapes)

    on_tape_items = [m for m in media if m.status in ("ON_TAPE_ONLY", "ON_TAPE_STUBBED", "ON_DISK_AND_TAPE")]
    queued_archive = [m for m in media if m.status == "QUEUED_ARCHIVE"]
    queued_restore = [m for m in media if m.status == "QUEUED_RESTORE"]

    return {
        "taparr_version": __version__,
        "tape_format_version": TAPE_FORMAT_VERSION,
        "summary": {
            "total_tapes": len(tapes),
            "managed_tapes": len(managed_tapes),
            "foreign_locked_tapes": sum(1 for t in tapes if t.pool_id in foreign_pool_ids or t.status == "FOREIGN_LOCKED"),
            "total_tape_capacity_bytes": total_tape_capacity,
            "total_tape_used_bytes": total_tape_used,
            "total_tape_dead_bytes": total_tape_dead,
            "media_total_count": len(media),
            "media_archived_count": len(on_tape_items),
            "media_archived_bytes": sum(m.size_bytes for m in on_tape_items),
            "queued_archive_count": len(queued_archive),
            "queued_archive_bytes": sum(m.size_bytes for m in queued_archive),
            "queued_restore_count": len(queued_restore),
        },
        "library": hw.get_library_state(),
        "scratch": scratch.get_scratch_telemetry(),
        "power": power.get_status(),
        "recent_jobs": [_serialize_job(j) for j in jobs],
        "lto_matrix": {
            "media_specs": LTO_MEDIA_SPECS,
            "drive_compatibility": LTO_DRIVE_COMPATIBILITY,
        },
    }


@router.get("/lto/compatibility")
def check_compatibility_endpoint(drive_generation: str, media_code: str) -> Dict[str, Any]:
    return check_drive_media_compatibility(drive_generation, media_code)


# ==============================================================================
# 2. HP MSL4048 Library, Drives & Power Control
# ==============================================================================


@router.get("/library/state")
def get_library_state(db: Session = Depends(get_db)) -> Dict[str, Any]:
    hw = MSL4048HardwareManager(db)
    return hw.get_library_state()


@router.post("/library/inventory")
def trigger_inventory(db: Session = Depends(get_db)) -> Dict[str, Any]:
    hw = MSL4048HardwareManager(db)
    return hw.run_inventory()


@router.get("/library/discover-devices")
def discover_devices(db: Session = Depends(get_db)) -> Dict[str, Any]:
    hw = MSL4048HardwareManager(db)
    return hw.discover_persistent_devices()


@router.post("/library/load")
def load_tape(req: LoadDriveRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    hw = MSL4048HardwareManager(db)
    tape = db.scalar(select(Tape).where(Tape.barcode == req.barcode.upper()))
    drive = db.scalar(select(TapeDrive).where(TapeDrive.drive_index == req.drive_index))
    if not tape or not drive:
        raise HTTPException(status_code=404, detail="Tape or drive not found.")
    try:
        return hw.load_tape_into_drive(tape, drive, require_write=False)
    except (ForeignTapeProtectedError, DriveMediaIncompatibleError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/library/unload")
def unload_tape(req: UnloadDriveRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    hw = MSL4048HardwareManager(db)
    drive = db.scalar(select(TapeDrive).where(TapeDrive.drive_index == req.drive_index))
    if not drive:
        raise HTTPException(status_code=404, detail="Drive not found.")
    return hw.unload_drive(drive, target_slot=req.target_slot)


@router.put("/drives/{drive_index}")
def update_drive_config(
    drive_index: int, req: DriveConfigUpdateRequest, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    drive = db.scalar(select(TapeDrive).where(TapeDrive.drive_index == drive_index))
    if not drive:
        raise HTTPException(status_code=404, detail="Drive not found.")
    if req.name is not None:
        drive.name = req.name
    if req.generation is not None:
        gen = req.generation.strip().upper()
        if gen not in LTO_DRIVE_COMPATIBILITY:
            raise HTTPException(status_code=400, detail=f"Unsupported drive generation: {gen}")
        drive.generation = gen
    if req.device_path is not None:
        drive.device_path = req.device_path
    if req.enabled is not None:
        drive.enabled = req.enabled
    db.commit()
    db.refresh(drive)
    return {
        "drive_index": drive.drive_index,
        "name": drive.name,
        "generation": drive.generation,
        "device_path": drive.device_path,
        "enabled": drive.enabled,
    }


@router.post("/library/power/on")
def power_on_library(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return LibraryPowerManager(db).ensure_library_online()


@router.post("/library/power/off")
def power_off_library(force: bool = False, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return LibraryPowerManager(db).power_off_library(force=force)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/library/power/maintenance")
def set_maintenance_lock(req: MaintenanceLockRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return LibraryPowerManager(db).set_maintenance_lock(req.locked)


# ==============================================================================
# 3. Tape Pools & Tapes (Including 'Unassigned' & Protected 'Used By Other')
# ==============================================================================


@router.get("/pools")
def list_pools(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    pools = db.scalars(select(TapePool).order_by(TapePool.id)).all()
    tapes = db.scalars(select(Tape)).all()
    result = []
    for p in pools:
        p_tapes = [t for t in tapes if t.pool_id == p.id]
        result.append(
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "color": p.color,
                "is_system": p.is_system,
                "is_protected_foreign": p.is_protected_foreign,
                "barcode_regex": p.barcode_regex,
                "close_threshold_gb": p.close_threshold_gb,
                "verify_after_write": p.verify_after_write,
                "tape_count": len(p_tapes),
                "total_capacity_bytes": sum(t.native_capacity_bytes for t in p_tapes),
                "used_bytes": sum(t.used_bytes for t in p_tapes),
                "dead_bytes": sum(t.dead_bytes for t in p_tapes),
            }
        )
    return result


@router.post("/pools")
def create_pool(req: PoolCreateRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    existing = db.scalar(select(TapePool).where(TapePool.name == req.name.strip()))
    if existing:
        raise HTTPException(status_code=400, detail=f"Pool '{req.name}' already exists.")
    pool = TapePool(
        name=req.name.strip(),
        description=req.description,
        color=req.color,
        is_system=False,
        is_protected_foreign=False,
        barcode_regex=req.barcode_regex,
        close_threshold_gb=req.close_threshold_gb,
        verify_after_write=req.verify_after_write,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)
    return {"id": pool.id, "name": pool.name, "color": pool.color}


@router.put("/pools/{pool_id}")
def update_pool(pool_id: int, req: PoolCreateRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    pool = db.get(TapePool, pool_id)
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found.")
    if not pool.is_system:
        pool.name = req.name.strip()
    pool.description = req.description
    pool.color = req.color
    pool.barcode_regex = req.barcode_regex
    pool.close_threshold_gb = req.close_threshold_gb
    pool.verify_after_write = req.verify_after_write
    db.commit()
    return {"id": pool.id, "name": pool.name, "barcode_regex": pool.barcode_regex}


@router.delete("/pools/{pool_id}")
def delete_pool(pool_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    pool = db.get(TapePool, pool_id)
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found.")
    if pool.is_system:
        raise HTTPException(status_code=400, detail=f"System pool '{pool.name}' cannot be deleted.")
    unassigned = db.scalar(select(TapePool).where(TapePool.name == "Unassigned"))
    for t in db.scalars(select(Tape).where(Tape.pool_id == pool.id)).all():
        t.pool_id = unassigned.id
    db.delete(pool)
    db.commit()
    return {"status": "deleted", "id": pool_id}


@router.get("/tapes")
def list_tapes(pool_id: Optional[int] = None, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    pools = {p.id: p for p in db.scalars(select(TapePool)).all()}
    stmt = select(Tape).order_by(Tape.slot_number.is_(None), Tape.slot_number, Tape.barcode)
    if pool_id is not None:
        stmt = stmt.where(Tape.pool_id == pool_id)
    tapes = db.scalars(stmt).all()
    return [_serialize_tape(t, pools.get(t.pool_id)) for t in tapes]


@router.post("/tapes")
def register_tape(req: TapeCreateRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    barcode = req.barcode.strip().upper()
    if db.scalar(select(Tape).where(Tape.barcode == barcode)):
        raise HTTPException(status_code=400, detail=f"Tape {barcode} already exists.")

    m_code = req.media_code.strip().upper() if req.media_code else detect_media_generation_from_barcode(barcode)
    spec = get_media_spec(m_code)

    unassigned = db.scalar(select(TapePool).where(TapePool.name == "Unassigned"))
    pool_id = req.pool_id or unassigned.id
    pool = db.get(TapePool, pool_id)

    status = "CLEANING" if spec["is_cleaning"] else "BLANK"
    if pool and pool.is_protected_foreign:
        status = "FOREIGN_LOCKED"

    tape = Tape(
        barcode=barcode,
        uuid=str(uuid.uuid4()),
        media_code=spec["code"],
        generation_label=spec["generation"],
        pool_id=pool_id,
        status=status,
        location_type="SLOT" if req.slot_number else "VAULT",
        slot_number=req.slot_number,
        native_capacity_bytes=spec["native_capacity_bytes"],
    )
    db.add(tape)
    db.commit()
    db.refresh(tape)
    return _serialize_tape(tape, pool)


@router.put("/tapes/{barcode}/assign-pool")
def assign_tape_pool(barcode: str, req: AssignPoolRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode.upper()))
    pool = db.get(TapePool, req.pool_id)
    if not tape or not pool:
        raise HTTPException(status_code=404, detail="Tape or pool not found.")

    tape.pool_id = pool.id
    if pool.is_protected_foreign:
        tape.status = "FOREIGN_LOCKED"
    elif tape.status == "FOREIGN_LOCKED":
        tape.status = "ACTIVE" if tape.used_bytes > 0 else "BLANK"

    db.commit()
    return _serialize_tape(tape, pool)


@router.post("/tapes/{barcode}/close")
def close_seal_tape(barcode: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Writes the final self-describing JSON/CSV manifest at the end of the tape
    and seals/closes the cartridge (`status = "CLOSED"`) so no further writes happen.
    """
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode.upper()))
    if not tape:
        raise HTTPException(status_code=404, detail="Tape not found.")
    hw = MSL4048HardwareManager(db)
    try:
        manifest_fm = hw.write_trailing_manifest(tape, close_tape=True)
    except ForeignTapeProtectedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    pool = db.get(TapePool, tape.pool_id)
    return {
        "status": "closed",
        "tape": _serialize_tape(tape, pool),
        "manifest_filemark": manifest_fm.filemark_number,
    }


@router.get("/tapes/{barcode}/header")
def inspect_tape_fm0_header(barcode: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode.upper()))
    if not tape:
        raise HTTPException(status_code=404, detail="Tape not found.")
    hw = MSL4048HardwareManager(db)
    try:
        return hw.read_tape_fm0_header(tape)
    except (ForeignTapeProtectedError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/tapes/{barcode}/filemarks")
def list_tape_filemarks(barcode: str, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    fms = db.scalars(
        select(TapeFilemark)
        .where(TapeFilemark.tape_barcode == barcode.upper())
        .order_by(TapeFilemark.filemark_number)
    ).all()
    return [
        {
            "id": fm.id,
            "filemark_number": fm.filemark_number,
            "block_offset": fm.block_offset,
            "record_type": fm.record_type,
            "title": fm.title,
            "size_bytes": fm.size_bytes,
            "file_count": fm.file_count,
            "archive_sha256": fm.archive_sha256,
            "taparr_version": fm.taparr_version,
            "tape_format_version": fm.tape_format_version,
            "is_superseded": fm.is_superseded,
            "created_at": fm.created_at.isoformat() if fm.created_at else None,
        }
        for fm in fms
    ]


@router.post("/tapes/{barcode}/clone")
def clone_tape_endpoint(barcode: str, req: CloneTapeRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    sched = TapeJobScheduler(db)
    try:
        return sched.clone_tape(barcode.upper(), req.target_barcode.upper())
    except (ForeignTapeProtectedError, DriveMediaIncompatibleError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/tapes/{barcode}/compact")
def compact_tape_endpoint(barcode: str, req: CompactTapeRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    sched = TapeJobScheduler(db)
    try:
        return sched.compact_tape(
            barcode.upper(), req.target_barcode.upper() if req.target_barcode else None
        )
    except (ForeignTapeProtectedError, DriveMediaIncompatibleError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/tapes/{barcode}/mailslot")
def eject_to_mailslot_endpoint(barcode: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode.upper()))
    if not tape:
        raise HTTPException(status_code=404, detail="Tape not found.")
    try:
        return MSL4048HardwareManager(db).eject_tape_to_mailslot(tape)
    except (ForeignTapeProtectedError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/tapes/{barcode}/vault")
def vault_tape_endpoint(barcode: str, req: VaultTapeRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode.upper()))
    if not tape:
        raise HTTPException(status_code=404, detail="Tape not found.")
    try:
        return MSL4048HardwareManager(db).move_tape_to_vault(tape, req.vault_note)
    except (ForeignTapeProtectedError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/tapes/{barcode}/return-vault")
def return_vault_endpoint(barcode: str, req: ReturnVaultRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode.upper()))
    if not tape:
        raise HTTPException(status_code=404, detail="Tape not found.")
    try:
        return MSL4048HardwareManager(db).return_tape_from_vault(tape, req.slot_number)
    except (ForeignTapeProtectedError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ==============================================================================
# 4. Media Browser, Queueing & Batch Jobs
# ==============================================================================


@router.get("/media")
def list_media_items(
    category: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    pools = {p.id: p for p in db.scalars(select(TapePool)).all()}
    stmt = select(MediaItem).order_by(MediaItem.sort_title, MediaItem.id)
    if category:
        stmt = stmt.where(MediaItem.media_category == category)
    if status:
        stmt = stmt.where(MediaItem.status == status)
    if q:
        stmt = stmt.where(MediaItem.title.ilike(f"%{q}%"))
    items = db.scalars(stmt).all()
    return [
        {
            "id": m.id,
            "source_type": m.source_type,
            "title": m.title,
            "year": m.year,
            "season_number": m.season_number,
            "media_category": m.media_category,
            "folder_path": m.folder_path,
            "size_bytes": m.size_bytes,
            "file_count": m.file_count,
            "monitored": m.monitored,
            "tags": m.tags,
            "status": m.status,
            "target_pool_id": m.target_pool_id,
            "target_pool_name": pools[m.target_pool_id].name if m.target_pool_id in pools else "Movies",
            "primary_tape_barcode": m.primary_tape_barcode,
            "primary_filemark": m.primary_filemark,
            "clone_tape_barcode": m.clone_tape_barcode,
            "stub_file_path": m.stub_file_path,
            "archived_at": m.archived_at.isoformat() if m.archived_at else None,
            "restored_at": m.restored_at.isoformat() if m.restored_at else None,
        }
        for m in items
    ]


@router.post("/media/unmanaged")
def add_unmanaged_folder(req: UnmanagedFolderRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    arr = ArrService(db)
    try:
        item = arr.register_unmanaged_folder(
            folder_path=req.folder_path,
            title=req.title,
            media_category=req.media_category,
            target_pool_id=req.target_pool_id,
            year=req.year,
        )
        return {"id": item.id, "title": item.title, "folder_path": item.folder_path, "size_bytes": item.size_bytes}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/media/seed-demo")
def seed_demo_media_on_disk(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Creates sample Movie, TV Season, and Anime directories in `./data/demo_media/`
    so the user can test staging, archiving to virtual LTO-5/LTO-6 tapes, `.mkv` stub generation,
    and single-pass restores right from the WebUI.
    """
    cfg = load_bootstrap_config()
    demo_root = Path(cfg["paths"]["data_dir"]) / "demo_media"
    movies_pool = db.scalar(select(TapePool).where(TapePool.name == "Movies"))
    tv_pool = db.scalar(select(TapePool).where(TapePool.name == "TV Shows"))
    anime_pool = db.scalar(select(TapePool).where(TapePool.name == "Anime"))

    samples = [
        (
            demo_root / "movies" / "Interstellar (2014)",
            "Interstellar",
            2014,
            "Movies",
            movies_pool.id if movies_pool else 1,
            {
                "Interstellar (2014) [Remux-2160p].mkv": b"MATROSKA_VIDEO_STREAM_INTERSTELLAR_" * 4096,
                "Interstellar (2014).en.srt": b"1\n00:00:01,000 --> 00:00:04,000\nDo not go gentle into that good night.\n",
                "movie.nfo": b"<movie><title>Interstellar</title><year>2014</year></movie>",
            },
        ),
        (
            demo_root / "tv" / "The Expanse" / "Season 01",
            "The Expanse - Season 01",
            2015,
            "TV Shows",
            tv_pool.id if tv_pool else 1,
            {
                "The.Expanse.S01E01.Dulcinea.1080p.mkv": b"EXPANSE_S01E01_PAYLOAD_" * 4096,
                "The.Expanse.S01E02.The.Big.Empty.1080p.mkv": b"EXPANSE_S01E02_PAYLOAD_" * 4096,
                "tvshow.nfo": b"<tvshow><title>The Expanse</title></tvshow>",
            },
        ),
        (
            demo_root / "anime" / "Cowboy Bebop" / "Season 01",
            "Cowboy Bebop - Season 01",
            1998,
            "Anime",
            anime_pool.id if anime_pool else 1,
            {
                "Cowboy.Bebop.S01E01.Asteroid.Blues.BD.1080p.mkv": b"BEBOP_EP01_DATA_" * 4096,
                "Cowboy.Bebop.S01E01.ja.ass": b"[Script Info]\nTitle: Asteroid Blues\n",
            },
        ),
    ]

    arr = ArrService(db)
    created = []
    for folder, title, year, cat, pid, files_dict in samples:
        folder.mkdir(parents=True, exist_ok=True)
        for fname, content in files_dict.items():
            (folder / fname).write_bytes(content)
        item = arr.register_unmanaged_folder(
            folder_path=str(folder),
            title=title,
            media_category=cat,
            target_pool_id=pid,
            year=year,
        )
        created.append(item.id)
    return {"seeded_media_ids": created, "demo_root": str(demo_root)}


@router.post("/media/queue-archive")
def queue_archive_endpoint(req: QueueArchiveRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    sched = TapeJobScheduler(db)
    try:
        return sched.queue_items_for_archive(
            media_item_ids=req.media_item_ids,
            target_pool_id=req.target_pool_id,
            post_archive_mode=req.post_archive_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/media/queue-restore")
def queue_restore_endpoint(req: QueueRestoreRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    sched = TapeJobScheduler(db)
    return sched.queue_items_for_restore(req.media_item_ids)


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    jobs = db.scalars(select(TapeJob).order_by(TapeJob.id.desc()).limit(50)).all()
    return [_serialize_job(j) for j in jobs]


@router.post("/jobs/run-archive")
def run_archive_endpoint(req: RunArchiveBatchRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    sched = TapeJobScheduler(db)
    try:
        return sched.run_archive_batch(pool_id=req.pool_id, post_archive_mode=req.post_archive_mode)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/jobs/run-restore")
def run_restore_endpoint(req: RunRestoreBatchRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    sched = TapeJobScheduler(db)
    try:
        return sched.run_restore_batch(remonitor_in_arr=req.remonitor_in_arr)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ==============================================================================
# 5. Catalog Search & Bare-Metal Disaster Recovery Backups
# ==============================================================================


@router.get("/catalog/search")
def search_catalog(
    q: Optional[str] = Query(default=None),
    tape_barcode: Optional[str] = Query(default=None),
    include_superseded: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    stmt = select(ArchivedFile).order_by(ArchivedFile.tape_barcode, ArchivedFile.filemark_number, ArchivedFile.relative_path)
    if not include_superseded:
        stmt = stmt.where(ArchivedFile.is_superseded.is_(False))
    if tape_barcode:
        stmt = stmt.where(
            or_(
                ArchivedFile.tape_barcode == tape_barcode.upper(),
                ArchivedFile.clone_tape_barcode == tape_barcode.upper(),
            )
        )
    if q:
        stmt = stmt.where(
            or_(
                ArchivedFile.title.ilike(f"%{q}%"),
                ArchivedFile.relative_path.ilike(f"%{q}%"),
                ArchivedFile.original_full_path.ilike(f"%{q}%"),
                ArchivedFile.tape_barcode.ilike(f"%{q}%"),
            )
        )
    rows = db.scalars(stmt.limit(500)).all()
    return [
        {
            "id": r.id,
            "media_item_id": r.media_item_id,
            "title": r.title,
            "tape_barcode": r.tape_barcode,
            "clone_tape_barcode": r.clone_tape_barcode,
            "tape_pool_name": r.tape_pool_name,
            "filemark_number": r.filemark_number,
            "block_offset": r.block_offset,
            "tar_block_factor": r.tar_block_factor,
            "relative_path": r.relative_path,
            "original_full_path": r.original_full_path,
            "size_bytes": r.size_bytes,
            "sha256_hash": r.sha256_hash,
            "taparr_version": r.taparr_version,
            "tape_format_version": r.tape_format_version,
            "is_superseded": r.is_superseded,
            "bare_metal_cmd": (
                f"mt -f /dev/nst0 rewind && mt -f /dev/nst0 fsf {r.filemark_number} && "
                f"tar -b {r.tar_block_factor} -xvf /dev/nst0 -C /mnt/restore"
            ),
            "archived_at": r.archived_at.isoformat() if r.archived_at else None,
        }
        for r in rows
    ]


@router.get("/catalog/backups")
def list_catalog_backups(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    return CatalogBackupExporter(db).list_backups()


@router.post("/catalog/backup")
def create_catalog_backup(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return CatalogBackupExporter(db).export_backup_bundle(reason="manual-webui")


# ==============================================================================
# 6. Settings, Sonarr/Radarr Instances, Seamless Updater & Plex Webhook
# ==============================================================================


@router.get("/settings")
def get_all_settings(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {k: get_setting(db, k) for k in DEFAULT_SETTINGS}


@router.put("/settings/{key}")
def update_setting_endpoint(key: str, payload: Dict[str, Any], db: Session = Depends(get_db)) -> Dict[str, Any]:
    if key not in DEFAULT_SETTINGS:
        raise HTTPException(status_code=400, detail=f"Unknown setting group: {key}")
    return update_setting(db, key, payload)


@router.get("/arr-instances")
def list_arr_instances(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    rows = db.scalars(select(ArrInstance).order_by(ArrInstance.id)).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "arr_type": r.arr_type,
            "url": r.url,
            "enabled": r.enabled,
            "default_pool_id": r.default_pool_id,
            "remote_path_prefix": r.remote_path_prefix,
            "local_path_prefix": r.local_path_prefix,
            "to_tape_tag": r.to_tape_tag,
            "on_tape_tag": r.on_tape_tag,
            "auto_unmonitor": r.auto_unmonitor,
            "last_sync_at": r.last_sync_at.isoformat() if r.last_sync_at else None,
        }
        for r in rows
    ]


@router.post("/arr-instances")
def create_arr_instance(req: ArrInstanceRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    inst = ArrInstance(**req.model_dump())
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return {"id": inst.id, "name": inst.name, "arr_type": inst.arr_type}


@router.post("/arr-instances/{instance_id}/sync")
def sync_arr_instance(instance_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    inst = db.get(ArrInstance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="Instance not found.")
    return ArrService(db).sync_instance(inst)


@router.get("/system/version")
def get_system_version_and_changelog(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return UpdateManager(db).get_version_and_changelog()


@router.post("/system/update")
def trigger_seamless_update(req: TriggerUpdateRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return UpdateManager(db).trigger_update(dry_run=req.dry_run, force=req.force)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/webhooks/plex")
def plex_webhook_endpoint(payload: Dict[str, Any], db: Session = Depends(get_db)) -> Dict[str, Any]:
    return PlexService(db).handle_playback_webhook(payload)


# ==============================================================================
# Serialization Helpers
# ==============================================================================


def _serialize_tape(t: Tape, pool: Optional[TapePool]) -> Dict[str, Any]:
    cap = max(1, t.native_capacity_bytes)
    return {
        "id": t.id,
        "barcode": t.barcode,
        "uuid": t.uuid,
        "media_code": t.media_code,
        "generation_label": t.generation_label,
        "pool_id": t.pool_id,
        "pool_name": pool.name if pool else "Unassigned",
        "pool_color": pool.color if pool else "#64748b",
        "is_protected_foreign": bool(pool and pool.is_protected_foreign) or t.status == "FOREIGN_LOCKED",
        "status": t.status,
        "location_type": t.location_type,
        "slot_number": t.slot_number,
        "drive_index": t.drive_index,
        "vault_note": t.vault_note,
        "native_capacity_bytes": t.native_capacity_bytes,
        "used_bytes": t.used_bytes,
        "dead_bytes": t.dead_bytes,
        "free_bytes": max(0, t.native_capacity_bytes - t.used_bytes),
        "usage_pct": round((t.used_bytes / cap) * 100.0, 2),
        "dead_pct": round((t.dead_bytes / cap) * 100.0, 2),
        "total_filemarks": t.total_filemarks,
        "manifest_filemark": t.manifest_filemark,
        "tar_block_factor": t.tar_block_factor,
        "header_taparr_version": t.header_taparr_version,
        "header_format_version": t.header_format_version,
        "clone_of_barcode": t.clone_of_barcode,
        "cloned_to_barcode": t.cloned_to_barcode,
        "last_written_at": t.last_written_at.isoformat() if t.last_written_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }


def _serialize_job(j: TapeJob) -> Dict[str, Any]:
    return {
        "id": j.id,
        "job_type": j.job_type,
        "status": j.status,
        "drive_index": j.drive_index,
        "pool_id": j.pool_id,
        "source_tape_barcode": j.source_tape_barcode,
        "target_tape_barcode": j.target_tape_barcode,
        "items_total": j.items_total,
        "items_completed": j.items_completed,
        "bytes_total": j.bytes_total,
        "bytes_processed": j.bytes_processed,
        "speed_mbps": j.speed_mbps,
        "progress_pct": j.progress_pct,
        "log_text": j.log_text,
        "error_message": j.error_message,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
    }
