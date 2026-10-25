"""
In-WebUI Seamless Updater & CHANGELOG.md Manager.
Provides:
  - Structured parsing of `CHANGELOG.md` and Semantic Version tracking
  - Pre-update active-drive safety checks (prevents interrupting an active tape write/restore)
  - Execution of `scripts/update.sh` from inside the WebUI with pre-update catalog backup
"""

import re
import subprocess
from pathlib import Path
from typing import Dict, Any, List
from sqlalchemy import select
from sqlalchemy.orm import Session

from taparr import __version__, TAPE_FORMAT_VERSION
from taparr.config import PROJECT_ROOT
from taparr.models import TapeDrive, TapeJob
from taparr.services.catalog_backup import CatalogBackupExporter


class UpdateManager:
    def __init__(self, db: Session):
        self.db = db
        self.changelog_path = PROJECT_ROOT / "CHANGELOG.md"
        self.update_script_path = PROJECT_ROOT / "scripts" / "update.sh"

    def get_version_and_changelog(self) -> Dict[str, Any]:
        raw_md = ""
        if self.changelog_path.exists():
            raw_md = self.changelog_path.read_text(encoding="utf-8")

        entries = self._parse_changelog_sections(raw_md)
        check_info = self._run_script_check()

        drives = self.db.scalars(select(TapeDrive)).all()
        busy_drives = [d.drive_index for d in drives if d.status not in ("IDLE", "OFFLINE")]

        return {
            "taparr_version": __version__,
            "tape_format_version": TAPE_FORMAT_VERSION,
            "git_commit": check_info.get("git_commit", "local"),
            "git_branch": check_info.get("git_branch", "main"),
            "upstream_commit": check_info.get("upstream_commit", "local"),
            "update_available": check_info.get("git_commit") != check_info.get("upstream_commit"),
            "safe_to_update": len(busy_drives) == 0,
            "busy_drives": busy_drives,
            "changelog_markdown": raw_md,
            "releases": entries,
        }

    def trigger_update(self, dry_run: bool = False, force: bool = False) -> Dict[str, Any]:
        """
        Executes `scripts/update.sh` from the WebUI after verifying no tape drives are busy
        and creating a pre-update Disaster Recovery Catalog Backup.
        """
        drives = self.db.scalars(select(TapeDrive)).all()
        busy = [d for d in drives if d.status not in ("IDLE", "OFFLINE")]
        if busy and not force:
            raise RuntimeError(
                f"Cannot run update while tape drive(s) {[d.drive_index for d in busy]} are active ({busy[0].status})."
            )

        active_jobs = self.db.scalars(
            select(TapeJob).where(TapeJob.status.in_(["POWERING_ON", "STAGING_SSD", "RUNNING", "VERIFYING"]))
        ).all()
        if active_jobs and not force:
            raise RuntimeError("Cannot run update while a tape batch job is actively running.")

        # Always export a pre-update catalog backup first
        backup_info = CatalogBackupExporter(self.db).export_backup_bundle(reason="pre-update")

        args = [str(self.update_script_path), "--no-restart"]
        if dry_run:
            args.append("--dry-run")

        res = subprocess.run(
            args,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

        return {
            "status": "completed" if res.returncode == 0 else "failed",
            "returncode": res.returncode,
            "dry_run": dry_run,
            "taparr_version": __version__,
            "tape_format_version": TAPE_FORMAT_VERSION,
            "pre_update_backup": backup_info["bundle_name"],
            "log_output": (res.stdout + "\n" + res.stderr).strip(),
        }

    def _run_script_check(self) -> Dict[str, str]:
        if not self.update_script_path.exists():
            return {}
        res = subprocess.run(
            [str(self.update_script_path), "--check"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        info: Dict[str, str] = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        return info

    @staticmethod
    def _parse_changelog_sections(md_text: str) -> List[Dict[str, Any]]:
        releases: List[Dict[str, Any]] = []
        header_re = re.compile(r"^##\s+\[([^\]]+)\]\s*-\s*(\S+)", re.MULTILINE)
        matches = list(header_re.finditer(md_text))
        for idx, m in enumerate(matches):
            ver = m.group(1)
            date_str = m.group(2)
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
            body = md_text[start:end].strip()
            releases.append({"version": ver, "date": date_str, "notes": body})
        return releases
