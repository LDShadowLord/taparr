"""
Pipelined Local SSD Scratch Stager & Inline SHA-256 Hasher.
Buffers media items on fast local NVMe/SSD storage before streaming to LTO tape
(and during tape extraction on restore) to prevent tape shoe-shining, while
respecting a configurable Max Scratch GB cap.
"""

import hashlib
import os
import shutil
from pathlib import Path
from typing import Dict, Any, List, Tuple
from sqlalchemy.orm import Session

from taparr.database import get_setting
from taparr.models import MediaItem, ArchivedFile


class ScratchSpaceExceededError(RuntimeError):
    """Raised when a single logical item exceeds the entire configured SSD scratch cap."""


class ScratchPipelineManager:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_setting(db, "scratch")
        self.scratch_root = Path(self.settings.get("scratch_dir", "./data/scratch")).resolve()
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self.max_scratch_bytes = int(float(self.settings.get("max_scratch_gb", 200)) * (1024**3))

    def get_scratch_telemetry(self) -> Dict[str, Any]:
        used_bytes = 0
        file_count = 0
        if self.scratch_root.exists():
            for p in self.scratch_root.rglob("*"):
                if p.is_file():
                    used_bytes += p.stat().st_size
                    file_count += 1
        return {
            "scratch_dir": str(self.scratch_root),
            "used_bytes": used_bytes,
            "max_scratch_bytes": self.max_scratch_bytes,
            "free_scratch_bytes": max(0, self.max_scratch_bytes - used_bytes),
            "usage_pct": round((used_bytes / self.max_scratch_bytes) * 100.0, 2) if self.max_scratch_bytes > 0 else 0.0,
            "staged_files_count": file_count,
        }

    def can_fit_in_scratch(self, item_size_bytes: int) -> bool:
        telemetry = self.get_scratch_telemetry()
        return (telemetry["used_bytes"] + item_size_bytes) <= self.max_scratch_bytes

    def compute_source_fingerprint(self, folder_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Scans `folder_path` (excluding `.taparr_stub.mkv` stub slates) and computes SHA-256
        for every file plus a deterministic composite fingerprint.
        Used both for staging and for verifying whether an `ON_DISK_AND_TAPE` item is unchanged
        for instant re-eviction.
        """
        manifest: List[Dict[str, Any]] = []
        if not folder_path.exists():
            return "", manifest

        for src_file in sorted(folder_path.rglob("*")):
            if not src_file.is_file():
                continue
            if src_file.name.endswith(".taparr_stub.mkv") or src_file.name == ".taparr_meta.json":
                continue
            rel = str(src_file.relative_to(folder_path))
            h = hashlib.sha256()
            size = 0
            with open(src_file, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
                    size += len(chunk)
            manifest.append(
                {
                    "relative_path": rel,
                    "original_full_path": str(src_file.resolve()),
                    "size_bytes": size,
                    "sha256_hash": h.hexdigest(),
                }
            )

        composite = hashlib.sha256(
            "|".join(f"{m['relative_path']}:{m['size_bytes']}:{m['sha256_hash']}" for m in manifest).encode("utf-8")
        ).hexdigest()
        return composite, manifest

    def stage_item_for_archive(self, media_item: MediaItem) -> Tuple[Path, List[Dict[str, Any]], str]:
        """
        Copies `media_item.folder_path` from NAS to the local SSD scratch directory
        while computing inline SHA-256 hashes for every file.
        Enforces `max_scratch_bytes`.
        """
        src_dir = Path(media_item.folder_path)
        if not src_dir.exists():
            raise FileNotFoundError(f"Source media folder does not exist: {src_dir}")

        stage_dir = self.scratch_root / f"archive_item_{media_item.id}"
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)

        manifest: List[Dict[str, Any]] = []
        total_copied = 0
        current_used = self.get_scratch_telemetry()["used_bytes"]

        for src_file in sorted(src_dir.rglob("*")):
            if not src_file.is_file():
                continue
            if src_file.name.endswith(".taparr_stub.mkv") or src_file.name == ".taparr_meta.json":
                continue

            f_size = src_file.stat().st_size
            if (current_used + total_copied + f_size) > self.max_scratch_bytes:
                shutil.rmtree(stage_dir, ignore_errors=True)
                raise ScratchSpaceExceededError(
                    f"Staging '{media_item.title}' ({total_copied + f_size} bytes) would exceed "
                    f"configured SSD scratch cap ({self.max_scratch_bytes} bytes)."
                )

            rel = src_file.relative_to(src_dir)
            dst_file = stage_dir / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)

            h = hashlib.sha256()
            copied_size = 0
            with open(src_file, "rb") as rf, open(dst_file, "wb") as wf:
                while chunk := rf.read(1024 * 1024):
                    wf.write(chunk)
                    h.update(chunk)
                    copied_size += len(chunk)

            shutil.copystat(src_file, dst_file)
            total_copied += copied_size
            manifest.append(
                {
                    "relative_path": str(rel),
                    "original_full_path": str(src_file.resolve()),
                    "size_bytes": copied_size,
                    "sha256_hash": h.hexdigest(),
                }
            )

        composite = hashlib.sha256(
            "|".join(f"{m['relative_path']}:{m['size_bytes']}:{m['sha256_hash']}" for m in manifest).encode("utf-8")
        ).hexdigest()

        media_item.size_bytes = total_copied
        media_item.file_count = len(manifest)
        media_item.content_fingerprint_sha256 = composite
        self.db.commit()

        return stage_dir, manifest, composite

    def cleanup_staged_item(self, stage_dir: Path) -> None:
        if stage_dir and stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)

    def get_restore_stage_dir(self, media_item_id: int) -> Path:
        d = self.scratch_root / f"restore_item_{media_item_id}"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def verify_and_unstage_restore(
        self,
        media_item: MediaItem,
        restore_stage_dir: Path,
        expected_files: List[ArchivedFile],
    ) -> Dict[str, Any]:
        """
        Verifies the SHA-256 checksums of all files extracted from tape into `restore_stage_dir`
        on the local SSD scratch, removes any `.taparr_stub.mkv` slate on the NAS, and moves
        the verified files back to `media_item.folder_path`.
        """
        verified_count = 0
        bytes_restored = 0

        for exp in expected_files:
            staged_file = restore_stage_dir / exp.relative_path
            if not staged_file.exists():
                raise FileNotFoundError(
                    f"Restored file missing from TAR extraction: {exp.relative_path}"
                )
            h = hashlib.sha256()
            with open(staged_file, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
            actual_hash = h.hexdigest()
            if actual_hash != exp.sha256_hash:
                raise ValueError(
                    f"SHA-256 mismatch on restored file '{exp.relative_path}': "
                    f"expected {exp.sha256_hash}, got {actual_hash}"
                )
            verified_count += 1
            bytes_restored += staged_file.stat().st_size

        dest_dir = Path(media_item.folder_path)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Remove any playable .mkv stub or .taparr_meta.json from destination before moving real files
        for p in list(dest_dir.rglob("*")):
            if p.is_file() and (p.name.endswith(".taparr_stub.mkv") or p.name == ".taparr_meta.json"):
                p.unlink(missing_ok=True)
        if media_item.stub_file_path and os.path.exists(media_item.stub_file_path):
            try:
                os.unlink(media_item.stub_file_path)
            except OSError:
                pass

        # Move verified files from SSD scratch to NAS destination folder
        for staged_file in sorted(restore_stage_dir.rglob("*")):
            if staged_file.is_file():
                rel = staged_file.relative_to(restore_stage_dir)
                target_path = dest_dir / rel
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged_file), str(target_path))

        shutil.rmtree(restore_stage_dir, ignore_errors=True)
        return {
            "verified_files": verified_count,
            "bytes_restored": bytes_restored,
            "destination": str(dest_dir),
        }
