"""
Multi-Instance Sonarr (TV / Anime) & Radarr (Movies) v3/v4 API Client + Unmanaged Folder Scanner.
Handles:
  - Library syncing & path mapping (`remote_path_prefix` -> `local_path_prefix`)
  - Automatic detection of the `to-tape` tag to queue items for archival
  - Post-archive API calls: Unmonitor Movie/Season + apply `on-tape` tag
  - Post-restore API calls: Rescan Movie/Series + remove `on-tape` tag
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from taparr.models import ArrInstance, MediaItem, TapePool


class ArrService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def map_path(instance: ArrInstance, remote_path: str) -> str:
        if (
            instance.remote_path_prefix
            and instance.local_path_prefix
            and remote_path.startswith(instance.remote_path_prefix)
        ):
            suffix = remote_path[len(instance.remote_path_prefix) :].lstrip("/")
            return str(Path(instance.local_path_prefix) / suffix)
        return remote_path

    def register_unmanaged_folder(
        self,
        folder_path: str,
        title: Optional[str] = None,
        media_category: str = "Unmanaged",
        target_pool_id: Optional[int] = None,
        year: Optional[int] = None,
    ) -> MediaItem:
        """
        Registers or updates a folder on disk (either managed or unmanaged) as a MediaItem.
        """
        p = Path(folder_path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Folder not found on disk: {p}")

        size_bytes = 0
        file_count = 0
        for f in p.rglob("*"):
            if f.is_file() and not f.name.endswith(".taparr_stub.mkv") and f.name != ".taparr_meta.json":
                size_bytes += f.stat().st_size
                file_count += 1

        existing = self.db.scalar(select(MediaItem).where(MediaItem.folder_path == str(p)))
        if existing:
            existing.title = title or existing.title
            existing.media_category = media_category
            existing.size_bytes = size_bytes
            existing.file_count = file_count
            if target_pool_id is not None:
                existing.target_pool_id = target_pool_id
            self.db.commit()
            self.db.refresh(existing)
            return existing

        if target_pool_id is None:
            pool = self.db.scalar(select(TapePool).where(TapePool.name == media_category))
            if not pool:
                pool = self.db.scalar(select(TapePool).where(TapePool.name == "Movies"))
            target_pool_id = pool.id if pool else None

        item = MediaItem(
            source_type="unmanaged",
            arr_instance_id=None,
            title=title or p.name,
            sort_title=(title or p.name).lower(),
            year=year,
            media_category=media_category,
            folder_path=str(p),
            size_bytes=size_bytes,
            file_count=file_count,
            monitored=True,
            tags=[],
            status="ON_DISK",
            target_pool_id=target_pool_id,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def sync_instance(self, instance: ArrInstance) -> Dict[str, Any]:
        """
        Queries a live Sonarr or Radarr instance API to synchronize movies/seasons
        and auto-queues any item tagged with `instance.to_tape_tag` (e.g. `to-tape`).
        """
        headers = {"X-Api-Key": instance.api_key}
        base_url = instance.url.rstrip("/")
        synced_count = 0
        auto_queued_count = 0

        try:
            with httpx.Client(timeout=20.0, headers=headers) as client:
                tags_resp = client.get(f"{base_url}/api/v3/tag")
                tags_resp.raise_for_status()
                tag_map = {t["id"]: t["label"] for t in tags_resp.json()}

                if instance.arr_type.lower() == "radarr":
                    movies_resp = client.get(f"{base_url}/api/v3/movie")
                    movies_resp.raise_for_status()
                    for m in movies_resp.json():
                        local_path = self.map_path(instance, m.get("path", ""))
                        labels = [tag_map.get(tid, str(tid)) for tid in m.get("tags", [])]
                        item = self._upsert_arr_item(
                            instance=instance,
                            external_id=m["id"],
                            season_number=None,
                            title=m.get("title", "Unknown Movie"),
                            year=m.get("year"),
                            folder_path=local_path,
                            size_bytes=int(m.get("sizeOnDisk", 0)),
                            monitored=bool(m.get("monitored", True)),
                            tags=labels,
                            media_category="Movies",
                        )
                        synced_count += 1
                        if instance.to_tape_tag in labels and item.status == "ON_DISK":
                            item.status = "QUEUED_ARCHIVE"
                            auto_queued_count += 1
                else:
                    series_resp = client.get(f"{base_url}/api/v3/series")
                    series_resp.raise_for_status()
                    for s in series_resp.json():
                        base_path = self.map_path(instance, s.get("path", ""))
                        labels = [tag_map.get(tid, str(tid)) for tid in s.get("tags", [])]
                        cat = "Anime" if s.get("seriesType") == "anime" else "TV Shows"
                        for season in s.get("seasons", []):
                            s_num = season.get("seasonNumber", 0)
                            if s_num == 0:
                                continue
                            stats = season.get("statistics", {})
                            s_size = int(stats.get("sizeOnDisk", 0))
                            if s_size == 0:
                                continue
                            season_path = str(Path(base_path) / f"Season {s_num:02d}")
                            item = self._upsert_arr_item(
                                instance=instance,
                                external_id=s["id"],
                                season_number=s_num,
                                title=f"{s.get('title')} - Season {s_num:02d}",
                                year=s.get("year"),
                                folder_path=season_path,
                                size_bytes=s_size,
                                monitored=bool(season.get("monitored", True)),
                                tags=labels,
                                media_category=cat,
                            )
                            synced_count += 1
                            if instance.to_tape_tag in labels and item.status == "ON_DISK":
                                item.status = "QUEUED_ARCHIVE"
                                auto_queued_count += 1
        except Exception as exc:
            return {"status": "offline_or_mock", "detail": str(exc), "synced": synced_count}

        instance.last_sync_at = datetime.now(timezone.utc)
        self.db.commit()
        return {"status": "ok", "synced": synced_count, "auto_queued": auto_queued_count}

    def _upsert_arr_item(
        self,
        instance: ArrInstance,
        external_id: int,
        season_number: Optional[int],
        title: str,
        year: Optional[int],
        folder_path: str,
        size_bytes: int,
        monitored: bool,
        tags: List[str],
        media_category: str,
    ) -> MediaItem:
        stmt = select(MediaItem).where(
            MediaItem.arr_instance_id == instance.id,
            MediaItem.external_id == external_id,
            MediaItem.season_number == season_number,
        )
        existing = self.db.scalar(stmt)
        if existing:
            existing.title = title
            existing.year = year
            existing.folder_path = folder_path
            if existing.status == "ON_DISK" and size_bytes > 0:
                existing.size_bytes = size_bytes
            existing.monitored = monitored
            existing.tags = tags
            self.db.commit()
            return existing

        item = MediaItem(
            source_type=instance.arr_type.lower(),
            arr_instance_id=instance.id,
            external_id=external_id,
            season_number=season_number,
            title=title,
            sort_title=title.lower(),
            year=year,
            media_category=media_category,
            folder_path=folder_path,
            size_bytes=size_bytes,
            file_count=1,
            monitored=monitored,
            tags=tags,
            status="ON_DISK",
            target_pool_id=instance.default_pool_id,
        )
        self.db.add(item)
        self.db.commit()
        return item

    def notify_arr_archived(self, media_item: MediaItem, tape_barcode: str) -> Dict[str, Any]:
        """
        Unmonitors the Movie/Season in Sonarr/Radarr and swaps the `to-tape` tag for `on-tape`.
        """
        media_item.monitored = False
        tags = [t for t in (media_item.tags or []) if t != "to-tape"]
        if "on-tape" not in tags:
            tags.append("on-tape")
        barcode_tag = f"tape-{tape_barcode.lower()}"
        if barcode_tag not in tags:
            tags.append(barcode_tag)
        media_item.tags = tags
        self.db.commit()

        if not media_item.arr_instance_id:
            return {"status": "local_only", "unmonitored": True, "tags": tags}

        instance = self.db.get(ArrInstance, media_item.arr_instance_id)
        if not instance or not instance.enabled:
            return {"status": "instance_disabled", "unmonitored": True, "tags": tags}

        try:
            headers = {"X-Api-Key": instance.api_key}
            base_url = instance.url.rstrip("/")
            with httpx.Client(timeout=10.0, headers=headers) as client:
                if instance.arr_type.lower() == "radarr" and media_item.external_id:
                    m = client.get(f"{base_url}/api/v3/movie/{media_item.external_id}").json()
                    m["monitored"] = False
                    client.put(f"{base_url}/api/v3/movie/{media_item.external_id}", json=m)
                elif instance.arr_type.lower() == "sonarr" and media_item.external_id:
                    s = client.get(f"{base_url}/api/v3/series/{media_item.external_id}").json()
                    for seas in s.get("seasons", []):
                        if seas.get("seasonNumber") == media_item.season_number:
                            seas["monitored"] = False
                    client.put(f"{base_url}/api/v3/series/{media_item.external_id}", json=s)
            return {"status": "synced_to_arr", "unmonitored": True, "tags": tags}
        except Exception as exc:
            return {"status": "arr_unreachable_updated_locally", "detail": str(exc), "tags": tags}

    def notify_arr_restored(self, media_item: MediaItem, remonitor: bool = True) -> Dict[str, Any]:
        """
        Triggers a Rescan in Sonarr/Radarr after restoring from tape and optionally re-monitors.
        """
        if remonitor:
            media_item.monitored = True
        tags = [t for t in (media_item.tags or []) if t != "on-tape"]
        media_item.tags = tags
        self.db.commit()

        if not media_item.arr_instance_id:
            return {"status": "local_only", "monitored": media_item.monitored}

        instance = self.db.get(ArrInstance, media_item.arr_instance_id)
        if not instance or not instance.enabled:
            return {"status": "instance_disabled", "monitored": media_item.monitored}

        try:
            headers = {"X-Api-Key": instance.api_key}
            base_url = instance.url.rstrip("/")
            with httpx.Client(timeout=10.0, headers=headers) as client:
                if instance.arr_type.lower() == "radarr" and media_item.external_id:
                    client.post(
                        f"{base_url}/api/v3/command",
                        json={"name": "RescanMovie", "movieId": media_item.external_id},
                    )
                elif instance.arr_type.lower() == "sonarr" and media_item.external_id:
                    client.post(
                        f"{base_url}/api/v3/command",
                        json={"name": "RescanSeries", "seriesId": media_item.external_id},
                    )
            return {"status": "rescan_triggered", "monitored": media_item.monitored}
        except Exception as exc:
            return {"status": "arr_unreachable_updated_locally", "detail": str(exc)}
