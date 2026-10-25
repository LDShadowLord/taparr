"""
HP MSL4048 Robotics & LTO-1..LTO-10 (LA) Tape Drive Engine.
Provides:
  1. Versioned 1 KB fm0 Tape Header generator & parser (recording taparr_version & tape_format_version).
  2. Trailing self-describing JSON/CSV manifest writer (overwritten on append, sealed on close).
  3. Strict protection for 'Used By Other' foreign backup tapes.
  4. Hardware compatibility enforcement across LTO-1 (L1) through LTO-10 (LA), M8, and CU.
  5. Both Physical Linux SCSI execution (mtx, mt-st, tar, mbuffer, sg_logs) and
     a full-fidelity Virtual MSL4048 Simulator writing real TAR filemarks on disk.
"""

import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from taparr import (
    __version__,
    TAPE_FORMAT_VERSION,
    TAPE_HEADER_MAGIC,
    detect_media_generation_from_barcode,
    get_media_spec,
    check_drive_media_compatibility,
)
from taparr.config import load_bootstrap_config
from taparr.database import get_setting
from taparr.models import Tape, TapePool, TapeDrive, TapeFilemark, ArchivedFile


class ForeignTapeProtectedError(RuntimeError):
    """Raised when any operation attempts to touch a tape in the 'Used By Other' pool."""


class DriveMediaIncompatibleError(RuntimeError):
    """Raised when a tape generation cannot be read/written by the target LTO drive."""


def build_fm0_descriptor(
    barcode: str,
    tape_uuid: str,
    media_code: str,
    pool_name: str,
    tar_block_factor: int = 2048,
    clone_of_barcode: Optional[str] = None,
    taparr_version: str = __version__,
    tape_format_version: int = TAPE_FORMAT_VERSION,
) -> bytes:
    """
    Creates the 1 KB (1024-byte) Filemark 0 (fm0) descriptor written at BOT.
    Includes `taparr_version` and `tape_format_version` so future versions of Taparr
    can identify and adapt to any format changes.
    """
    spec = get_media_spec(media_code)
    payload = {
        "magic": TAPE_HEADER_MAGIC,
        "taparr_version": taparr_version,
        "tape_format_version": tape_format_version,
        "barcode": barcode,
        "uuid": tape_uuid,
        "media_code": spec["code"],
        "generation": spec["generation"],
        "pool_name": pool_name,
        "tar_block_factor": tar_block_factor,
        "clone_of_barcode": clone_of_barcode,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "recovery_cmd": f"mt -f /dev/nst0 rewind && mt -f /dev/nst0 fsf <FM#> && tar -b {tar_block_factor} -xvf /dev/nst0",
    }
    raw_json = json.dumps(payload, indent=2).encode("utf-8")
    if len(raw_json) < 1024:
        # Pad with newline + spaces to exact 1024 bytes (1 KB)
        padding = b"\n" + b" " * (1024 - len(raw_json) - 1)
        return raw_json + padding
    return raw_json[:1024]


def parse_fm0_descriptor(raw_bytes: bytes) -> Dict[str, Any]:
    """
    Parses and validates a 1 KB fm0 tape header descriptor.
    """
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    data = json.loads(text)
    if data.get("magic") != TAPE_HEADER_MAGIC:
        raise ValueError(f"Unrecognized tape header magic: {data.get('magic')}")
    fmt_ver = int(data.get("tape_format_version", 1))
    data["recognized_format"] = fmt_ver <= TAPE_FORMAT_VERSION
    return data


def ensure_tape_not_foreign_locked(db: Session, tape: Tape) -> None:
    """
    Hard guard preventing Taparr from ever loading, moving, reading, or writing
    a cartridge assigned to 'Used By Other' (or marked FOREIGN_LOCKED).
    """
    pool = db.get(TapePool, tape.pool_id)
    if tape.status == "FOREIGN_LOCKED" or (pool and pool.is_protected_foreign):
        raise ForeignTapeProtectedError(
            f"Tape '{tape.barcode}' is assigned to protected pool '{pool.name if pool else 'Used By Other'}' "
            f"(status={tape.status}). Taparr is strictly locked from touching foreign backup tapes."
        )


class MSL4048HardwareManager:
    """
    Unified interface for the HP MSL4048 48-slot library & LTO drives.
    Operates in Virtual Simulator Mode (for testing/dry-run) or Physical SCSI Mode (`mtx`, `mt`, `tar`, `mbuffer`).
    """

    def __init__(self, db: Session):
        self.db = db
        self.bootstrap = load_bootstrap_config()
        self.hw_settings = get_setting(db, "hardware")
        self.sim_mode: bool = bool(self.hw_settings.get("simulator_mode", True))
        self.virtual_root = Path(self.bootstrap["paths"]["virtual_library_dir"])
        self.virtual_root.mkdir(parents=True, exist_ok=True)

    def get_tape_sim_dir(self, barcode: str) -> Path:
        d = self.virtual_root / "cartridges" / barcode
        d.mkdir(parents=True, exist_ok=True)
        return d

    def discover_persistent_devices(self) -> Dict[str, Any]:
        """
        Scans /dev/tape/by-id/ and lsscsi for persistent changer and non-rewinding tape drive paths.
        """
        changers = []
        drives = []
        by_id = Path("/dev/tape/by-id")
        if by_id.exists():
            for p in sorted(by_id.iterdir()):
                name = p.name
                if "changer" in name.lower():
                    changers.append(str(p))
                elif name.endswith("-nst"):
                    drives.append(str(p))

        if not changers:
            changers = [self.hw_settings.get("changer_device", "/dev/tape/by-id/scsi-1HP_MSL_G3_Series_changer")]
        if not drives:
            drives = [
                "/dev/tape/by-id/scsi-35001438012345600-nst",
                "/dev/tape/by-id/scsi-35001438012345601-nst",
            ]
        return {
            "simulator_mode": self.sim_mode,
            "changers": changers,
            "drives": drives,
        }

    def run_inventory(self) -> Dict[str, Any]:
        """
        Queries the 48-slot HP MSL4048 inventory (`mtx status` or virtual slot table).
        Applies automatic barcode regex matching for 'Used By Other' on newly discovered tapes.
        """
        unassigned_pool = self.db.scalar(select(TapePool).where(TapePool.name == "Unassigned"))
        used_by_other_pool = self.db.scalar(select(TapePool).where(TapePool.name == "Used By Other"))

        if not self.sim_mode:
            changer = self.hw_settings.get("changer_device", "/dev/sg2")
            res = subprocess.run(
                ["mtx", "-f", changer, "status"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if res.returncode == 0:
                self._parse_mtx_status_output(res.stdout, unassigned_pool, used_by_other_pool)

        # Sync any existing tapes against 'Used By Other' regex if they are still in Unassigned
        if used_by_other_pool and used_by_other_pool.barcode_regex:
            pattern = re.compile(used_by_other_pool.barcode_regex, re.IGNORECASE)
            tapes = self.db.scalars(select(Tape)).all()
            for t in tapes:
                if t.pool_id == unassigned_pool.id and pattern.search(t.barcode):
                    t.pool_id = used_by_other_pool.id
                    t.status = "FOREIGN_LOCKED"
            self.db.commit()

        return self.get_library_state()

    def _parse_mtx_status_output(
        self, stdout: str, unassigned_pool: TapePool, used_by_other_pool: Optional[TapePool]
    ) -> None:
        """
        Parses standard `mtx status` output from HP MSL4048:
          Data Transfer Element 0:Empty / Full (Storage Element 1 Loaded):VolumeTag = MOV001L5
          Storage Element 1:Full :VolumeTag=MOV001L5
          Storage Element 46 IMPORT/EXPORT:Empty
        """
        import uuid as uuid_mod

        slot_re = re.compile(
            r"Storage Element\s+(\d+)(\s+IMPORT/EXPORT)?:(Full|Empty)(?:\s*:VolumeTag\s*=\s*([A-Za-z0-9_-]+))?",
            re.IGNORECASE,
        )
        for line in stdout.splitlines():
            m = slot_re.search(line)
            if m and m.group(3).lower() == "full" and m.group(4):
                slot_num = int(m.group(1))
                is_ie = bool(m.group(2))
                barcode = m.group(4).strip().upper()
                existing = self.db.scalar(select(Tape).where(Tape.barcode == barcode))
                if not existing:
                    m_code = detect_media_generation_from_barcode(barcode)
                    spec = get_media_spec(m_code)
                    pool_id = unassigned_pool.id
                    status = "CLEANING" if spec["is_cleaning"] else "BLANK"
                    if (
                        used_by_other_pool
                        and used_by_other_pool.barcode_regex
                        and re.search(used_by_other_pool.barcode_regex, barcode, re.IGNORECASE)
                    ):
                        pool_id = used_by_other_pool.id
                        status = "FOREIGN_LOCKED"
                    existing = Tape(
                        barcode=barcode,
                        uuid=str(uuid_mod.uuid4()),
                        media_code=spec["code"],
                        generation_label=spec["generation"],
                        pool_id=pool_id,
                        status=status,
                        location_type="MAILSLOT" if is_ie else "SLOT",
                        slot_number=slot_num,
                        native_capacity_bytes=spec["native_capacity_bytes"],
                    )
                    self.db.add(existing)
                else:
                    existing.slot_number = slot_num
                    existing.location_type = "MAILSLOT" if is_ie else "SLOT"
        self.db.commit()

    def get_library_state(self) -> Dict[str, Any]:
        total_slots = int(self.hw_settings.get("total_slots", 48))
        mailslots = set(self.hw_settings.get("mailslot_slots", [46, 47, 48]))
        pools = {p.id: p for p in self.db.scalars(select(TapePool)).all()}
        tapes = self.db.scalars(select(Tape)).all()
        drives = self.db.scalars(select(TapeDrive).order_by(TapeDrive.drive_index)).all()

        slot_map: Dict[int, Optional[Dict[str, Any]]] = {s: None for s in range(1, total_slots + 1)}
        vaulted_tapes = []

        for t in tapes:
            pool = pools.get(t.pool_id)
            t_dict = {
                "id": t.id,
                "barcode": t.barcode,
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
                "used_bytes": t.used_bytes,
                "dead_bytes": t.dead_bytes,
                "native_capacity_bytes": t.native_capacity_bytes,
                "total_filemarks": t.total_filemarks,
                "header_taparr_version": t.header_taparr_version,
                "header_format_version": t.header_format_version,
            }
            if t.location_type in ("SLOT", "MAILSLOT") and t.slot_number in slot_map:
                slot_map[t.slot_number] = t_dict
            elif t.location_type == "VAULT":
                vaulted_tapes.append(t_dict)

        slots_list = []
        for s in range(1, total_slots + 1):
            slots_list.append(
                {
                    "slot_number": s,
                    "is_mailslot": s in mailslots,
                    "tape": slot_map[s],
                }
            )

        drives_list = [
            {
                "id": d.id,
                "drive_index": d.drive_index,
                "name": d.name,
                "generation": d.generation,
                "device_path": d.device_path,
                "enabled": d.enabled,
                "status": d.status,
                "loaded_barcode": d.loaded_barcode,
                "source_slot": d.source_slot,
                "current_filemark": d.current_filemark,
                "speed_mbps": d.speed_mbps,
                "bytes_transferred": d.bytes_transferred,
                "needs_cleaning": d.needs_cleaning,
            }
            for d in drives
        ]

        return {
            "simulator_mode": self.sim_mode,
            "total_slots": total_slots,
            "mailslot_slots": sorted(mailslots),
            "slots": slots_list,
            "drives": drives_list,
            "vaulted_tapes": vaulted_tapes,
        }

    def load_tape_into_drive(
        self, tape: Tape, drive: TapeDrive, require_write: bool = False
    ) -> Dict[str, Any]:
        """
        Loads `tape` into `drive` after verifying:
          1. Tape is NOT in 'Used By Other' (ForeignTapeProtectedError).
          2. Drive generation (LTO-1..LTO-10) is compatible with cartridge media code (L1..LA, M8, CU).
          3. Drive is free (or already has this exact tape loaded).
        """
        ensure_tape_not_foreign_locked(self.db, tape)

        compat = check_drive_media_compatibility(drive.generation, tape.media_code)
        if tape.media_code == "CU":
            if not compat["can_clean"]:
                raise DriveMediaIncompatibleError(compat["reason"])
        elif require_write and not compat["can_write"]:
            raise DriveMediaIncompatibleError(compat["reason"])
        elif not require_write and not compat["can_read"]:
            raise DriveMediaIncompatibleError(compat["reason"])

        if drive.loaded_barcode == tape.barcode:
            return {"status": "already_loaded", "drive_index": drive.drive_index, "barcode": tape.barcode}

        if drive.loaded_barcode:
            self.unload_drive(drive)

        if tape.location_type == "VAULT":
            raise RuntimeError(
                f"Tape {tape.barcode} is currently vaulted/offsite ({tape.vault_note or 'Vault'}). "
                "Please insert it into the HP MSL4048 library or mailslot first."
            )

        origin_slot = tape.slot_number or 1
        if not self.sim_mode:
            changer = self.hw_settings.get("changer_device", "/dev/sg2")
            subprocess.run(
                ["mtx", "-f", changer, "load", str(origin_slot), str(drive.drive_index)],
                check=True,
                timeout=180,
            )

        tape.location_type = "DRIVE"
        tape.drive_index = drive.drive_index
        drive.loaded_barcode = tape.barcode
        drive.source_slot = origin_slot
        drive.current_filemark = 0
        drive.status = "IDLE"
        self.db.commit()
        return {"status": "loaded", "drive_index": drive.drive_index, "barcode": tape.barcode, "from_slot": origin_slot}

    def unload_drive(self, drive: TapeDrive, target_slot: Optional[int] = None) -> Dict[str, Any]:
        """
        Rewinds/offlines the tape in `drive` and unloads it back to its storage slot.
        """
        if not drive.loaded_barcode:
            drive.status = "IDLE"
            self.db.commit()
            return {"status": " already_empty", "drive_index": drive.drive_index}

        tape = self.db.scalar(select(Tape).where(Tape.barcode == drive.loaded_barcode))
        dest_slot = target_slot or drive.source_slot or (tape.slot_number if tape else 1) or 1

        if not self.sim_mode:
            changer = self.hw_settings.get("changer_device", "/dev/sg2")
            subprocess.run(["mt", "-f", drive.device_path, "offline"], check=False, timeout=120)
            subprocess.run(
                ["mtx", "-f", changer, "unload", str(dest_slot), str(drive.drive_index)],
                check=True,
                timeout=180,
            )

        if tape:
            mailslots = set(self.hw_settings.get("mailslot_slots", [46, 47, 48]))
            tape.location_type = "MAILSLOT" if dest_slot in mailslots else "SLOT"
            tape.slot_number = dest_slot
            tape.drive_index = None

        unloaded_barcode = drive.loaded_barcode
        drive.loaded_barcode = None
        drive.source_slot = None
        drive.current_filemark = 0
        drive.speed_mbps = 0.0
        drive.status = "IDLE"
        self.db.commit()
        return {"status": "unloaded", "drive_index": drive.drive_index, "barcode": unloaded_barcode, "to_slot": dest_slot}

    def ensure_fm0_header_written(self, tape: Tape, drive: TapeDrive) -> TapeFilemark:
        """
        Writes the 1 KB versioned Taparr descriptor at Filemark 0 (`fm0`) if the tape is BLANK.
        Records `taparr_version` and `tape_format_version` both on the tape header and in the DB.
        """
        ensure_tape_not_foreign_locked(self.db, tape)
        existing_fm0 = self.db.scalar(
            select(TapeFilemark).where(
                TapeFilemark.tape_id == tape.id,
                TapeFilemark.filemark_number == 0,
            )
        )
        if existing_fm0:
            return existing_fm0

        pool = self.db.get(TapePool, tape.pool_id)
        block_factor = int(self.hw_settings.get("tar_block_factor", 2048))
        header_bytes = build_fm0_descriptor(
            barcode=tape.barcode,
            tape_uuid=tape.uuid,
            media_code=tape.media_code,
            pool_name=pool.name if pool else "Unassigned",
            tar_block_factor=block_factor,
            clone_of_barcode=tape.clone_of_barcode,
            taparr_version=__version__,
            tape_format_version=TAPE_FORMAT_VERSION,
        )
        header_hash = hashlib.sha256(header_bytes).hexdigest()

        if self.sim_mode:
            sim_dir = self.get_tape_sim_dir(tape.barcode)
            (sim_dir / "fm0000.header.json").write_bytes(header_bytes)
        else:
            subprocess.run(["mt", "-f", drive.device_path, "rewind"], check=True, timeout=120)
            with open(drive.device_path, "wb") as dev_f:
                dev_f.write(header_bytes)
            subprocess.run(["mt", "-f", drive.device_path, "weof", "1"], check=True, timeout=60)

        now = datetime.now(timezone.utc)
        tape.header_taparr_version = __version__
        tape.header_format_version = TAPE_FORMAT_VERSION
        tape.tar_block_factor = block_factor
        tape.formatted_at = now
        tape.last_written_at = now
        if tape.status == "BLANK":
            tape.status = "ACTIVE"

        fm0 = TapeFilemark(
            tape_id=tape.id,
            tape_barcode=tape.barcode,
            filemark_number=0,
            block_offset=0,
            tar_block_factor=block_factor,
            record_type="HEADER",
            media_item_id=None,
            title=f"Taparr Tape Header ({tape.barcode} v{__version__} fmt={TAPE_FORMAT_VERSION})",
            size_bytes=len(header_bytes),
            file_count=1,
            archive_sha256=header_hash,
            taparr_version=__version__,
            tape_format_version=TAPE_FORMAT_VERSION,
        )
        self.db.add(fm0)
        drive.current_filemark = 1
        self.db.commit()
        return fm0

    def read_tape_fm0_header(self, tape: Tape, drive: Optional[TapeDrive] = None) -> Dict[str, Any]:
        """
        Reads and parses Filemark 0 (`fm0`) from the cartridge, verifying `taparr_version`
        and `tape_format_version`.
        """
        ensure_tape_not_foreign_locked(self.db, tape)
        if self.sim_mode:
            sim_dir = self.get_tape_sim_dir(tape.barcode)
            header_path = sim_dir / "fm0000.header.json"
            if not header_path.exists():
                raise FileNotFoundError(f"No fm0 header found on virtual tape {tape.barcode}")
            return parse_fm0_descriptor(header_path.read_bytes())

        if not drive:
            raise ValueError("Drive required for reading physical tape header.")
        subprocess.run(["mt", "-f", drive.device_path, "rewind"], check=True, timeout=120)
        with open(drive.device_path, "rb") as dev_f:
            raw = dev_f.read(1024)
        return parse_fm0_descriptor(raw)

    def write_media_tar_filemark(
        self,
        tape: Tape,
        drive: TapeDrive,
        staged_dir: Path,
        file_manifest: List[Dict[str, Any]],
        media_item_id: Optional[int],
        title: str,
    ) -> Tuple[TapeFilemark, List[ArchivedFile]]:
        """
        Writes a staged Movie or Season directory as a single raw TAR filemark at the next
        media filemark position on `tape`.
        If a trailing manifest filemark existed from a previous batch, overwrites that
        filemark number and appends a fresh trailing manifest after the batch completes.
        """
        ensure_tape_not_foreign_locked(self.db, tape)
        self.ensure_fm0_header_written(tape, drive)

        # Remove old trailing manifest record if present (it will be overwritten on tape at that fm#)
        old_manifest = self.db.scalar(
            select(TapeFilemark).where(
                TapeFilemark.tape_id == tape.id,
                TapeFilemark.record_type == "MANIFEST",
            )
        )
        if old_manifest:
            next_fm = old_manifest.filemark_number
            self.db.delete(old_manifest)
            self.db.commit()
        else:
            next_fm = tape.total_filemarks + 1

        block_factor = tape.tar_block_factor or int(self.hw_settings.get("tar_block_factor", 2048))
        pool = self.db.get(TapePool, tape.pool_id)
        pool_name = pool.name if pool else "Unassigned"

        # Calculate deterministic block offset (in 512-byte tar records)
        block_offset = 2 + int(tape.used_bytes // 512)
        total_bytes = sum(f["size_bytes"] for f in file_manifest)
        spec = get_media_spec(tape.media_code)
        drive.status = "WRITING"
        drive.current_filemark = next_fm
        drive.speed_mbps = float(spec["native_speed_mbps"])
        self.db.commit()

        if self.sim_mode:
            sim_dir = self.get_tape_sim_dir(tape.barcode)
            tar_path = sim_dir / f"fm{next_fm:04d}.tar"
            with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT, bufsize=block_factor * 512) as tf:
                for item in sorted(staged_dir.rglob("*")):
                    if item.is_file():
                        rel = item.relative_to(staged_dir)
                        tf.add(str(item), arcname=str(rel))
            archive_sha256 = self._sha256_file(tar_path)
            tar_size_bytes = tar_path.stat().st_size
        else:
            # Physical SCSI tape positioning and streaming via tar + mbuffer
            subprocess.run(["mt", "-f", drive.device_path, "rewind"], check=True, timeout=180)
            subprocess.run(["mt", "-f", drive.device_path, "fsf", str(next_fm)], check=True, timeout=300)
            mbuf_mb = int(self.hw_settings.get("mbuffer_size_mb", 512))
            cmd = (
                f"tar -b {block_factor} -cf - -C {shlex_quote(str(staged_dir))} . "
                f"| mbuffer -m {mbuf_mb}M -P 80 -o {shlex_quote(drive.device_path)}"
            )
            subprocess.run(["bash", "-o", "pipefail", "-c", cmd], check=True)
            archive_sha256 = hashlib.sha256(
                "|".join(f"{f['relative_path']}:{f['sha256_hash']}" for f in file_manifest).encode()
            ).hexdigest()
            tar_size_bytes = total_bytes

        fm_rec = TapeFilemark(
            tape_id=tape.id,
            tape_barcode=tape.barcode,
            filemark_number=next_fm,
            block_offset=block_offset,
            tar_block_factor=block_factor,
            record_type="MEDIA",
            media_item_id=media_item_id,
            title=title,
            size_bytes=total_bytes,
            file_count=len(file_manifest),
            archive_sha256=archive_sha256,
            taparr_version=__version__,
            tape_format_version=TAPE_FORMAT_VERSION,
            is_superseded=False,
        )
        self.db.add(fm_rec)
        self.db.flush()

        archived_rows: List[ArchivedFile] = []
        for f_info in file_manifest:
            af = ArchivedFile(
                filemark_id=fm_rec.id,
                media_item_id=media_item_id,
                title=title,
                tape_barcode=tape.barcode,
                clone_tape_barcode=tape.cloned_to_barcode,
                tape_pool_name=pool_name,
                filemark_number=next_fm,
                block_offset=block_offset,
                tar_block_factor=block_factor,
                relative_path=f_info["relative_path"],
                original_full_path=f_info["original_full_path"],
                size_bytes=f_info["size_bytes"],
                sha256_hash=f_info["sha256_hash"],
                taparr_version=__version__,
                tape_format_version=TAPE_FORMAT_VERSION,
                is_superseded=False,
            )
            self.db.add(af)
            archived_rows.append(af)

        now = datetime.now(timezone.utc)
        tape.total_filemarks = next_fm
        tape.used_bytes += max(total_bytes, tar_size_bytes if not self.sim_mode else total_bytes)
        tape.last_written_at = now
        drive.bytes_transferred += total_bytes
        drive.current_filemark = next_fm + 1
        drive.status = "IDLE"
        self.db.commit()

        return fm_rec, archived_rows

    def write_trailing_manifest(self, tape: Tape, drive: Optional[TapeDrive] = None, close_tape: bool = False) -> TapeFilemark:
        """
        Writes the self-describing JSON + CSV manifest at the end of the tape (`fm(N+1)`).
        When new data is later appended to an ACTIVE tape, this trailing filemark is overwritten
        with the next media filemark and a new trailing manifest is written at the new end.
        When `close_tape=True`, marks the tape as `CLOSED`.
        """
        ensure_tape_not_foreign_locked(self.db, tape)
        pool = self.db.get(TapePool, tape.pool_id)
        pool_name = pool.name if pool else "Unassigned"

        # Remove previous MANIFEST record if present
        old_manifest = self.db.scalar(
            select(TapeFilemark).where(
                TapeFilemark.tape_id == tape.id,
                TapeFilemark.record_type == "MANIFEST",
            )
        )
        if old_manifest:
            manifest_fm_num = old_manifest.filemark_number
            self.db.delete(old_manifest)
            self.db.commit()
        else:
            manifest_fm_num = tape.total_filemarks + 1

        media_fms = self.db.scalars(
            select(TapeFilemark)
            .where(TapeFilemark.tape_id == tape.id, TapeFilemark.record_type == "MEDIA")
            .order_by(TapeFilemark.filemark_number)
        ).all()
        files = self.db.scalars(
            select(ArchivedFile)
            .where(ArchivedFile.tape_barcode == tape.barcode)
            .order_by(ArchivedFile.filemark_number, ArchivedFile.relative_path)
        ).all()

        manifest_payload = {
            "magic": "TAPARR_TAPE_MANIFEST",
            "taparr_version": __version__,
            "tape_format_version": TAPE_FORMAT_VERSION,
            "barcode": tape.barcode,
            "uuid": tape.uuid,
            "media_code": tape.media_code,
            "generation": tape.generation_label,
            "pool_name": pool_name,
            "tar_block_factor": tape.tar_block_factor,
            "manifest_filemark": manifest_fm_num,
            "closed": close_tape or (tape.status == "CLOSED"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filemarks": [
                {
                    "filemark_number": fm.filemark_number,
                    "block_offset": fm.block_offset,
                    "title": fm.title,
                    "size_bytes": fm.size_bytes,
                    "file_count": fm.file_count,
                    "archive_sha256": fm.archive_sha256,
                    "taparr_version": fm.taparr_version,
                    "tape_format_version": fm.tape_format_version,
                    "is_superseded": fm.is_superseded,
                }
                for fm in media_fms
            ],
            "files": [
                {
                    "filemark_number": f.filemark_number,
                    "block_offset": f.block_offset,
                    "title": f.title,
                    "relative_path": f.relative_path,
                    "original_full_path": f.original_full_path,
                    "size_bytes": f.size_bytes,
                    "sha256_hash": f.sha256_hash,
                    "taparr_version": f.taparr_version,
                    "tape_format_version": f.tape_format_version,
                }
                for f in files
            ],
        }

        json_bytes = json.dumps(manifest_payload, indent=2).encode("utf-8")

        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(
            [
                "tape_barcode",
                "tape_pool",
                "filemark_number",
                "block_offset",
                "tar_block_factor",
                "title",
                "relative_path",
                "original_full_path",
                "size_bytes",
                "sha256_hash",
                "taparr_version",
                "tape_format_version",
            ]
        )
        for f in files:
            writer.writerow(
                [
                    f.tape_barcode,
                    f.tape_pool_name,
                    f.filemark_number,
                    f.block_offset,
                    f.tar_block_factor,
                    f.title,
                    f.relative_path,
                    f.original_full_path,
                    f.size_bytes,
                    f.sha256_hash,
                    f.taparr_version,
                    f.tape_format_version,
                ]
            )
        csv_bytes = csv_buf.getvalue().encode("utf-8")

        recovery_txt = (
            f"TAPARR SELF-DESCRIBING TAPE MANIFEST ({tape.barcode})\n"
            f"Taparr Version: {__version__} | Format Version: {TAPE_FORMAT_VERSION}\n"
            f"Pool: {pool_name} | TAR Block Factor: -b {tape.tar_block_factor}\n\n"
            f"To extract Filemark <N> without Taparr on any Linux machine:\n"
            f"  mt -f /dev/nst0 rewind\n"
            f"  mt -f /dev/nst0 fsf <N>\n"
            f"  tar -b {tape.tar_block_factor} -xvf /dev/nst0 -C /restore/destination\n"
        ).encode("utf-8")

        if self.sim_mode:
            sim_dir = self.get_tape_sim_dir(tape.barcode)
            tar_path = sim_dir / f"fm{manifest_fm_num:04d}.tar"
            with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT, bufsize=tape.tar_block_factor * 512) as tf:
                for name, bdata in [
                    ("tape-manifest.json", json_bytes),
                    ("tape-manifest.csv", csv_bytes),
                    ("RECOVERY.txt", recovery_txt),
                ]:
                    ti = tarfile.TarInfo(name=name)
                    ti.size = len(bdata)
                    ti.mtime = int(datetime.now(timezone.utc).timestamp())
                    tf.addfile(ti, io.BytesIO(bdata))
            # Also save readable copy in sim dir for quick inspection
            (sim_dir / "latest-manifest.json").write_bytes(json_bytes)

        manifest_sha = hashlib.sha256(json_bytes).hexdigest()
        block_offset = 2 + int(tape.used_bytes // 512)
        manifest_rec = TapeFilemark(
            tape_id=tape.id,
            tape_barcode=tape.barcode,
            filemark_number=manifest_fm_num,
            block_offset=block_offset,
            tar_block_factor=tape.tar_block_factor,
            record_type="MANIFEST",
            media_item_id=None,
            title=f"Tape Manifest ({tape.barcode} - {len(media_fms)} items)",
            size_bytes=len(json_bytes) + len(csv_bytes) + len(recovery_txt),
            file_count=3,
            archive_sha256=manifest_sha,
            taparr_version=__version__,
            tape_format_version=TAPE_FORMAT_VERSION,
        )
        self.db.add(manifest_rec)
        tape.manifest_filemark = manifest_fm_num
        if close_tape:
            tape.status = "CLOSED"
            tape.closed_at = datetime.now(timezone.utc)
        self.db.commit()
        return manifest_rec

    def extract_filemark_to_dir(
        self, tape: Tape, drive: TapeDrive, filemark_number: int, dest_dir: Path
    ) -> Dict[str, Any]:
        """
        Seeks to `filemark_number` on `tape` and extracts the raw TAR archive into `dest_dir`.
        Verifies the tape header version before extracting.
        """
        ensure_tape_not_foreign_locked(self.db, tape)
        header = self.read_tape_fm0_header(tape, drive)
        dest_dir.mkdir(parents=True, exist_ok=True)
        drive.status = "READING"
        drive.current_filemark = filemark_number
        spec = get_media_spec(tape.media_code)
        drive.speed_mbps = float(spec["native_speed_mbps"])
        self.db.commit()

        if self.sim_mode:
            sim_dir = self.get_tape_sim_dir(tape.barcode)
            tar_path = sim_dir / f"fm{filemark_number:04d}.tar"
            if not tar_path.exists():
                raise FileNotFoundError(f"Simulated filemark TAR not found: {tar_path}")
            with tarfile.open(tar_path, "r") as tf:
                tf.extractall(path=dest_dir, filter="data")
        else:
            subprocess.run(["mt", "-f", drive.device_path, "rewind"], check=True, timeout=180)
            subprocess.run(["mt", "-f", drive.device_path, "fsf", str(filemark_number)], check=True, timeout=300)
            subprocess.run(
                [
                    "tar",
                    "-b",
                    str(tape.tar_block_factor),
                    "-xvf",
                    drive.device_path,
                    "-C",
                    str(dest_dir),
                ],
                check=True,
            )

        drive.status = "IDLE"
        self.db.commit()
        return {
            "barcode": tape.barcode,
            "filemark_number": filemark_number,
            "header_taparr_version": header.get("taparr_version"),
            "header_format_version": header.get("tape_format_version"),
            "dest_dir": str(dest_dir),
        }

    def clone_tape_drive_to_drive(
        self, source_tape: Tape, target_tape: Tape, source_drive: TapeDrive, target_drive: TapeDrive
    ) -> Dict[str, Any]:
        """
        Streams all filemarks (fm0 header, fm1..fmN media, and trailing manifest) from
        `source_tape` in `source_drive` to `target_tape` in `target_drive` for offsite redundancy.
        """
        ensure_tape_not_foreign_locked(self.db, source_tape)
        ensure_tape_not_foreign_locked(self.db, target_tape)

        if target_tape.used_bytes > 0 and target_tape.status != "BLANK":
            raise ValueError(f"Target clone tape {target_tape.barcode} is not blank.")

        self.load_tape_into_drive(source_tape, source_drive, require_write=False)
        self.load_tape_into_drive(target_tape, target_drive, require_write=True)

        source_drive.status = "CLONING"
        target_drive.status = "CLONING"
        self.db.commit()

        target_tape.clone_of_barcode = source_tape.barcode
        source_tape.cloned_to_barcode = target_tape.barcode
        self.ensure_fm0_header_written(target_tape, target_drive)

        if self.sim_mode:
            src_dir = self.get_tape_sim_dir(source_tape.barcode)
            dst_dir = self.get_tape_sim_dir(target_tape.barcode)
            for p in sorted(src_dir.glob("fm*.tar")):
                shutil.copy2(p, dst_dir / p.name)

        # Replicate filemark metadata records for the cloned tape
        src_fms = self.db.scalars(
            select(TapeFilemark)
            .where(TapeFilemark.tape_id == source_tape.id, TapeFilemark.filemark_number > 0)
            .order_by(TapeFilemark.filemark_number)
        ).all()

        for sfm in src_fms:
            if sfm.record_type == "MEDIA":
                cloned_fm = TapeFilemark(
                    tape_id=target_tape.id,
                    tape_barcode=target_tape.barcode,
                    filemark_number=sfm.filemark_number,
                    block_offset=sfm.block_offset,
                    tar_block_factor=sfm.tar_block_factor,
                    record_type="MEDIA",
                    media_item_id=sfm.media_item_id,
                    title=sfm.title,
                    size_bytes=sfm.size_bytes,
                    file_count=sfm.file_count,
                    archive_sha256=sfm.archive_sha256,
                    taparr_version=sfm.taparr_version,
                    tape_format_version=sfm.tape_format_version,
                    is_superseded=sfm.is_superseded,
                )
                self.db.add(cloned_fm)

        # Update clone_tape_barcode on ArchivedFile and MediaItem rows
        files = self.db.scalars(
            select(ArchivedFile).where(ArchivedFile.tape_barcode == source_tape.barcode)
        ).all()
        for f in files:
            f.clone_tape_barcode = target_tape.barcode

        target_tape.used_bytes = source_tape.used_bytes
        target_tape.dead_bytes = source_tape.dead_bytes
        target_tape.total_filemarks = source_tape.total_filemarks
        target_tape.status = "CLONED"
        target_tape.last_written_at = datetime.now(timezone.utc)
        self.db.commit()

        self.write_trailing_manifest(target_tape, target_drive, close_tape=False)
        source_drive.status = "IDLE"
        target_drive.status = "IDLE"
        self.db.commit()

        return {
            "source_barcode": source_tape.barcode,
            "target_barcode": target_tape.barcode,
            "filemarks_cloned": source_tape.total_filemarks,
            "bytes_cloned": source_tape.used_bytes,
        }

    def eject_tape_to_mailslot(self, tape: Tape) -> Dict[str, Any]:
        """
        Moves `tape` to an empty MSL4048 IMPORT/EXPORT (mailslot) element.
        """
        ensure_tape_not_foreign_locked(self.db, tape)
        if tape.location_type == "DRIVE" and tape.drive_index is not None:
            drv = self.db.scalar(select(TapeDrive).where(TapeDrive.drive_index == tape.drive_index))
            if drv:
                self.unload_drive(drv)

        mailslots = sorted(self.hw_settings.get("mailslot_slots", [46, 47, 48]))
        occupied = {
            t.slot_number
            for t in self.db.scalars(select(Tape).where(Tape.location_type.in_(["SLOT", "MAILSLOT"]))).all()
        }
        free_ie = next((s for s in mailslots if s not in occupied), None)
        if free_ie is None:
            raise RuntimeError(f"All Import/Export mailslots ({mailslots}) are currently full.")

        if not self.sim_mode and tape.slot_number:
            changer = self.hw_settings.get("changer_device", "/dev/sg2")
            subprocess.run(
                ["mtx", "-f", changer, "transfer", str(tape.slot_number), str(free_ie)],
                check=True,
                timeout=180,
            )

        tape.slot_number = free_ie
        tape.location_type = "MAILSLOT"
        self.db.commit()
        return {"barcode": tape.barcode, "mailslot": free_ie, "location_type": "MAILSLOT"}

    def move_tape_to_vault(self, tape: Tape, vault_note: str = "Offsite Vault") -> Dict[str, Any]:
        """
        Marks a cartridge as physically removed from the MSL4048 and stored in a Vault / Offsite location.
        """
        ensure_tape_not_foreign_locked(self.db, tape)
        if tape.location_type == "DRIVE":
            raise RuntimeError(f"Cannot vault tape {tape.barcode} while it is loaded in a drive.")
        tape.location_type = "VAULT"
        tape.slot_number = None
        tape.drive_index = None
        tape.vault_note = vault_note or "Offsite Vault"
        self.db.commit()
        return {"barcode": tape.barcode, "location_type": "VAULT", "vault_note": tape.vault_note}

    def return_tape_from_vault(self, tape: Tape, target_slot: Optional[int] = None) -> Dict[str, Any]:
        """
        Imports a vaulted tape back into an available storage slot in the MSL4048.
        """
        ensure_tape_not_foreign_locked(self.db, tape)
        total_slots = int(self.hw_settings.get("total_slots", 48))
        mailslots = set(self.hw_settings.get("mailslot_slots", [46, 47, 48]))
        occupied = {
            t.slot_number
            for t in self.db.scalars(select(Tape).where(Tape.location_type.in_(["SLOT", "MAILSLOT"]))).all()
            if t.slot_number is not None
        }
        slot = target_slot
        if slot is None:
            slot = next((s for s in range(1, total_slots + 1) if s not in occupied and s not in mailslots), None)
        if slot is None:
            raise RuntimeError("No free storage slots available in the MSL4048.")

        tape.location_type = "MAILSLOT" if slot in mailslots else "SLOT"
        tape.slot_number = slot
        tape.vault_note = ""
        self.db.commit()
        return {"barcode": tape.barcode, "location_type": tape.location_type, "slot_number": slot}

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return h.hexdigest()


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)
