"""
Background Worker & Periodic Maintenance Manager for Taparr.
Provides:
  1. Asynchronous background thread execution for multi-hour LTO tape batches so HTTP calls never time out.
  2. Automatic HP MSL4048 Idle Cooldown Power-Off evaluation (`idle_cooldown_minutes`).
  3. Periodic Sonarr / Radarr `to-tape` tag polling sync.
"""

import threading
from datetime import datetime, timezone
from typing import Dict, Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

import taparr.database as db_mod
from taparr.models import ArrInstance, TapeDrive, TapeJob
from taparr.services.arr_client import ArrService
from taparr.services.power import LibraryPowerManager
from taparr.services.scheduler import TapeJobScheduler


def spawn_background_job(task_fn: Callable[[Session], Any]) -> str:
    """
    Runs `task_fn(db)` in a background daemon thread with its own SQLAlchemy session.
    """
    thread_id = f"taparr-bg-{int(datetime.now(timezone.utc).timestamp() * 1000)}"

    def _runner():
        with db_mod.SessionLocal() as bg_db:
            try:
                task_fn(bg_db)
            except Exception:
                pass

    t = threading.Thread(target=_runner, name=thread_id, daemon=True)
    t.start()
    return thread_id


def check_idle_cooldown_and_auto_power_off(db: Session) -> Dict[str, Any]:
    """
    Evaluates whether the HP MSL4048 has been idle longer than `idle_cooldown_minutes`.
    If so (and Maintenance Lock is off, and no jobs/drives are active), safely unloads all
    drives and powers off the tape library.
    """
    pwr_cfg = db_mod.get_setting(db, "power")
    if not pwr_cfg.get("auto_power_enabled", True):
        return {"action": "none", "reason": "auto_power_disabled"}
    if pwr_cfg.get("power_state") != "ONLINE":
        return {"action": "none", "reason": "already_offline"}
    if pwr_cfg.get("maintenance_lock", False):
        return {"action": "none", "reason": "maintenance_lock_active"}

    drives = db.scalars(select(TapeDrive)).all()
    if any(d.status not in ("IDLE", "OFFLINE") for d in drives):
        return {"action": "none", "reason": "drive_active"}

    active_jobs = db.scalars(
        select(TapeJob).where(TapeJob.status.in_(["POWERING_ON", "STAGING_SSD", "RUNNING", "VERIFYING"]))
    ).all()
    if active_jobs:
        return {"action": "none", "reason": "job_active"}

    cooldown_minutes = float(pwr_cfg.get("idle_cooldown_minutes", 20))
    now = datetime.now(timezone.utc)

    last_job = db.scalar(
        select(TapeJob).where(TapeJob.completed_at.is_not(None)).order_by(TapeJob.completed_at.desc())
    )
    last_activity = None
    if last_job and last_job.completed_at:
        last_activity = last_job.completed_at
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=timezone.utc)
    elif pwr_cfg.get("last_powered_on_at"):
        try:
            last_activity = datetime.fromisoformat(pwr_cfg["last_powered_on_at"])
        except ValueError:
            last_activity = None

    if last_activity is None:
        return {"action": "none", "reason": "no_activity_timestamp_yet"}

    idle_minutes = (now - last_activity).total_seconds() / 60.0
    if idle_minutes >= cooldown_minutes:
        res = LibraryPowerManager(db).power_off_library(force=False)
        return {"action": "powered_off", "idle_minutes": round(idle_minutes, 2), "detail": res}

    return {
        "action": "none",
        "reason": "cooldown_not_elapsed",
        "idle_minutes": round(idle_minutes, 2),
        "cooldown_minutes": cooldown_minutes,
    }


def poll_all_arr_instances(db: Session) -> Dict[str, Any]:
    """
    Polls all enabled Sonarr and Radarr instances for library updates and `to-tape` tags.
    """
    arr = ArrService(db)
    instances = db.scalars(select(ArrInstance).where(ArrInstance.enabled.is_(True))).all()
    results = {}
    for inst in instances:
        results[inst.name] = arr.sync_instance(inst)
    return {"polled_instances": len(instances), "results": results}

