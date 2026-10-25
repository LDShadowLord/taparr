"""
SQLAlchemy 2.0 ORM Models for Taparr.
Supports PostgreSQL (primary production database) and SQLite (fallback/test).
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    String,
    Integer,
    BigInteger,
    Boolean,
    Text,
    DateTime,
    ForeignKey,
    JSON,
    Float,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ArrInstance(Base):
    """
    Represents a connected Sonarr (TV / Anime) or Radarr (Movies) instance.
    """
    __tablename__ = "arr_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    arr_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "sonarr" or "radarr"
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_pool_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tape_pools.id"), nullable=True)
    remote_path_prefix: Mapped[str] = mapped_column(String(512), default="")
    local_path_prefix: Mapped[str] = mapped_column(String(512), default="")
    to_tape_tag: Mapped[str] = mapped_column(String(64), default="to-tape")
    on_tape_tag: Mapped[str] = mapped_column(String(64), default="on-tape")
    auto_unmonitor: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TapePool(Base):
    """
    Tape Pool definition.
    Includes two permanent system pools:
      - 'Unassigned': newly discovered tapes waiting for user pool assignment.
      - 'Used By Other': foreign backup software tapes (hard-locked so Taparr never touches them).
    Plus user-defined pools (e.g., 'Movies', 'TV Shows', 'Anime', 'Offsite Clones').
    """
    __tablename__ = "tape_pools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(512), default="")
    color: Mapped[str] = mapped_column(String(32), default="#3b82f6")
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_protected_foreign: Mapped[bool] = mapped_column(Boolean, default=False)  # True for 'Used By Other'
    barcode_regex: Mapped[str] = mapped_column(String(256), default="")  # Optional auto-match rule
    close_threshold_gb: Mapped[int] = mapped_column(Integer, default=35)
    verify_after_write: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TapeDrive(Base):
    """
    Represents one of the physical or virtual LTO tape drives inside the HP MSL4048.
    Default configuration: Drive 0 = LTO-5, Drive 1 = LTO-6.
    Supports LTO-1 through LTO-10 (LA).
    """
    __tablename__ = "tape_drives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_index: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)  # 0 or 1 in mtx
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[str] = mapped_column(String(32), nullable=False, default="LTO-5")  # LTO-1 .. LTO-10
    device_path: Mapped[str] = mapped_column(String(512), nullable=False)  # /dev/tape/by-id/scsi-...-nst
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="IDLE")  # IDLE, WRITING, READING, CLONING, COMPACTING, CLEANING, OFFLINE
    loaded_barcode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_slot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_filemark: Mapped[int] = mapped_column(Integer, default=0)
    current_block: Mapped[int] = mapped_column(BigInteger, default=0)
    speed_mbps: Mapped[float] = mapped_column(Float, default=0.0)
    bytes_transferred: Mapped[int] = mapped_column(BigInteger, default=0)
    needs_cleaning: Mapped[bool] = mapped_column(Boolean, default=False)
    tape_alert_Message: Mapped[str] = mapped_column(String(512), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Tape(Base):
    """
    Represents an LTO cartridge (L1..LA, M8, CU) tracked by Taparr.
    Includes version fields (`header_taparr_version` and `header_format_version`) written to fm0.
    """
    __tablename__ = "tapes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    barcode: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    media_code: Mapped[str] = mapped_column(String(16), nullable=False, default="L5")  # L1..L9, LA, M8, CU
    generation_label: Mapped[str] = mapped_column(String(32), nullable=False, default="LTO-5")
    pool_id: Mapped[int] = mapped_column(Integer, ForeignKey("tape_pools.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="BLANK"
    )  # BLANK, ACTIVE, CLOSED, CLONED, FOREIGN_LOCKED, CLEANING
    location_type: Mapped[str] = mapped_column(
        String(32), default="SLOT"
    )  # SLOT, DRIVE, MAILSLOT, VAULT
    slot_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1..48
    drive_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 0 or 1
    vault_note: Mapped[str] = mapped_column(String(256), default="")
    native_capacity_bytes: Mapped[int] = mapped_column(BigInteger, default=1500000000000)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    dead_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    total_filemarks: Mapped[int] = mapped_column(Integer, default=0)  # Count of media filemarks (fm1..fmN)
    manifest_filemark: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Trailing manifest fm#
    tar_block_factor: Mapped[int] = mapped_column(Integer, default=2048)
    header_taparr_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    header_format_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    clone_of_barcode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    cloned_to_barcode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    formatted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_written_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MediaItem(Base):
    """
    Logical archival unit: a Movie folder (Radarr), a TV Season folder (Sonarr), or an Unmanaged folder.
    """
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "radarr", "sonarr", "unmanaged"
    arr_instance_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("arr_instances.id"), nullable=True)
    external_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Movie ID or Series ID in *arr
    season_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # For Sonarr seasons
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    sort_title: Mapped[str] = mapped_column(String(512), default="")
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    media_category: Mapped[str] = mapped_column(String(64), default="Movies")  # Movies, TV Shows, Anime, Unmanaged
    folder_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(
        String(32), default="ON_DISK"
    )
    # Valid statuses:
    # ON_DISK, QUEUED_ARCHIVE, STAGING_SSD, WRITING_TAPE,
    # ON_TAPE_ONLY, ON_TAPE_STUBBED, QUEUED_RESTORE, RESTORING, ON_DISK_AND_TAPE
    target_pool_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tape_pools.id"), nullable=True)
    primary_tape_barcode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    primary_filemark: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    clone_tape_barcode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stub_file_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    content_fingerprint_sha256: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    restored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TapeFilemark(Base):
    """
    Records an individual hardware EOF filemark on an LTO cartridge:
      - fm0: HEADER (1 KB versioned JSON/text descriptor)
      - fm1..fmN: MEDIA (Raw TAR archive of a Movie or Season)
      - fm(N+1): MANIFEST (Self-describing JSON/CSV catalog of the tape)
    """
    __tablename__ = "tape_filemarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tape_id: Mapped[int] = mapped_column(Integer, ForeignKey("tapes.id"), nullable=False)
    tape_barcode: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filemark_number: Mapped[int] = mapped_column(Integer, nullable=False)
    block_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    tar_block_factor: Mapped[int] = mapped_column(Integer, default=2048)
    record_type: Mapped[str] = mapped_column(String(32), nullable=False)  # HEADER, MEDIA, MANIFEST
    media_item_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("media_items.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    archive_sha256: Mapped[str] = mapped_column(String(128), default="")
    taparr_version: Mapped[str] = mapped_column(String(32), nullable=False)
    tape_format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_superseded: Mapped[bool] = mapped_column(Boolean, default=False)  # True if dead space / replaced
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArchivedFile(Base):
    """
    Individual file stored inside a TapeFilemark TAR stream.
    Forms the core table for Disaster Recovery Catalog Backups.
    """
    __tablename__ = "archived_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filemark_id: Mapped[int] = mapped_column(Integer, ForeignKey("tape_filemarks.id"), nullable=False)
    media_item_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("media_items.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    tape_barcode: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    clone_tape_barcode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tape_pool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    filemark_number: Mapped[int] = mapped_column(Integer, nullable=False)
    block_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    tar_block_factor: Mapped[int] = mapped_column(Integer, default=2048)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_full_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    taparr_version: Mapped[str] = mapped_column(String(32), nullable=False)
    tape_format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TapeJob(Base):
    """
    Tracks batch archive, restore, clone, compact, inventory, and drive clean jobs.
    """
    __tablename__ = "tape_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # ARCHIVE_BATCH, RESTORE_BATCH, CLONE_TAPE, COMPACT_TAPE, INVENTORY, CLEAN_DRIVE
    status: Mapped[str] = mapped_column(
        String(32), default="QUEUED"
    )  # QUEUED, POWERING_ON, STAGING_SSD, RUNNING, VERIFYING, COMPLETED, FAILED, PAUSED
    drive_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pool_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tape_pools.id"), nullable=True)
    source_tape_barcode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_tape_barcode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    items_total: Mapped[int] = mapped_column(Integer, default=0)
    items_completed: Mapped[int] = mapped_column(Integer, default=0)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    speed_mbps: Mapped[float] = mapped_column(Float, default=0.0)
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    log_text: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
