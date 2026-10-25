"""
Disaster Recovery Catalog Backup Exporter.
Produces self-contained, zero-dependency recovery bundles so that every file on every tape
can be located and extracted using nothing but a standard Linux shell (`mtx`, `mt`, `tar`).

Each bundle directory contains:
  - `catalog-files.csv` & `catalog-files.json`: Every archived file, SHA-256, tape barcode,
    clone tape barcode, tape pool, filemark #, block offset, TAR block factor,
    `taparr_version`, and `tape_format_version`.
  - `tapes-inventory.csv` & `tapes-inventory.json`: All tapes, pools, capacities, and versions.
  - `RECOVERY.md`: Step-by-step bare-metal recovery guide.
  - `recover-bare.sh`: Standalone POSIX recovery script.
  - `taparr-pgdump.sql`: Full PostgreSQL database dump (or portable SQL/JSON snapshot).
"""

import csv
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List
from sqlalchemy import select
from sqlalchemy.orm import Session

from taparr import __version__, TAPE_FORMAT_VERSION
from taparr.config import load_bootstrap_config, PROJECT_ROOT
from taparr.database import get_setting
from taparr.models import Tape, TapePool, TapeFilemark, ArchivedFile


class CatalogBackupExporter:
    def __init__(self, db: Session):
        self.db = db
        self.bootstrap = load_bootstrap_config()
        self.cfg = get_setting(db, "catalog_backup")

    def export_backup_bundle(self, reason: str = "manual") -> Dict[str, Any]:
        """
        Generates a complete Disaster Recovery Catalog Backup bundle across all configured
        `backup_dirs`, rotates old backups according to `retention_count`, and runs any
        configured post-backup shell hook.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bundle_name = f"taparr-catalog-{timestamp}-{reason}"

        backup_dirs = self.cfg.get("backup_dirs") or [self.bootstrap["paths"]["backup_dir"]]
        retention = max(1, int(self.cfg.get("retention_count", 15)))

        pools = {p.id: p for p in self.db.scalars(select(TapePool)).all()}
        tapes = self.db.scalars(select(Tape).order_by(Tape.barcode)).all()
        filemarks = self.db.scalars(
            select(TapeFilemark).order_by(TapeFilemark.tape_barcode, TapeFilemark.filemark_number)
        ).all()
        files = self.db.scalars(
            select(ArchivedFile).order_by(
                ArchivedFile.tape_barcode, ArchivedFile.filemark_number, ArchivedFile.relative_path
            )
        ).all()

        files_json_data = [
            {
                "id": f.id,
                "title": f.title,
                "tape_barcode": f.tape_barcode,
                "clone_tape_barcode": f.clone_tape_barcode,
                "tape_pool": f.tape_pool_name,
                "filemark_number": f.filemark_number,
                "block_offset": f.block_offset,
                "tar_block_factor": f.tar_block_factor,
                "relative_path": f.relative_path,
                "original_full_path": f.original_full_path,
                "size_bytes": f.size_bytes,
                "sha256_hash": f.sha256_hash,
                "taparr_version": f.taparr_version,
                "tape_format_version": f.tape_format_version,
                "is_superseded": f.is_superseded,
                "archived_at": f.archived_at.isoformat() if f.archived_at else None,
            }
            for f in files
        ]

        tapes_json_data = [
            {
                "barcode": t.barcode,
                "uuid": t.uuid,
                "media_code": t.media_code,
                "generation": t.generation_label,
                "pool_name": pools[t.pool_id].name if t.pool_id in pools else "Unassigned",
                "is_protected_foreign": bool(t.pool_id in pools and pools[t.pool_id].is_protected_foreign),
                "status": t.status,
                "location_type": t.location_type,
                "slot_number": t.slot_number,
                "vault_note": t.vault_note,
                "used_bytes": t.used_bytes,
                "dead_bytes": t.dead_bytes,
                "native_capacity_bytes": t.native_capacity_bytes,
                "total_filemarks": t.total_filemarks,
                "manifest_filemark": t.manifest_filemark,
                "tar_block_factor": t.tar_block_factor,
                "header_taparr_version": t.header_taparr_version,
                "header_format_version": t.header_format_version,
                "clone_of_barcode": t.clone_of_barcode,
                "cloned_to_barcode": t.cloned_to_barcode,
            }
            for t in tapes
        ]

        created_bundles: List[str] = []
        for b_root_str in backup_dirs:
            b_root = Path(b_root_str).resolve()
            b_root.mkdir(parents=True, exist_ok=True)
            bundle_dir = b_root / bundle_name
            bundle_dir.mkdir(parents=True, exist_ok=True)

            # 1. catalog-files.json & catalog-files.csv
            (bundle_dir / "catalog-files.json").write_text(
                json.dumps(
                    {
                        "taparr_version": __version__,
                        "tape_format_version": TAPE_FORMAT_VERSION,
                        "exported_at": datetime.now(timezone.utc).isoformat(),
                        "total_files": len(files_json_data),
                        "files": files_json_data,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            with open(bundle_dir / "catalog-files.csv", "w", newline="", encoding="utf-8") as cf:
                writer = csv.writer(cf)
                writer.writerow(
                    [
                        "tape_barcode",
                        "clone_tape_barcode",
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
                        "is_superseded",
                        "archived_at",
                    ]
                )
                for f in files_json_data:
                    writer.writerow(
                        [
                            f["tape_barcode"],
                            f["clone_tape_barcode"] or "",
                            f["tape_pool"],
                            f["filemark_number"],
                            f["block_offset"],
                            f["tar_block_factor"],
                            f["title"],
                            f["relative_path"],
                            f["original_full_path"],
                            f["size_bytes"],
                            f["sha256_hash"],
                            f["taparr_version"],
                            f["tape_format_version"],
                            f["is_superseded"],
                            f["archived_at"] or "",
                        ]
                    )

            # 2. tapes-inventory.json & tapes-inventory.csv
            (bundle_dir / "tapes-inventory.json").write_text(
                json.dumps(
                    {
                        "taparr_version": __version__,
                        "tape_format_version": TAPE_FORMAT_VERSION,
                        "tapes": tapes_json_data,
                        "filemarks": [
                            {
                                "tape_barcode": fm.tape_barcode,
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
                            }
                            for fm in filemarks
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            with open(bundle_dir / "tapes-inventory.csv", "w", newline="", encoding="utf-8") as tf_csv:
                writer = csv.writer(tf_csv)
                writer.writerow(
                    [
                        "barcode",
                        "media_code",
                        "generation",
                        "pool_name",
                        "status",
                        "location_type",
                        "slot_number",
                        "used_bytes",
                        "total_filemarks",
                        "manifest_filemark",
                        "tar_block_factor",
                        "header_taparr_version",
                        "header_format_version",
                        "clone_of_barcode",
                        "cloned_to_barcode",
                    ]
                )
                for t in tapes_json_data:
                    writer.writerow(
                        [
                            t["barcode"],
                            t["media_code"],
                            t["generation"],
                            t["pool_name"],
                            t["status"],
                            t["location_type"],
                            t["slot_number"] or "",
                            t["used_bytes"],
                            t["total_filemarks"],
                            t["manifest_filemark"] or "",
                            t["tar_block_factor"],
                            t["header_taparr_version"] or "",
                            t["header_format_version"] or "",
                            t["clone_of_barcode"] or "",
                            t["cloned_to_barcode"] or "",
                        ]
                    )

            # 3. Copy standalone recover-bare.sh & generate RECOVERY.md
            recover_sh_src = PROJECT_ROOT / "scripts" / "recover-bare.sh"
            if recover_sh_src.exists():
                dst_sh = bundle_dir / "recover-bare.sh"
                shutil.copy2(recover_sh_src, dst_sh)
                os.chmod(dst_sh, 0o755)

            (bundle_dir / "RECOVERY.md").write_text(
                self._build_recovery_markdown(tapes_json_data),
                encoding="utf-8",
            )

            # 4. PostgreSQL dump (`pg_dump`) or portable DB snapshot
            self._export_db_dump(bundle_dir)

            created_bundles.append(str(bundle_dir))
            self._rotate_old_backups(b_root, retention)

        hook_cmd = (self.cfg.get("post_backup_shell_hook") or "").strip()
        hook_status = None
        if hook_cmd and created_bundles:
            res = subprocess.run(
                ["bash", "-c", hook_cmd],
                env={**os.environ, "TAPARR_BACKUP_DIR": created_bundles[0]},
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            hook_status = {"returncode": res.returncode, "stdout": res.stdout.strip()}

        return {
            "bundle_name": bundle_name,
            "bundle_paths": created_bundles,
            "total_tapes": len(tapes_json_data),
            "total_files": len(files_json_data),
            "taparr_version": __version__,
            "tape_format_version": TAPE_FORMAT_VERSION,
            "hook_status": hook_status,
        }

    def list_backups(self) -> List[Dict[str, Any]]:
        backup_dirs = self.cfg.get("backup_dirs") or [self.bootstrap["paths"]["backup_dir"]]
        results = []
        for b_root_str in backup_dirs:
            b_root = Path(b_root_str).resolve()
            if not b_root.exists():
                continue
            for d in sorted(b_root.glob("taparr-catalog-*"), reverse=True):
                if d.is_dir():
                    files_in_dir = [p.name for p in sorted(d.iterdir())]
                    results.append(
                        {
                            "name": d.name,
                            "path": str(d),
                            "created_at": datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc).isoformat(),
                            "files": files_in_dir,
                        }
                    )
        return results

    def _export_db_dump(self, bundle_dir: Path) -> None:
        db_url = self.bootstrap["database"]["url"]
        dump_path = bundle_dir / "taparr-pgdump.sql"
        pg_dump_bin = shutil.which("pg_dump")
        if db_url.startswith("postgresql") and pg_dump_bin:
            clean_url = db_url.replace("postgresql+psycopg://", "postgresql://")
            res = subprocess.run(
                [pg_dump_bin, "--dbname", clean_url, "--file", str(dump_path)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if res.returncode == 0 and dump_path.exists():
                return

        # Portable SQL comment + JSON dump header if pg_dump was not invoked
        dump_path.write_text(
            f"-- Taparr Database Portable Snapshot (Taparr v{__version__}, Format v{TAPE_FORMAT_VERSION})\n"
            f"-- Generated at {datetime.now(timezone.utc).isoformat()}\n"
            f"-- See catalog-files.json and tapes-inventory.json in this bundle for complete table records.\n",
            encoding="utf-8",
        )

    @staticmethod
    def _rotate_old_backups(b_root: Path, retention: int) -> None:
        bundles = sorted(
            [d for d in b_root.glob("taparr-catalog-*") if d.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old_dir in bundles[retention:]:
            shutil.rmtree(old_dir, ignore_errors=True)

    @staticmethod
    def _build_recovery_markdown(tapes_data: List[Dict[str, Any]]) -> str:
        return f"""# Taparr Bare-Metal Disaster Recovery Guide

Generated by **Taparr v{__version__}** (`tape_format_version: {TAPE_FORMAT_VERSION}`).

Even without Taparr installed, every cartridge written by Taparr uses standard, unencrypted
POSIX/GNU `tar` archives separated by hardware EOF filemarks (`mt-st`).

## 1. Finding Your File
Search `catalog-files.csv` or `catalog-files.json` using `grep`:
```bash
grep -i "Movie or Series Name" catalog-files.csv
```
Note the columns:
- **`tape_barcode`** (and **`clone_tape_barcode`** if cloned)
- **`filemark_number`** (e.g., `1`, `2`, `42`)
- **`tar_block_factor`** (default `2048` = 1 MiB blocks)

## 2. Loading & Extracting Using `recover-bare.sh`
```bash
# Load cartridge from Slot <SLOT> into Drive 0:
./recover-bare.sh --load /dev/sg2 <SLOT> 0

# Inspect the 1 KB fm0 versioned tape header:
./recover-bare.sh --inspect-header /dev/nst0

# Extract Filemark #<FM> to /mnt/restore:
./recover-bare.sh --extract /dev/nst0 <FM> /mnt/restore 2048
```

## 3. Manual Commands Using Standard Linux `mtx`, `mt`, and `tar`
```bash
# 1. Rewind tape to Beginning of Tape (BOT / fm0):
mt -f /dev/nst0 rewind

# 2. Read the 1 KB fm0 Taparr Header (contains taparr_version & tape_format_version):
dd if=/dev/nst0 bs=1024 count=1

# 3. Fast-forward to Filemark <FM> from BOT:
mt -f /dev/nst0 rewind
mt -f /dev/nst0 fsf <FM>

# 4. Extract the TAR stream:
mkdir -p /mnt/restore
tar -b 2048 -xvf /dev/nst0 -C /mnt/restore
```

## 4. Reading the Self-Describing Manifest Directly from a Tape
Every Taparr cartridge also stores its own complete JSON + CSV manifest at its final filemark (`manifest_filemark` in `tapes-inventory.csv`):
```bash
./recover-bare.sh --read-manifest /dev/nst0 <MANIFEST_FM> ./manifest_out 2048
```
"""
