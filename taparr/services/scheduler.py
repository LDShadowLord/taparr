"""
Dual-Drive Job Scheduler, Queue Bin-Packer, Single-Pass Ascending Filemark Restorer,
Instant Re-Eviction Engine, Drive-to-Drive Cloner & Dual-Drive Tape Compactor.
"""

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from taparr import check_drive_media_compatibility
from taparr.database import get_setting
from taparr.models import (
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
    ensure_tape_not_foreign_locked,
)
from taparr.services.scratch import ScratchPipelineManager
from taparr.services.stub_generator import apply_post_archive_disk_cleanup
from taparr.services.arr_client import ArrService
from taparr.services.plex_client import PlexService
from taparr.services.power import LibraryPowerManager
from taparr.services.catalog_backup import CatalogBackupExporter
from taparr.services.notifications import NotificationDispatcher


class TapeJobScheduler:
    def __init__(self, db: Session):
        self.db = db
        self.hw = MSL4048HardwareManager(db)
        self.scratch = ScratchPipelineManager(db)
        self.arr = ArrService(db)
        self.plex = PlexService(db)
        self.power = LibraryPowerManager(db)
        self.catalog_exporter = CatalogBackupExporter(db)
        self.notifier = NotificationDispatcher(db)

    def queue_items_for_archive(
        self,
        media_item_ids: List[int],
        target_pool_id: Optional[int] = None,
        post_archive_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Queues selected MediaItems for tape archival.
        If an item is currently `ON_DISK_AND_TAPE` (previously archived and restored)
        and its disk files are unchanged (verified by SHA-256 fingerprint), performs
        an **instant re-eviction** on disk without needing to re-write the tape!
        If its disk files were upgraded/modified, marks the old tape filemark as
        superseded (dead space) and queues the item for a fresh tape write.
        """
        if target_pool_id is not None:
            pool = self.db.get(TapePool, target_pool_id)
            if not pool:
                raise ValueError(f"Tape pool {target_pool_id} not found.")
            if pool.is_protected_foreign or pool.name == "Unassigned":
                raise ValueError(
                    f"Cannot queue media items to system pool '{pool.name}'. Please select an active media pool."
                )

        cleanup_cfg = get_setting(self.db, "disk_cleanup")
        instant_reevict = bool(cleanup_cfg.get("instant_reevict_if_unchanged", True))

        queued_ids: List[int] = []
        instantly_reevicted_ids: List[int] = []
        superseded_filemarks: List[int] = []

        for item_id in media_item_ids:
            item = self.db.get(MediaItem, item_id)
            if not item:
                continue
            if target_pool_id is not None:
                item.target_pool_id = target_pool_id

            # Check if item is already on tape and currently restored on disk (ON_DISK_AND_TAPE)
            if (
                item.status == "ON_DISK_AND_TAPE"
                and item.primary_tape_barcode
                and item.primary_filemark
                and instant_reevict
            ):
                current_fp, _ = self.scratch.compute_source_fingerprint(Path(item.folder_path))
                if current_fp and current_fp == item.content_fingerprint_sha256:
                    # Files on disk are 100% identical to the existing filemark on tape!
                    pool_obj = self.db.get(TapePool, item.target_pool_id) if item.target_pool_id else None
                    apply_post_archive_disk_cleanup(
                        self.db,
                        media_item=item,
                        tape_barcode=item.primary_tape_barcode,
                        pool_name=pool_obj.name if pool_obj else "Movies",
                        filemark_number=item.primary_filemark,
                        override_mode=post_archive_mode,
                    )
                    self.arr.notify_arr_archived(item, item.primary_tape_barcode)
                    self.plex.refresh_folder(item.folder_path)
                    instantly_reevicted_ids.append(item.id)
                    continue
                else:
                    # Files on disk changed (e.g. upgraded by Sonarr/Radarr): mark old filemark as dead space
                    self._mark_previous_filemark_superseded(item)
                    superseded_filemarks.append(item.id)

            item.status = "QUEUED_ARCHIVE"
            queued_ids.append(item.id)

        self.db.commit()
        return {
            "queued_ids": queued_ids,
            "instantly_reevicted_ids": instantly_reevicted_ids,
            "superseded_updated_ids": superseded_filemarks,
        }

    def _mark_previous_filemark_superseded(self, item: MediaItem) -> None:
        if not item.primary_tape_barcode or not item.primary_filemark:
            return
        old_fm = self.db.scalar(
            select(TapeFilemark).where(
                TapeFilemark.tape_barcode == item.primary_tape_barcode,
                TapeFilemark.filemark_number == item.primary_filemark,
                TapeFilemark.record_type == "MEDIA",
            )
        )
        if old_fm and not old_fm.is_superseded:
            old_fm.is_superseded = True
            tape = self.db.scalar(select(Tape).where(Tape.barcode == item.primary_tape_barcode))
            if tape:
                tape.dead_bytes += old_fm.size_bytes
            for af in self.db.scalars(select(ArchivedFile).where(ArchivedFile.filemark_id == old_fm.id)).all():
                af.is_superseded = True
            self.db.commit()

    def queue_items_for_restore(self, media_item_ids: List[int]) -> Dict[str, Any]:
        restorable = []
        vault_warnings = []
        for item_id in media_item_ids:
            item = self.db.get(MediaItem, item_id)
            if not item or not item.primary_tape_barcode:
                continue
            item.status = "QUEUED_RESTORE"
            restorable.append(item.id)

            tape = self.db.scalar(select(Tape).where(Tape.barcode == item.primary_tape_barcode))
            if tape and tape.location_type == "VAULT":
                # Check if clone tape is in library
                clone = (
                    self.db.scalar(select(Tape).where(Tape.barcode == item.clone_tape_barcode))
                    if item.clone_tape_barcode
                    else None
                )
                if not clone or clone.location_type == "VAULT":
                    vault_warnings.append(
                        {
                            "media_item_id": item.id,
                            "title": item.title,
                            "barcode": item.primary_tape_barcode,
                            "vault_note": tape.vault_note,
                        }
                    )
        self.db.commit()
        if vault_warnings:
            self.notifier.send_event(
                event_type="vault_tape_required",
                title="Vaulted Tape Required for Restore",
                message=f"Please insert vaulted tape(s) {[w['barcode'] for w in vault_warnings]} into the MSL4048.",
                extra={"vault_warnings": vault_warnings},
            )
        return {"queued_restore_ids": restorable, "vault_warnings": vault_warnings}

    def run_archive_batch(
        self,
        pool_id: Optional[int] = None,
        post_archive_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Flushes all `QUEUED_ARCHIVE` items (or those for `pool_id`) to tape:
          - Wakes the HP MSL4048 if powered off.
          - Assigns pools across available drives (supporting concurrent dual-drive LTO-5 + LTO-6 usage).
          - Performs Queue Bin-Packing when a cartridge nears full capacity so logical units
            are never split across cartridges while maximizing tape fill efficiency.
          - Stages each item via the Pipelined Local SSD Scratch Stager (with inline SHA-256).
          - Writes `fm0` (with `taparr_version` and `tape_format_version`), `fm1..fmN`, and the
            trailing self-describing manifest `fm(N+1)`.
          - Applies post-archive disk cleanup (`stub_mkv` or `delete`) + Sonarr/Radarr unmonitor + Plex scan.
          - Exports a Disaster Recovery Catalog Backup bundle upon completion.
        """
        stmt = select(MediaItem).where(MediaItem.status == "QUEUED_ARCHIVE")
        if pool_id is not None:
            stmt = stmt.where(MediaItem.target_pool_id == pool_id)
        queued_items = list(self.db.scalars(stmt.order_by(MediaItem.id)).all())
        if not queued_items:
            return {"status": "empty_queue", "jobs": []}

        # Power on MSL4048 if offline
        self.power.ensure_library_online()

        # Group queued items by target_pool_id
        default_pool = self.db.scalar(select(TapePool).where(TapePool.name == "Movies"))
        grouped: Dict[int, List[MediaItem]] = {}
        for item in queued_items:
            pid = item.target_pool_id or (default_pool.id if default_pool else 1)
            item.target_pool_id = pid
            grouped.setdefault(pid, []).append(item)
        self.db.commit()

        enabled_drives = list(
            self.db.scalars(
                select(TapeDrive).where(TapeDrive.enabled.is_(True)).order_by(TapeDrive.drive_index)
            ).all()
        )
        if not enabled_drives:
            raise RuntimeError("No enabled LTO tape drives available.")

        completed_jobs = []
        for idx, (pid, pool_items) in enumerate(grouped.items()):
            pool = self.db.get(TapePool, pid)
            if not pool or pool.is_protected_foreign or pool.name == "Unassigned":
                raise RuntimeError(
                    f"Invalid target pool '{pool.name if pool else pid}' for archive batch."
                )

            job = TapeJob(
                job_type="ARCHIVE_BATCH",
                status="RUNNING",
                pool_id=pool.id,
                items_total=len(pool_items),
                bytes_total=sum(max(1, i.size_bytes) for i in pool_items),
                started_at=datetime.now(timezone.utc),
                log_text=f"[Batch Start] Archiving {len(pool_items)} item(s) to Pool '{pool.name}'.\n",
            )
            self.db.add(job)
            self.db.commit()

            try:
                res = self._process_pool_archive_queue(job, pool, pool_items, enabled_drives, idx, post_archive_mode)
                completed_jobs.append(res)
            except Exception as exc:
                job.status = "FAILED"
                job.error_message = str(exc)
                job.log_text += f"[ERROR] {exc}\n"
                job.completed_at = datetime.now(timezone.utc)
                self.db.commit()
                raise

        # Auto-export Disaster Recovery Catalog Backup after batch
        backup_cfg = get_setting(self.db, "catalog_backup")
        backup_bundle = None
        if backup_cfg.get("auto_backup_after_batch", True):
            backup_bundle = self.catalog_exporter.export_backup_bundle(reason="post-archive")

        self.notifier.send_event(
            event_type="archive_batch_complete",
            title="Tape Archive Batch Completed",
            message=f"Completed {len(completed_jobs)} pool batch(es). Backup: {backup_bundle['bundle_name'] if backup_bundle else 'none'}",
        )

        return {
            "status": "completed",
            "jobs": completed_jobs,
            "catalog_backup": backup_bundle,
        }

    def _process_pool_archive_queue(
        self,
        job: TapeJob,
        pool: TapePool,
        pool_items: List[MediaItem],
        enabled_drives: List[TapeDrive],
        pool_seq_index: int,
        post_archive_mode: Optional[str],
    ) -> Dict[str, Any]:
        remaining_items = list(pool_items)
        tapes_used: List[str] = []
        close_threshold_bytes = int(pool.close_threshold_gb) * (1000**3)

        while remaining_items:
            tape = self._select_writable_tape_for_pool(pool)
            if not tape:
                job.status = "PAUSED"
                msg = (
                    f"Pool '{pool.name}' has no available BLANK or ACTIVE tapes with sufficient space. "
                    "Please assign a cartridge from 'Unassigned' in the WebUI."
                )
                job.error_message = msg
                job.log_text += f"[PAUSED] {msg}\n"
                self.db.commit()
                self.notifier.send_event("pool_empty", f"Tape Pool '{pool.name}' Out of Tapes", msg)
                break

            drive = self._select_compatible_drive(enabled_drives, tape, require_write=True, prefer_index=pool_seq_index)
            job.drive_index = drive.drive_index
            job.target_tape_barcode = tape.barcode
            self.hw.load_tape_into_drive(tape, drive, require_write=True)
            if tape.barcode not in tapes_used:
                tapes_used.append(tape.barcode)
            job.log_text += f"[Drive {drive.drive_index} ({drive.generation})] Loaded tape {tape.barcode}.\n"
            self.db.commit()

            wrote_any_on_this_tape = False

            while remaining_items:
                free_bytes = max(0, tape.native_capacity_bytes - tape.used_bytes)

                # Bin-Packing selection:
                # 1. If the next item in queue order fits on the current tape, pick it.
                # 2. Otherwise, scan remaining queue for the largest item that DOES fit in `free_bytes`!
                selected_item: Optional[MediaItem] = None
                if remaining_items[0].size_bytes <= free_bytes:
                    selected_item = remaining_items[0]
                else:
                    fitting_candidates = [i for i in remaining_items if i.size_bytes <= free_bytes]
                    if fitting_candidates:
                        # Pick largest candidate that fits to pack remaining space tightly
                        fitting_candidates.sort(key=lambda x: x.size_bytes, reverse=True)
                        selected_item = fitting_candidates[0]
                        job.log_text += (
                            f"[Bin-Packer] '{remaining_items[0].title}' ({remaining_items[0].size_bytes} B) "
                            f"exceeds remaining space ({free_bytes} B) on {tape.barcode}; "
                            f"packing '{selected_item.title}' ({selected_item.size_bytes} B) first.\n"
                        )

                if selected_item is None:
                    # No remaining item in queue fits on this tape -> seal/close this tape!
                    job.log_text += (
                        f"[Close Tape] Remaining space ({free_bytes} B) on {tape.barcode} cannot fit "
                        "next queued item. Writing final manifest and closing tape.\n"
                    )
                    self.hw.write_trailing_manifest(tape, drive, close_tape=True)
                    self.hw.unload_drive(drive)
                    break

                remaining_items.remove(selected_item)

                # Step 1: Stage item to local SSD scratch & compute SHA-256 hashes
                selected_item.status = "STAGING_SSD"
                job.status = "STAGING_SSD"
                job.log_text += f"[SSD Scratch] Staging '{selected_item.title}' & computing SHA-256...\n"
                self.db.commit()

                stage_dir, file_manifest, _ = self.scratch.stage_item_for_archive(selected_item)

                try:
                    # Re-check actual staged size against free_bytes on tape
                    if selected_item.size_bytes > (tape.native_capacity_bytes - tape.used_bytes):
                        # Put item back at front of queue, close current tape, and move to next tape
                        remaining_items.insert(0, selected_item)
                        self.scratch.cleanup_staged_item(stage_dir)
                        self.hw.write_trailing_manifest(tape, drive, close_tape=True)
                        self.hw.unload_drive(drive)
                        break

                    # Step 2: Stream from SSD scratch to LTO tape filemark
                    selected_item.status = "WRITING_TAPE"
                    job.status = "RUNNING"
                    self.db.commit()

                    fm_rec, _ = self.hw.write_media_tar_filemark(
                        tape=tape,
                        drive=drive,
                        staged_dir=stage_dir,
                        file_manifest=file_manifest,
                        media_item_id=selected_item.id,
                        title=selected_item.title,
                    )
                    wrote_any_on_this_tape = True

                    # Step 3: Optional read-back verification pass
                    if pool.verify_after_write:
                        job.status = "VERIFYING"
                        verify_dir = self.scratch.get_restore_stage_dir(selected_item.id)
                        self.hw.extract_filemark_to_dir(tape, drive, fm_rec.filemark_number, verify_dir)
                        shutil.rmtree(verify_dir, ignore_errors=True)
                        job.log_text += f"[Verify] Read-back verified {tape.barcode} FM #{fm_rec.filemark_number}.\n"
                finally:
                    # Step 4: Clear item from local SSD scratch immediately to free space for next item
                    self.scratch.cleanup_staged_item(stage_dir)

                # Step 5: Update MediaItem catalog pointers & clean up NAS disk (playable .mkv stub or delete)
                selected_item.primary_tape_barcode = tape.barcode
                selected_item.primary_filemark = fm_rec.filemark_number
                selected_item.archived_at = datetime.now(timezone.utc)
                self.db.commit()

                cleanup_res = apply_post_archive_disk_cleanup(
                    self.db,
                    media_item=selected_item,
                    tape_barcode=tape.barcode,
                    pool_name=pool.name,
                    filemark_number=fm_rec.filemark_number,
                    override_mode=post_archive_mode,
                )
                self.arr.notify_arr_archived(selected_item, tape.barcode)
                self.plex.refresh_folder(selected_item.folder_path)

                job.items_completed += 1
                job.bytes_processed += selected_item.size_bytes
                job.speed_mbps = drive.speed_mbps
                job.progress_pct = round((job.items_completed / max(1, job.items_total)) * 100.0, 1)
                job.log_text += (
                    f"[Archived] '{selected_item.title}' -> {tape.barcode} FM #{fm_rec.filemark_number} "
                    f"({cleanup_res['mode']}).\n"
                )
                self.db.commit()

                # Auto-close tape if remaining capacity is below pool close threshold
                rem_after = max(0, tape.native_capacity_bytes - tape.used_bytes)
                if close_threshold_bytes > 0 and rem_after <= close_threshold_bytes:
                    job.log_text += (
                        f"[Auto-Close] {tape.barcode} remaining capacity ({rem_after} B) is below "
                        f"threshold ({close_threshold_bytes} B). Sealing tape.\n"
                    )
                    self.hw.write_trailing_manifest(tape, drive, close_tape=True)
                    self.hw.unload_drive(drive)
                    break

            if wrote_any_on_this_tape and tape.status != "CLOSED":
                # Always write updated trailing manifest at the end of the batch!
                self.hw.write_trailing_manifest(tape, drive, close_tape=False)

        if job.status != "PAUSED":
            job.status = "COMPLETED"
            job.progress_pct = 100.0
            job.completed_at = datetime.now(timezone.utc)
            self.db.commit()

        return {
            "job_id": job.id,
            "pool": pool.name,
            "status": job.status,
            "items_completed": job.items_completed,
            "items_total": job.items_total,
            "tapes_used": tapes_used,
        }

    def run_restore_batch(self, remonitor_in_arr: bool = True) -> Dict[str, Any]:
        """
        Executes all `QUEUED_RESTORE` items:
          - Groups items by tape cartridge.
          - Sorts filemarks on each cartridge in **strictly ascending order (`fm1` -> `fm5` -> `fm20`)**
            so each tape is loaded once and read strictly forward in a single pass!
          - Extracts each TAR to local SSD scratch, verifies every file's SHA-256 hash,
            removes any `.taparr_stub.mkv` slate, moves restored files to the NAS folder,
            marks the item `ON_DISK_AND_TAPE` (for future instant re-eviction!),
            and triggers Sonarr/Radarr rescan + Plex folder refresh.
        """
        queued = list(self.db.scalars(select(MediaItem).where(MediaItem.status == "QUEUED_RESTORE")).all())
        if not queued:
            return {"status": "empty_queue", "restored_items": []}

        self.power.ensure_library_online()

        enabled_drives = list(
            self.db.scalars(
                select(TapeDrive).where(TapeDrive.enabled.is_(True)).order_by(TapeDrive.drive_index)
            ).all()
        )
        if not enabled_drives:
            raise RuntimeError("No enabled LTO tape drives available for restore.")

        # Group by tape barcode (automatically using clone_tape_barcode if primary is vaulted)
        by_tape: Dict[str, List[MediaItem]] = {}
        for item in queued:
            barcode = item.primary_tape_barcode
            primary_tape = self.db.scalar(select(Tape).where(Tape.barcode == barcode)) if barcode else None
            if primary_tape and primary_tape.location_type == "VAULT" and item.clone_tape_barcode:
                clone_tape = self.db.scalar(select(Tape).where(Tape.barcode == item.clone_tape_barcode))
                if clone_tape and clone_tape.location_type != "VAULT":
                    barcode = clone_tape.barcode
            if not barcode:
                continue
            by_tape.setdefault(barcode, []).append(item)

        job = TapeJob(
            job_type="RESTORE_BATCH",
            status="RUNNING",
            items_total=len(queued),
            bytes_total=sum(i.size_bytes for i in queued),
            started_at=datetime.now(timezone.utc),
            log_text=f"[Restore Batch] Restoring {len(queued)} item(s) across {len(by_tape)} tape(s).\n",
        )
        self.db.add(job)
        self.db.commit()

        restored_summary = []

        for idx, (barcode, items_on_tape) in enumerate(by_tape.items()):
            tape = self.db.scalar(select(Tape).where(Tape.barcode == barcode))
            if not tape:
                raise RuntimeError(f"Tape {barcode} not found in catalog.")
            ensure_tape_not_foreign_locked(self.db, tape)

            drive = self._select_compatible_drive(enabled_drives, tape, require_write=False, prefer_index=idx)
            job.drive_index = drive.drive_index
            job.source_tape_barcode = tape.barcode
            self.hw.load_tape_into_drive(tape, drive, require_write=False)

            # Single-pass ascending filemark sort!
            items_on_tape.sort(key=lambda m: int(m.primary_filemark or 0))
            fm_order = [m.primary_filemark for m in items_on_tape]
            job.log_text += (
                f"[Single-Pass Restore] Loaded {tape.barcode} in Drive {drive.drive_index}; "
                f"reading ascending filemarks: {fm_order}\n"
            )
            self.db.commit()

            for item in items_on_tape:
                fm_num = int(item.primary_filemark or 1)
                item.status = "RESTORING"
                self.db.commit()

                restore_stage_dir = self.scratch.get_restore_stage_dir(item.id)
                self.hw.extract_filemark_to_dir(tape, drive, fm_num, restore_stage_dir)

                expected_files = list(
                    self.db.scalars(
                        select(ArchivedFile).where(
                            ArchivedFile.tape_barcode == item.primary_tape_barcode,
                            ArchivedFile.filemark_number == fm_num,
                            ArchivedFile.is_superseded.is_(False),
                        )
                    ).all()
                )
                verify_res = self.scratch.verify_and_unstage_restore(item, restore_stage_dir, expected_files)

                item.status = "ON_DISK_AND_TAPE"
                item.stub_file_path = None
                item.restored_at = datetime.now(timezone.utc)
                self.arr.notify_arr_restored(item, remonitor=remonitor_in_arr)
                self.plex.refresh_folder(item.folder_path)

                job.items_completed += 1
                job.bytes_processed += verify_res["bytes_restored"]
                job.progress_pct = round((job.items_completed / max(1, job.items_total)) * 100.0, 1)
                job.log_text += (
                    f"[Restored] '{item.title}' from {tape.barcode} FM #{fm_num} "
                    f"({verify_res['verified_files']} files SHA-256 verified).\n"
                )
                self.db.commit()

                restored_summary.append(
                    {
                        "media_item_id": item.id,
                        "title": item.title,
                        "tape_barcode": tape.barcode,
                        "filemark_number": fm_num,
                        "verified_files": verify_res["verified_files"],
                        "status": item.status,
                    }
                )

        job.status = "COMPLETED"
        job.progress_pct = 100.0
        job.completed_at = datetime.now(timezone.utc)
        self.db.commit()

        return {
            "status": "completed",
            "job_id": job.id,
            "restored_items": restored_summary,
        }

    def clone_tape(self, source_barcode: str, target_barcode: str) -> Dict[str, Any]:
        """
        Uses both drives in the HP MSL4048 to stream a complete 1:1 clone of `source_barcode`
        onto `target_barcode` for offsite vault storage.
        """
        self.power.ensure_library_online()
        source_tape = self.db.scalar(select(Tape).where(Tape.barcode == source_barcode))
        target_tape = self.db.scalar(select(Tape).where(Tape.barcode == target_barcode))
        if not source_tape or not target_tape:
            raise ValueError("Both source and target tapes must exist.")

        ensure_tape_not_foreign_locked(self.db, source_tape)
        ensure_tape_not_foreign_locked(self.db, target_tape)

        drives = list(
            self.db.scalars(
                select(TapeDrive).where(TapeDrive.enabled.is_(True)).order_by(TapeDrive.drive_index)
            ).all()
        )
        if len(drives) < 2:
            raise RuntimeError("Drive-to-Drive Tape Cloning requires 2 enabled drives.")

        src_drive, dst_drive = self._select_drive_pair_for_dual_op(drives, source_tape, target_tape)

        job = TapeJob(
            job_type="CLONE_TAPE",
            status="RUNNING",
            source_tape_barcode=source_tape.barcode,
            target_tape_barcode=target_tape.barcode,
            items_total=source_tape.total_filemarks,
            bytes_total=source_tape.used_bytes,
            started_at=datetime.now(timezone.utc),
            log_text=f"[Clone] Streaming {source_tape.barcode} (Drive {src_drive.drive_index}) -> {target_tape.barcode} (Drive {dst_drive.drive_index}).\n",
        )
        self.db.add(job)
        self.db.commit()

        res = self.hw.clone_tape_drive_to_drive(source_tape, target_tape, src_drive, dst_drive)

        # Update MediaItem clone pointers
        for item in self.db.scalars(
            select(MediaItem).where(MediaItem.primary_tape_barcode == source_tape.barcode)
        ).all():
            item.clone_tape_barcode = target_tape.barcode

        job.status = "COMPLETED"
        job.items_completed = source_tape.total_filemarks
        job.bytes_processed = source_tape.used_bytes
        job.progress_pct = 100.0
        job.completed_at = datetime.now(timezone.utc)
        job.log_text += f"[Clone Complete] {res['filemarks_cloned']} filemarks cloned to {target_tape.barcode}.\n"
        self.db.commit()

        self.catalog_exporter.export_backup_bundle(reason=f"clone-{source_tape.barcode}")
        return {"job_id": job.id, **res}

    def compact_tape(self, source_barcode: str, target_barcode: Optional[str] = None) -> Dict[str, Any]:
        """
        Reclaims wasted/dead space (`dead_bytes`) on `source_barcode` using dual-drive compaction:
          1. Loads `source_barcode` into Drive A and a BLANK `target_barcode` (from the same pool) into Drive B.
          2. Streams only active (`is_superseded == False`) MEDIA filemarks onto `target_barcode`
             with compact sequential filemark numbers (`fm1..fmK`) and a fresh `fm0` header + manifest.
          3. Updates all `MediaItem` and `ArchivedFile` catalog pointers to the new tape and filemark numbers.
          4. Resets `source_barcode` back to `BLANK` (`used_bytes=0`, `dead_bytes=0`) for reuse!
        """
        self.power.ensure_library_online()
        source_tape = self.db.scalar(select(Tape).where(Tape.barcode == source_barcode))
        if not source_tape:
            raise ValueError(f"Source tape {source_barcode} not found.")
        ensure_tape_not_foreign_locked(self.db, source_tape)

        pool = self.db.get(TapePool, source_tape.pool_id)
        if target_barcode:
            target_tape = self.db.scalar(select(Tape).where(Tape.barcode == target_barcode))
        else:
            target_tape = self.db.scalar(
                select(Tape).where(
                    Tape.pool_id == source_tape.pool_id,
                    Tape.status == "BLANK",
                    Tape.barcode != source_tape.barcode,
                    Tape.location_type == "SLOT",
                )
            )
        if not target_tape:
            raise RuntimeError(
                f"No BLANK target tape available in pool '{pool.name if pool else source_tape.pool_id}' for compaction."
            )
        ensure_tape_not_foreign_locked(self.db, target_tape)

        drives = list(
            self.db.scalars(
                select(TapeDrive).where(TapeDrive.enabled.is_(True)).order_by(TapeDrive.drive_index)
            ).all()
        )
        if len(drives) < 2:
            raise RuntimeError("Dual-Drive Tape Compaction requires 2 enabled drives.")

        src_drive, dst_drive = self._select_drive_pair_for_dual_op(drives, source_tape, target_tape)
        self.hw.load_tape_into_drive(source_tape, src_drive, require_write=False)
        self.hw.load_tape_into_drive(target_tape, dst_drive, require_write=True)

        active_fms = list(
            self.db.scalars(
                select(TapeFilemark)
                .where(
                    TapeFilemark.tape_id == source_tape.id,
                    TapeFilemark.record_type == "MEDIA",
                    TapeFilemark.is_superseded.is_(False),
                )
                .order_by(TapeFilemark.filemark_number)
            ).all()
        )

        reclaimed_bytes = source_tape.dead_bytes
        job = TapeJob(
            job_type="COMPACT_TAPE",
            status="RUNNING",
            source_tape_barcode=source_tape.barcode,
            target_tape_barcode=target_tape.barcode,
            items_total=len(active_fms),
            bytes_total=sum(fm.size_bytes for fm in active_fms),
            started_at=datetime.now(timezone.utc),
            log_text=f"[Compaction] Repacking {len(active_fms)} active filemark(s) from {source_tape.barcode} -> {target_tape.barcode}.\n",
        )
        self.db.add(job)
        self.db.commit()

        self.hw.ensure_fm0_header_written(target_tape, dst_drive)

        new_fm_num = 0
        for old_fm in active_fms:
            new_fm_num += 1
            if self.hw.sim_mode:
                src_tar = self.hw.get_tape_sim_dir(source_tape.barcode) / f"fm{old_fm.filemark_number:04d}.tar"
                dst_tar = self.hw.get_tape_sim_dir(target_tape.barcode) / f"fm{new_fm_num:04d}.tar"
                if src_tar.exists():
                    shutil.copy2(src_tar, dst_tar)
            else:
                self.hw.stream_filemark_between_drives(
                    src_drive=src_drive,
                    dst_drive=dst_drive,
                    src_fm=old_fm.filemark_number,
                    dst_fm=new_fm_num,
                    block_factor=target_tape.tar_block_factor,
                )

            block_offset = self.hw.read_hardware_block_position(dst_drive, target_tape.used_bytes)
            new_fm = TapeFilemark(
                tape_id=target_tape.id,
                tape_barcode=target_tape.barcode,
                filemark_number=new_fm_num,
                block_offset=block_offset,
                tar_block_factor=target_tape.tar_block_factor,
                record_type="MEDIA",
                media_item_id=old_fm.media_item_id,
                title=old_fm.title,
                size_bytes=old_fm.size_bytes,
                file_count=old_fm.file_count,
                archive_sha256=old_fm.archive_sha256,
                taparr_version=old_fm.taparr_version,
                tape_format_version=old_fm.tape_format_version,
                is_superseded=False,
            )
            self.db.add(new_fm)
            self.db.flush()

            # Repoint ArchivedFile rows and MediaItem to the new compacted tape & filemark
            for af in self.db.scalars(select(ArchivedFile).where(ArchivedFile.filemark_id == old_fm.id)).all():
                af.filemark_id = new_fm.id
                af.tape_barcode = target_tape.barcode
                af.filemark_number = new_fm_num
                af.block_offset = block_offset

            if old_fm.media_item_id:
                m_item = self.db.get(MediaItem, old_fm.media_item_id)
                if m_item:
                    m_item.primary_tape_barcode = target_tape.barcode
                    m_item.primary_filemark = new_fm_num

            target_tape.used_bytes += old_fm.size_bytes
            target_tape.total_filemarks = new_fm_num
            job.items_completed += 1
            job.bytes_processed += old_fm.size_bytes

        self.db.commit()
        self.hw.write_trailing_manifest(target_tape, dst_drive, close_tape=False)

        # Clean up old filemarks and reset source_tape to BLANK
        for af in self.db.scalars(
            select(ArchivedFile).where(ArchivedFile.tape_barcode == source_tape.barcode)
        ).all():
            self.db.delete(af)
        for fm in self.db.scalars(
            select(TapeFilemark).where(TapeFilemark.tape_id == source_tape.id)
        ).all():
            self.db.delete(fm)

        if self.hw.sim_mode:
            shutil.rmtree(self.hw.get_tape_sim_dir(source_tape.barcode), ignore_errors=True)
        else:
            import subprocess
            subprocess.run(["mt", "-f", src_drive.device_path, "rewind"], check=False, timeout=180)
            subprocess.run(["mt", "-f", src_drive.device_path, "weof", "2"], check=False, timeout=60)

        source_tape.status = "BLANK"
        source_tape.used_bytes = 0
        source_tape.dead_bytes = 0
        source_tape.total_filemarks = 0
        source_tape.manifest_filemark = None
        source_tape.header_taparr_version = None
        source_tape.header_format_version = None

        job.status = "COMPLETED"
        job.progress_pct = 100.0
        job.completed_at = datetime.now(timezone.utc)
        job.log_text += (
            f"[Compaction Complete] {new_fm_num} active filemarks written to {target_tape.barcode}; "
            f"{reclaimed_bytes} bytes of dead space reclaimed. {source_tape.barcode} reset to BLANK.\n"
        )
        self.db.commit()

        self.catalog_exporter.export_backup_bundle(reason=f"compact-{source_tape.barcode}")
        return {
            "job_id": job.id,
            "source_barcode": source_tape.barcode,
            "target_barcode": target_tape.barcode,
            "active_filemarks_kept": new_fm_num,
            "reclaimed_dead_bytes": reclaimed_bytes,
        }

    def _select_writable_tape_for_pool(self, pool: TapePool) -> Optional[Tape]:
        if pool.is_protected_foreign or pool.name == "Unassigned":
            return None
        # Prefer ACTIVE appendable tape first, then BLANK tape assigned to this pool
        active = self.db.scalars(
            select(Tape)
            .where(
                Tape.pool_id == pool.id,
                Tape.status == "ACTIVE",
                Tape.location_type.in_(["SLOT", "DRIVE"]),
            )
            .order_by(Tape.barcode)
        ).first()
        if active and (active.native_capacity_bytes - active.used_bytes) > 0:
            return active

        blank = self.db.scalars(
            select(Tape)
            .where(
                Tape.pool_id == pool.id,
                Tape.status == "BLANK",
                Tape.location_type.in_(["SLOT", "DRIVE"]),
            )
            .order_by(Tape.barcode)
        ).first()
        return blank

    @staticmethod
    def _select_compatible_drive(
        drives: List[TapeDrive],
        tape: Tape,
        require_write: bool = True,
        prefer_index: int = 0,
    ) -> TapeDrive:
        # Check if tape is already loaded in one of the drives
        for d in drives:
            if d.loaded_barcode == tape.barcode:
                compat = check_drive_media_compatibility(d.generation, tape.media_code)
                if (require_write and compat["can_write"]) or (not require_write and compat["can_read"]):
                    return d

        compatible_drives = []
        for d in drives:
            compat = check_drive_media_compatibility(d.generation, tape.media_code)
            if (require_write and compat["can_write"]) or (not require_write and compat["can_read"]):
                compatible_drives.append(d)

        if not compatible_drives:
            raise RuntimeError(
                f"No enabled tape drive is hardware-compatible with cartridge {tape.barcode} ({tape.generation_label})."
            )

        return compatible_drives[prefer_index % len(compatible_drives)]

    @staticmethod
    def _select_drive_pair_for_dual_op(
        drives: List[TapeDrive], source_tape: Tape, target_tape: Tape
    ):
        for i, d_src in enumerate(drives):
            for j, d_dst in enumerate(drives):
                if i == j:
                    continue
                c_src = check_drive_media_compatibility(d_src.generation, source_tape.media_code)
                c_dst = check_drive_media_compatibility(d_dst.generation, target_tape.media_code)
                if c_src["can_read"] and c_dst["can_write"]:
                    return d_src, d_dst
        raise RuntimeError(
            f"Could not find a compatible drive pair to read {source_tape.barcode} ({source_tape.media_code}) "
            f"and write {target_tape.barcode} ({target_tape.media_code})."
        )
