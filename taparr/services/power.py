"""
HP MSL4048 Tape Library Power & Idle Cooldown Manager.
Handles:
  - Wake-on-Job via HTTP Webhook (Home Assistant, Shelly, Tasmota) or Shell Script
  - Optional Linux SCSI bus rescan & `mtx status` boot inventory readiness polling
  - Safe Auto-Power-Off after Idle Cooldown (rewinds & unloads all drives first!)
  - Maintenance Lock override in the WebUI
"""

import subprocess
from datetime import datetime, timezone
from typing import Dict, Any
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from taparr.database import get_setting, update_setting
from taparr.models import TapeDrive, TapeJob
from taparr.services.hardware import MSL4048HardwareManager


class LibraryPowerManager:
    def __init__(self, db: Session):
        self.db = db

    def get_status(self) -> Dict[str, Any]:
        cfg = get_setting(self.db, "power")
        drives = self.db.scalars(select(TapeDrive)).all()
        any_loaded = any(bool(d.loaded_barcode) for d in drives)
        any_busy = any(d.status not in ("IDLE", "OFFLINE") for d in drives)
        return {
            "auto_power_enabled": bool(cfg.get("auto_power_enabled", True)),
            "power_state": cfg.get("power_state", "ONLINE"),
            "maintenance_lock": bool(cfg.get("maintenance_lock", False)),
            "idle_cooldown_minutes": int(cfg.get("idle_cooldown_minutes", 20)),
            "boot_inventory_timeout_seconds": int(cfg.get("boot_inventory_timeout_seconds", 360)),
            "power_on_mode": cfg.get("power_on_mode", "webhook"),
            "power_off_mode": cfg.get("power_off_mode", "webhook"),
            "any_drive_loaded": any_loaded,
            "any_drive_busy": any_busy,
        }

    def set_maintenance_lock(self, locked: bool) -> Dict[str, Any]:
        update_setting(self.db, "power", {"maintenance_lock": bool(locked)})
        return self.get_status()

    def ensure_library_online(self) -> Dict[str, Any]:
        """
        Called automatically before any tape batch, restore, clone, or compaction job.
        If the MSL4048 is OFFLINE, triggers the configured Power-On webhook/script,
        polls `mtx status` until the 48-slot barcode scan completes, and marks ONLINE.
        """
        cfg = get_setting(self.db, "power")
        if cfg.get("power_state") == "ONLINE":
            return {"status": "already_online", "power_state": "ONLINE"}

        update_setting(self.db, "power", {"power_state": "BOOTING"})
        hook_result = self._execute_power_hook(
            mode=cfg.get("power_on_mode", "webhook"),
            webhook_url=cfg.get("power_on_webhook_url", ""),
            webhook_method=cfg.get("power_on_webhook_method", "POST"),
            webhook_body=cfg.get("power_on_webhook_body", '{"state": "ON"}'),
            shell_cmd=cfg.get("power_on_shell_cmd", ""),
        )

        hw = MSL4048HardwareManager(self.db)
        hw.run_inventory()

        update_setting(
            self.db,
            "power",
            {
                "power_state": "ONLINE",
                "last_powered_on_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {"status": "powered_on", "power_state": "ONLINE", "hook": hook_result}

    def power_off_library(self, force: bool = False) -> Dict[str, Any]:
        """
        Safely powers off the HP MSL4048:
          1. Verifies Maintenance Lock is not active (unless force=True).
          2. Verifies no drives or jobs are actively writing/reading.
          3. Unloads any cartridges currently in Drive 0 or Drive 1 back to their storage slots.
          4. Fires the configured Power-Off webhook or shell command.
        """
        cfg = get_setting(self.db, "power")
        if cfg.get("maintenance_lock") and not force:
            raise RuntimeError(
                "Cannot power off HP MSL4048 while Maintenance Lock (Keep Awake) is enabled."
            )

        drives = self.db.scalars(select(TapeDrive)).all()
        for d in drives:
            if d.status not in ("IDLE", "OFFLINE") and not force:
                raise RuntimeError(
                    f"Cannot power off library: Drive {d.drive_index} ({d.name}) is currently {d.status}."
                )

        active_jobs = self.db.scalars(
            select(TapeJob).where(TapeJob.status.in_(["POWERING_ON", "STAGING_SSD", "RUNNING", "VERIFYING"]))
        ).all()
        if active_jobs and not force:
            raise RuntimeError("Cannot power off library while a tape job is actively running.")

        # Critical safety step: unload all drives back to their storage slots before cutting power
        hw = MSL4048HardwareManager(self.db)
        unloaded_drives = []
        for d in drives:
            if d.loaded_barcode:
                res = hw.unload_drive(d)
                unloaded_drives.append(res)

        hook_result = self._execute_power_hook(
            mode=cfg.get("power_off_mode", "webhook"),
            webhook_url=cfg.get("power_off_webhook_url", ""),
            webhook_method=cfg.get("power_off_webhook_method", "POST"),
            webhook_body=cfg.get("power_off_webhook_body", '{"state": "OFF"}'),
            shell_cmd=cfg.get("power_off_shell_cmd", ""),
        )

        update_setting(
            self.db,
            "power",
            {
                "power_state": "OFFLINE",
                "last_powered_off_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {
            "status": "powered_off",
            "power_state": "OFFLINE",
            "unloaded_drives": unloaded_drives,
            "hook": hook_result,
        }

    @staticmethod
    def _execute_power_hook(
        mode: str,
        webhook_url: str,
        webhook_method: str,
        webhook_body: str,
        shell_cmd: str,
    ) -> Dict[str, Any]:
        if mode == "shell" and shell_cmd.strip():
            res = subprocess.run(
                ["bash", "-c", shell_cmd],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            return {"mode": "shell", "returncode": res.returncode, "stdout": res.stdout.strip()}
        if mode == "webhook" and webhook_url.strip():
            try:
                with httpx.Client(timeout=15.0) as client:
                    resp = client.request(
                        method=webhook_method.upper() or "POST",
                        url=webhook_url.strip(),
                        content=webhook_body.encode("utf-8") if webhook_body else None,
                        headers={"Content-Type": "application/json"},
                    )
                    return {"mode": "webhook", "status_code": resp.status_code}
            except Exception as exc:
                return {"mode": "webhook", "error": str(exc)}
        return {"mode": "noop", "detail": "No webhook URL or shell command configured; state updated."}
