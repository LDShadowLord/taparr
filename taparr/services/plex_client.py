"""
Plex Media Server Integration:
  1. Targeted Partial Library Folder Refresh (`GET /library/sections/{id}/refresh?path=...`)
  2. Stub Playback Webhook Handler (`/api/v1/webhooks/plex`) that detects when a user plays
     an 'Archived on Tape' `.taparr_stub.mkv` video card and queues a restore or sends an alert.
"""

from typing import Dict, Any
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from taparr.database import get_setting
from taparr.models import MediaItem
from taparr.services.notifications import NotificationDispatcher


class PlexService:
    def __init__(self, db: Session):
        self.db = db
        self.cfg = get_setting(db, "plex")

    def refresh_folder(self, folder_path: str) -> Dict[str, Any]:
        """
        Triggers a targeted partial scan of `folder_path` in Plex so Plex immediately
        updates the stub or restored media without scanning the entire NAS.
        """
        if not self.cfg.get("enabled") or not self.cfg.get("token"):
            return {"status": "skipped", "reason": "Plex integration not enabled"}

        base_url = self.cfg.get("url", "http://localhost:32400").rstrip("/")
        token = self.cfg.get("token", "")

        try:
            with httpx.Client(timeout=10.0) as client:
                sec_resp = client.get(
                    f"{base_url}/library/sections",
                    headers={"X-Plex-Token": token, "Accept": "application/json"},
                )
                sec_resp.raise_for_status()
                sections = sec_resp.json().get("MediaContainer", {}).get("Directory", [])
                for sec in sections:
                    sec_key = sec.get("key")
                    client.get(
                        f"{base_url}/library/sections/{sec_key}/refresh",
                        params={"path": folder_path, "X-Plex-Token": token},
                    )
                return {"status": "refreshed", "folder_path": folder_path}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    def handle_playback_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Processes incoming Plex / Tautulli webhook payloads.
        If the played item matches a stubbed/archived `MediaItem`, triggers a notification
        and optionally queues it for restore (`QUEUED_RESTORE`).
        """
        event = payload.get("event", "media.play")
        if event not in ("media.play", "media.resume", "playback.start"):
            return {"status": "ignored", "event": event}

        metadata = payload.get("Metadata", {})
        title = metadata.get("title") or payload.get("title") or ""
        grandparent_title = metadata.get("grandparentTitle") or ""
        file_hint = payload.get("file") or metadata.get("file") or ""

        matched_item = None
        if file_hint:
            matched_item = self.db.scalar(
                select(MediaItem).where(MediaItem.stub_file_path == file_hint)
            )
        if not matched_item and title:
            stmt = select(MediaItem).where(
                MediaItem.status.in_(["ON_TAPE_STUBBED", "ON_TAPE_ONLY"])
            )
            for candidate in self.db.scalars(stmt).all():
                if (
                    title.lower() in candidate.title.lower()
                    or (grandparent_title and grandparent_title.lower() in candidate.title.lower())
                ):
                    matched_item = candidate
                    break

        if not matched_item:
            return {"status": "no_archived_match", "title": title}

        action = self.cfg.get("webhook_stub_action", "notify_and_queue")
        queued = False
        if action == "notify_and_queue" and matched_item.status in ("ON_TAPE_STUBBED", "ON_TAPE_ONLY"):
            matched_item.status = "QUEUED_RESTORE"
            self.db.commit()
            queued = True

        notifier = NotificationDispatcher(self.db)
        notifier.send_event(
            event_type="plex_stub_played",
            title=f"Plex Playback Attempt on Archived Media: {matched_item.title}",
            message=(
                f"User attempted to play '{matched_item.title}' on Tape {matched_item.primary_tape_barcode} "
                f"(FM #{matched_item.primary_filemark}). Auto-queued for restore: {queued}."
            ),
        )

        return {
            "status": "handled",
            "media_item_id": matched_item.id,
            "title": matched_item.title,
            "tape_barcode": matched_item.primary_tape_barcode,
            "filemark": matched_item.primary_filemark,
            "queued_for_restore": queued,
        }
