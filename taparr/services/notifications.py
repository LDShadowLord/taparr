"""
Multi-Channel System Notification Dispatcher (Discord, Gotify, ntfy, Generic Webhook).
"""

from typing import Dict, Any
import httpx
from sqlalchemy.orm import Session

from taparr.database import get_setting


class NotificationDispatcher:
    def __init__(self, db: Session):
        self.db = db
        self.cfg = get_setting(db, "notifications")

    def send_event(self, event_type: str, title: str, message: str, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        if not self.cfg.get("enabled"):
            return {"sent": False, "reason": "notifications_disabled", "event_type": event_type}

        dispatched = []
        discord_url = (self.cfg.get("discord_webhook_url") or "").strip()
        if discord_url:
            try:
                with httpx.Client(timeout=10.0) as client:
                    client.post(discord_url, json={"content": f"**[Taparr] {title}**\n{message}"})
                dispatched.append("discord")
            except Exception:
                pass

        ntfy_url = (self.cfg.get("ntfy_url") or "").strip()
        if ntfy_url:
            try:
                with httpx.Client(timeout=10.0) as client:
                    client.post(ntfy_url, content=message.encode("utf-8"), headers={"Title": f"Taparr: {title}"})
                dispatched.append("ntfy")
            except Exception:
                pass

        generic_url = (self.cfg.get("generic_webhook_url") or "").strip()
        if generic_url:
            try:
                with httpx.Client(timeout=10.0) as client:
                    client.post(
                        generic_url,
                        json={
                            "event_type": event_type,
                            "title": title,
                            "message": message,
                            "extra": extra or {},
                        },
                    )
                dispatched.append("webhook")
            except Exception:
                pass

        return {"sent": bool(dispatched), "channels": dispatched, "event_type": event_type}
