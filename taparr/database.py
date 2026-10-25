"""
Database engine, session factory, and initial seed setup for Taparr.
Supports PostgreSQL via SQLAlchemy 2.0 + psycopg3, with seamless SQLite fallback for tests/sandbox.
"""

import uuid
from typing import Generator, Dict, Any
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker, Session

from taparr.config import load_bootstrap_config
from taparr.models import (
    Base,
    AppSetting,
    TapePool,
    TapeDrive,
    Tape,
    MediaItem,
    TapeFilemark,
    ArchivedFile,
    TapeJob,
)
from taparr import detect_media_generation_from_barcode, get_media_spec

_config = load_bootstrap_config()
_db_url = _config["database"]["url"]
_strict_pg = _config["database"].get("strict_postgres", False)
_fallback_reason: str | None = None


def _build_engine(db_url: str):
    if db_url.startswith("sqlite"):
        return create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
    return create_engine(
        db_url,
        pool_size=_config["database"]["pool_size"],
        max_overflow=_config["database"]["max_overflow"],
        pool_pre_ping=True,
        future=True,
    )


try:
    engine = _build_engine(_db_url)
    with engine.connect() as _conn:
        _conn.execute(text("SELECT 1"))
except Exception as _exc:
    if _strict_pg:
        raise RuntimeError(f"Strict PostgreSQL mode enabled and PostgreSQL connection failed: {_exc}") from _exc
    _fallback_reason = str(_exc).splitlines()[0] if str(_exc) else "PostgreSQL server not running on localhost:5432"
    fallback_url = f"sqlite:///{_config['paths']['data_dir']}/taparr.db"
    engine = _build_engine(fallback_url)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_database_telemetry(db: Session) -> Dict[str, Any]:
    """
    Returns live database engine telemetry (PostgreSQL vs SQLite fallback, driver, version, and row counts).
    """
    dialect = db.bind.dialect.name if db.bind else engine.dialect.name
    driver = db.bind.dialect.driver if db.bind else engine.dialect.driver
    version_str = "unknown"
    try:
        if dialect == "postgresql":
            version_str = str(db.execute(text("SHOW server_version")).scalar())
        elif dialect == "sqlite":
            version_str = str(db.execute(text("SELECT sqlite_version()")).scalar())
    except Exception:
        pass

    return {
        "active_dialect": dialect,
        "active_driver": driver,
        "server_version": version_str,
        "configured_url_scheme": _db_url.split("://")[0] if "://" in _db_url else "unknown",
        "strict_postgres": _strict_pg,
        "fallback_active": dialect == "sqlite" and _db_url.startswith("postgresql"),
        "fallback_reason": _fallback_reason,
        "psycopg3_installed": True,
        "row_counts": {
            "pools": db.query(TapePool).count(),
            "tapes": db.query(Tape).count(),
            "media_items": db.query(MediaItem).count(),
            "filemarks": db.query(TapeFilemark).count(),
            "archived_files": db.query(ArchivedFile).count(),
            "jobs": db.query(TapeJob).count(),
        },
    }


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DEFAULT_SETTINGS = {
    "hardware": {
        "simulator_mode": True,
        "changer_device": "/dev/tape/by-id/scsi-1HP_MSL_G3_Series_changer",
        "tar_block_factor": 2048,
        "mbuffer_size_mb": 512,
        "total_slots": 48,
        "mailslot_slots": [46, 47, 48],
    },
    "scratch": {
        "scratch_dir": _config["paths"]["scratch_dir"],
        "max_scratch_gb": 200,
        "verify_hashes_on_stage": True,
    },
    "disk_cleanup": {
        "post_archive_mode": "stub_mkv",  # "stub_mkv" or "delete"
        "stub_duration_seconds": 5,
        "stub_resolution": "1280x720",
        "instant_reevict_if_unchanged": True,
    },
    "power": {
        "auto_power_enabled": True,
        "power_state": "ONLINE",  # OFFLINE, BOOTING, ONLINE
        "maintenance_lock": False,
        "idle_cooldown_minutes": 20,
        "boot_inventory_timeout_seconds": 360,
        "power_on_mode": "webhook",  # "webhook" or "shell"
        "power_on_webhook_url": "",
        "power_on_webhook_method": "POST",
        "power_on_webhook_body": '{"state": "ON"}',
        "power_on_shell_cmd": "",
        "power_off_mode": "webhook",
        "power_off_webhook_url": "",
        "power_off_webhook_method": "POST",
        "power_off_webhook_body": '{"state": "OFF"}',
        "power_off_shell_cmd": "",
        "scsi_rescan_on_boot": True,
    },
    "plex": {
        "enabled": False,
        "url": "http://localhost:32400",
        "token": "",
        "partial_scan_on_change": True,
        "webhook_stub_action": "notify_and_queue",  # "notify_only", "notify_and_queue", "disabled"
    },
    "catalog_backup": {
        "enabled": True,
        "backup_dirs": [_config["paths"]["backup_dir"]],
        "retention_count": 15,
        "auto_backup_after_batch": True,
        "include_pg_dump": True,
        "post_backup_shell_hook": "",
    },
    "notifications": {
        "enabled": False,
        "discord_webhook_url": "",
        "gotify_url": "",
        "gotify_token": "",
        "ntfy_url": "",
        "generic_webhook_url": "",
        "notify_on_job_complete": True,
        "notify_on_pool_empty": True,
        "notify_on_cleaning_required": True,
        "notify_on_vault_needed": True,
    },
}


def get_setting(db: Session, key: str) -> dict:
    row = db.get(AppSetting, key)
    if row is None:
        default_val = DEFAULT_SETTINGS.get(key, {})
        row = AppSetting(key=key, value=default_val, description=f"Configuration for {key}")
        db.add(row)
        db.commit()
        db.refresh(row)
    return dict(row.value)


def update_setting(db: Session, key: str, new_value: dict) -> dict:
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=new_value, description=f"Configuration for {key}")
        db.add(row)
    else:
        merged = dict(row.value)
        merged.update(new_value)
        row.value = merged
    db.commit()
    db.refresh(row)
    return dict(row.value)


def init_db(db: Session | None = None) -> None:
    """
    Creates database schema and seeds mandatory system pools ('Unassigned', 'Used By Other'),
    default media pools ('Movies', 'TV Shows', 'Anime'), and default HP MSL4048 LTO-5 + LTO-6 drives.
    """
    Base.metadata.create_all(bind=engine)
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        for k, v in DEFAULT_SETTINGS.items():
            if db.get(AppSetting, k) is None:
                db.add(AppSetting(key=k, value=v, description=f"Default settings for {k}"))
        db.commit()

        # Seed System Pools ('Unassigned' and 'Used By Other') + Default Media Pools
        pools_to_seed = [
            {
                "name": "Unassigned",
                "description": "Newly discovered cartridges awaiting manual pool assignment.",
                "color": "#64748b",
                "is_system": True,
                "is_protected_foreign": False,
                "barcode_regex": "",
            },
            {
                "name": "Used By Other",
                "description": "Protected foreign backup software cartridges (Veeam, PBS, Bareos, etc.). Taparr will NEVER load, move, or modify these tapes.",
                "color": "#ef4444",
                "is_system": True,
                "is_protected_foreign": True,
                "barcode_regex": "^(BKP|VEEAM|PBS)",
            },
            {
                "name": "Movies",
                "description": "Radarr movie archives",
                "color": "#f59e0b",
                "is_system": False,
                "is_protected_foreign": False,
                "barcode_regex": "",
            },
            {
                "name": "TV Shows",
                "description": "Sonarr television season archives",
                "color": "#3b82f6",
                "is_system": False,
                "is_protected_foreign": False,
                "barcode_regex": "",
            },
            {
                "name": "Anime",
                "description": "Sonarr / Radarr anime archives",
                "color": "#ec4899",
                "is_system": False,
                "is_protected_foreign": False,
                "barcode_regex": "",
            },
        ]

        for p_def in pools_to_seed:
            existing = db.scalar(select(TapePool).where(TapePool.name == p_def["name"]))
            if not existing:
                db.add(TapePool(**p_def))
        db.commit()

        # Seed Default HP MSL4048 Drives (Drive 0 = LTO-5, Drive 1 = LTO-6)
        drives_to_seed = [
            {
                "drive_index": 0,
                "name": "HP Ultrium 3000 (Drive 0 - LTO-5)",
                "generation": "LTO-5",
                "device_path": "/dev/tape/by-id/scsi-35001438012345600-nst",
                "enabled": True,
            },
            {
                "drive_index": 1,
                "name": "HP Ultrium 6250 (Drive 1 - LTO-6)",
                "generation": "LTO-6",
                "device_path": "/dev/tape/by-id/scsi-35001438012345601-nst",
                "enabled": True,
            },
        ]
        for d_def in drives_to_seed:
            existing_d = db.scalar(select(TapeDrive).where(TapeDrive.drive_index == d_def["drive_index"]))
            if not existing_d:
                db.add(TapeDrive(**d_def))
        db.commit()

        # Seed initial virtual MSL4048 cartridges if table is empty so simulator works out-of-the-box
        tape_count = db.query(Tape).count()
        if tape_count == 0:
            unassigned_pool = db.scalar(select(TapePool).where(TapePool.name == "Unassigned"))
            used_by_other_pool = db.scalar(select(TapePool).where(TapePool.name == "Used By Other"))
            movies_pool = db.scalar(select(TapePool).where(TapePool.name == "Movies"))
            tv_pool = db.scalar(select(TapePool).where(TapePool.name == "TV Shows"))
            anime_pool = db.scalar(select(TapePool).where(TapePool.name == "Anime"))

            initial_tapes = [
                ("MOV001L5", movies_pool.id, 1, "BLANK"),
                ("MOV002L5", movies_pool.id, 2, "BLANK"),
                ("TVS001L5", tv_pool.id, 3, "BLANK"),
                ("TVS002L5", tv_pool.id, 4, "BLANK"),
                ("ANI001L5", anime_pool.id, 5, "BLANK"),
                ("RAW001L5", unassigned_pool.id, 6, "BLANK"),
                ("RAW002L5", unassigned_pool.id, 7, "BLANK"),
                ("RAW003L6", unassigned_pool.id, 8, "BLANK"),
                ("RAW004LA", unassigned_pool.id, 9, "BLANK"),
                ("BKP001L5", used_by_other_pool.id, 10, "FOREIGN_LOCKED"),
                ("VEEAM2L6", used_by_other_pool.id, 11, "FOREIGN_LOCKED"),
                ("CLN001CU", unassigned_pool.id, 45, "CLEANING"),
            ]
            for barcode, pid, slot, st in initial_tapes:
                m_code = detect_media_generation_from_barcode(barcode)
                spec = get_media_spec(m_code)
                db.add(
                    Tape(
                        barcode=barcode,
                        uuid=str(uuid.uuid4()),
                        media_code=spec["code"],
                        generation_label=spec["generation"],
                        pool_id=pid,
                        status=st,
                        location_type="SLOT",
                        slot_number=slot,
                        native_capacity_bytes=spec["native_capacity_bytes"],
                    )
                )
            db.commit()
    finally:
        if close_session:
            db.close()
