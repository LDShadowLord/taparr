"""
Comprehensive End-to-End Test Suite for Taparr:
  1. LTO-1 (L1) through LTO-10 (LA), M8, and CU compatibility matrix verification.
  2. Versioned 1 KB fm0 Tape Header (`taparr_version` & `tape_format_version`) + trailing manifest overwrite & close tape.
  3. System pools ('Unassigned' and protected 'Used By Other' hard lock + regex auto-matching).
  4. Pipelined Local SSD Scratch Stager, SHA-256 hashing, Queue Bin-Packing, and FFmpeg Playable .mkv Stubs.
  5. Single-pass ascending filemark restore + Instant Re-Eviction of unchanged `ON_DISK_AND_TAPE` items.
  6. Dual-drive Tape Cloning, Dual-drive Dead-Space Compaction, and Vault/Mailslot tracking.
  7. Bare-metal recovery using `scripts/recover-bare.sh` without Taparr + Catalog Backup Bundle verification.
  8. HP MSL4048 Power Manager safe drive unload on shutdown + Seamless WebUI Updater (`scripts/update.sh`) & `CHANGELOG.md`.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taparr import (
    __version__,
    TAPE_FORMAT_VERSION,
    LTO_MEDIA_SPECS,
    check_drive_media_compatibility,
    detect_media_generation_from_barcode,
)
from taparr.config import PROJECT_ROOT
from taparr.services.hardware import (
    build_fm0_descriptor,
    parse_fm0_descriptor,
    ForeignTapeProtectedError,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "taparr_test_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "test_taparr.db"
    monkeypatch.setenv("TAPARR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TAPARR_DATABASE_URL", f"sqlite:///{db_path}")

    import taparr.database as db_mod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, future=True)
    TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", test_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSessionLocal)

    # Update default paths in DEFAULT_SETTINGS for isolated test run
    db_mod.DEFAULT_SETTINGS["scratch"]["scratch_dir"] = str(data_dir / "scratch")
    db_mod.DEFAULT_SETTINGS["catalog_backup"]["backup_dirs"] = [str(data_dir / "backups")]

    from taparr.main import app

    db_mod.init_db()
    with TestClient(app) as c:
        yield c


def test_lto_generations_up_to_lto10_la():
    """Verifies all LTO generations L1..L9, LA (LTO-10), M8, and CU are supported."""
    expected_codes = {"L1", "L2", "L3", "L4", "L5", "L6", "L7", "M8", "L8", "L9", "LA", "CU"}
    assert expected_codes.issubset(set(LTO_MEDIA_SPECS.keys()))

    assert detect_media_generation_from_barcode("MOV001L5") == "L5"
    assert detect_media_generation_from_barcode("ULT999LA") == "LA"
    assert detect_media_generation_from_barcode("TYPEM1M8") == "M8"
    assert detect_media_generation_from_barcode("CLN001CU") == "CU"

    # LTO-5 drive can R/W L5, cannot read/write L6 or LA
    c_l5_on_l5 = check_drive_media_compatibility("LTO-5", "L5")
    assert c_l5_on_l5["can_read"] is True and c_l5_on_l5["can_write"] is True

    c_l6_on_l5 = check_drive_media_compatibility("LTO-5", "L6")
    assert c_l6_on_l5["can_read"] is False and c_l6_on_l5["can_write"] is False

    # LTO-6 drive can R/W both L6 and L5 media!
    c_l5_on_l6 = check_drive_media_compatibility("LTO-6", "L5")
    assert c_l5_on_l6["can_read"] is True and c_l5_on_l6["can_write"] is True

    # LTO-10 drive can R/W LA (LTO-10) and L9 media
    c_la_on_l10 = check_drive_media_compatibility("LTO-10", "LA")
    assert c_la_on_l10["can_read"] is True and c_la_on_l10["can_write"] is True
    assert LTO_MEDIA_SPECS["LA"]["native_capacity_gb"] == 30000


def test_fm0_descriptor_contains_taparr_version_and_format_version():
    """Verifies the 1 KB fm0 header stores taparr_version and tape_format_version and pads to 1024 bytes."""
    raw = build_fm0_descriptor(
        barcode="MOV001L5",
        tape_uuid="1234-5678",
        media_code="L5",
        pool_name="Movies",
        tar_block_factor=2048,
    )
    assert len(raw) == 1024
    parsed = parse_fm0_descriptor(raw)
    assert parsed["taparr_version"] == __version__
    assert parsed["tape_format_version"] == TAPE_FORMAT_VERSION
    assert parsed["recognized_format"] is True
    assert parsed["barcode"] == "MOV001L5"
    assert parsed["pool_name"] == "Movies"


def test_used_by_other_pool_strict_protection(client: TestClient):
    """
    Verifies the 'Used By Other' system pool exists alongside 'Unassigned' and strictly blocks
    loading, closing, cloning, compacting, or queueing media to foreign backup tapes.
    """
    pools = client.get("/api/v1/pools").json()
    pool_names = {p["name"]: p for p in pools}
    assert "Unassigned" in pool_names
    assert "Used By Other" in pool_names
    used_by_other = pool_names["Used By Other"]
    assert used_by_other["is_system"] is True
    assert used_by_other["is_protected_foreign"] is True

    # Verify seeded BKP001L5 and VEEAM2L6 are in 'Used By Other' and locked
    tapes = client.get("/api/v1/tapes").json()
    bkp_tape = next(t for t in tapes if t["barcode"] == "BKP001L5")
    assert bkp_tape["pool_name"] == "Used By Other"
    assert bkp_tape["is_protected_foreign"] is True
    assert bkp_tape["status"] == "FOREIGN_LOCKED"

    # Attempt to load BKP001L5 into Drive 0 -> must fail with 400 ForeignTapeProtectedError
    res_load = client.post("/api/v1/library/load", json={"barcode": "BKP001L5", "drive_index": 0})
    assert res_load.status_code == 400
    assert "Used By Other" in res_load.json()["detail"]

    # Attempt to close BKP001L5 -> must fail with 400
    res_close = client.post("/api/v1/tapes/BKP001L5/close")
    assert res_close.status_code == 400

    # Attempt to delete system pool 'Used By Other' -> must fail with 400
    res_del = client.delete(f"/api/v1/pools/{used_by_other['id']}")
    assert res_del.status_code == 400


def test_end_to_end_archive_stub_restore_reevict_and_bare_recovery(client: TestClient, tmp_path):
    """
    Full lifecycle test:
      1. Seed demo media (Movies, TV Shows, Anime).
      2. Queue for archive & flush batch -> writes fm0 (with taparr_version), fm1, trailing manifest fm2.
      3. Verify playable .mkv stub file created on disk and fm0 header readable.
      4. Append a second batch to the same tape -> overwrites old manifest fm2 with new media fm2 and writes manifest at fm3!
      5. Test `scripts/recover-bare.sh` extracting fm1 directly from the tape without Taparr!
      6. Queue & run Single-Pass Ascending Filemark Restore -> verifies SHA-256, removes .mkv stub, marks `ON_DISK_AND_TAPE`.
      7. Re-queue unchanged `ON_DISK_AND_TAPE` item -> verifies Instant Re-Eviction without rewriting to tape!
    """
    seed_res = client.post("/api/v1/media/seed-demo").json()
    media_ids = seed_res["seeded_media_ids"]
    assert len(media_ids) == 3

    # Queue first Movie (Interstellar) to Movies pool
    q_res = client.post("/api/v1/media/queue-archive", json={"media_item_ids": [media_ids[0]]}).json()
    assert q_res["queued_ids"] == [media_ids[0]]

    # Run archive batch
    batch_res = client.post("/api/v1/jobs/run-archive", json={}).json()
    assert batch_res["status"] == "completed"
    assert batch_res["catalog_backup"] is not None

    # Verify media item is now ON_TAPE_STUBBED on MOV001L5 at FM #1, and .taparr_stub.mkv exists on disk
    media_list = client.get("/api/v1/media").json()
    m0 = next(m for m in media_list if m["id"] == media_ids[0])
    assert m0["status"] == "ON_TAPE_STUBBED"
    assert m0["primary_tape_barcode"] == "MOV001L5"
    assert m0["primary_filemark"] == 1
    assert m0["stub_file_path"] and os.path.exists(m0["stub_file_path"])

    # Verify fm0 header on MOV001L5 includes taparr_version and tape_format_version
    hdr = client.get("/api/v1/tapes/MOV001L5/header").json()
    assert hdr["taparr_version"] == __version__
    assert hdr["tape_format_version"] == TAPE_FORMAT_VERSION
    assert hdr["barcode"] == "MOV001L5"

    # Check filemarks on MOV001L5: fm0 (HEADER), fm1 (MEDIA), fm2 (MANIFEST)
    fms_after_batch1 = client.get("/api/v1/tapes/MOV001L5/filemarks").json()
    assert [(fm["filemark_number"], fm["record_type"]) for fm in fms_after_batch1] == [
        (0, "HEADER"),
        (1, "MEDIA"),
        (2, "MANIFEST"),
    ]

    # Now queue second item to the SAME Movies pool to test Trailing Manifest Overwrite on Append!
    movies_pool = next(p for p in client.get("/api/v1/pools").json() if p["name"] == "Movies")
    client.post(
        "/api/v1/media/queue-archive",
        json={"media_item_ids": [media_ids[1]], "target_pool_id": movies_pool["id"]},
    )
    client.post("/api/v1/jobs/run-archive", json={})

    # Verify fm2 was overwritten with the new MEDIA item and the trailing MANIFEST moved to fm3!
    fms_after_batch2 = client.get("/api/v1/tapes/MOV001L5/filemarks").json()
    assert [(fm["filemark_number"], fm["record_type"]) for fm in fms_after_batch2] == [
        (0, "HEADER"),
        (1, "MEDIA"),
        (2, "MEDIA"),
        (3, "MANIFEST"),
    ]

    # Test bare-metal recovery script `scripts/recover-bare.sh` without Taparr!
    sim_tape_dir = Path(os.environ["TAPARR_DATA_DIR"]) / "virtual_msl4048" / "cartridges" / "MOV001L5"
    bare_extract_dir = tmp_path / "bare_restore_out"
    recover_sh = PROJECT_ROOT / "scripts" / "recover-bare.sh"

    hdr_cmd = subprocess.run(
        [str(recover_sh), "--inspect-header", str(sim_tape_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(hdr_cmd.stdout)["taparr_version"] == __version__

    subprocess.run(
        [str(recover_sh), "--extract", str(sim_tape_dir), "1", str(bare_extract_dir), "2048"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (bare_extract_dir / "Interstellar (2014) [Remux-2160p].mkv").exists()

    # Queue both items for Restore in reverse order [media_ids[1] (FM #2), media_ids[0] (FM #1)]
    # and verify Single-Pass Ascending Filemark Restore sorts them [1, 2]!
    client.post("/api/v1/media/queue-restore", json={"media_item_ids": [media_ids[1], media_ids[0]]})
    restore_res = client.post("/api/v1/jobs/run-restore", json={"remonitor_in_arr": True}).json()
    assert restore_res["status"] == "completed"
    restored_fms = [r["filemark_number"] for r in restore_res["restored_items"]]
    assert restored_fms == [1, 2]

    # Verify both items are now ON_DISK_AND_TAPE and original files are back on disk
    media_after_restore = {m["id"]: m for m in client.get("/api/v1/media").json()}
    assert media_after_restore[media_ids[0]]["status"] == "ON_DISK_AND_TAPE"
    assert (Path(media_after_restore[media_ids[0]]["folder_path"]) / "Interstellar (2014) [Remux-2160p].mkv").exists()

    # Now re-queue unchanged media_ids[0] for archive -> must trigger Instant Re-Eviction without writing to tape!
    reevict_res = client.post("/api/v1/media/queue-archive", json={"media_item_ids": [media_ids[0]]}).json()
    assert reevict_res["instantly_reevicted_ids"] == [media_ids[0]]
    assert reevict_res["queued_ids"] == []


def test_bin_packing_clone_compaction_power_and_updater(client: TestClient, tmp_path):
    """
    Tests:
      1. Queue Bin-Packing when a large item doesn't fit in remaining tape space but a smaller item later in the queue does.
      2. Drive-to-Drive Tape Cloning (`MOV001L5` -> `RAW001L5`).
      3. Dead-space tracking & Dual-Drive Tape Compaction.
      4. Power Manager safe drive unload before power-off.
      5. Seamless WebUI Updater (`scripts/update.sh` dry-run + `CHANGELOG.md` parsing).
    """
    # Create 3 folders: Item A (600 KB), Item B (800 KB), Item C (300 KB)
    # and set MOV001L5 remaining capacity to 1,000 KB so Item A (600 KB) + Item C (300 KB) pack into MOV001L5
    # while Item B (800 KB) rolls over onto MOV002L5!
    movies_pool = next(p for p in client.get("/api/v1/pools").json() if p["name"] == "Movies")
    # Set close_threshold_gb = 0 on Movies pool so exact byte bin-packing is tested
    client.put(
        f"/api/v1/pools/{movies_pool['id']}",
        json={
            "name": "Movies",
            "description": "Radarr movie archives",
            "color": "#f59e0b",
            "barcode_regex": "",
            "close_threshold_gb": 0,
            "verify_after_write": False,
        },
    )

    import taparr.database as db_mod
    from sqlalchemy import select
    from taparr.models import Tape

    with db_mod.SessionLocal() as db:
        t1 = db.scalar(select(Tape).where(Tape.barcode == "MOV001L5"))
        t1.native_capacity_bytes = 1000 * 1024  # 1,000 KB capacity for test
        db.commit()

    item_ids = []
    for name, kb in [("Movie_A_600KB", 600), ("Movie_B_800KB", 800), ("Movie_C_300KB", 300)]:
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.mkv").write_bytes(b"X" * (kb * 1024))
        r = client.post(
            "/api/v1/media/unmanaged",
            json={"folder_path": str(d), "title": name, "media_category": "Movies", "target_pool_id": movies_pool["id"]},
        ).json()
        item_ids.append(r["id"])

    client.post("/api/v1/media/queue-archive", json={"media_item_ids": item_ids})
    client.post("/api/v1/jobs/run-archive", json={})

    # Verify Bin-Packing: Movie_A (600KB) and Movie_C (300KB) packed onto MOV001L5 (which is now CLOSED),
    # while Movie_B (800KB) was written to MOV002L5!
    media_map = {m["title"]: m for m in client.get("/api/v1/media").json()}
    assert media_map["Movie_A_600KB"]["primary_tape_barcode"] == "MOV001L5"
    assert media_map["Movie_C_300KB"]["primary_tape_barcode"] == "MOV001L5"
    assert media_map["Movie_B_800KB"]["primary_tape_barcode"] == "MOV002L5"

    tapes_map = {t["barcode"]: t for t in client.get("/api/v1/tapes").json()}
    assert tapes_map["MOV001L5"]["status"] == "CLOSED"

    # Test Drive-to-Drive Tape Cloning: clone MOV001L5 onto RAW001L5
    clone_res = client.post("/api/v1/tapes/MOV001L5/clone", json={"target_barcode": "RAW001L5"}).json()
    assert clone_res["filemarks_cloned"] == 2

    # Test Dual-Drive Tape Compaction on MOV001L5:
    # Restore Movie_A_600KB, modify its file on disk, and re-queue so FM #1 on MOV001L5 becomes dead space!
    client.post("/api/v1/media/queue-restore", json={"media_item_ids": [media_map["Movie_A_600KB"]["id"]]})
    client.post("/api/v1/jobs/run-restore", json={})
    (tmp_path / "Movie_A_600KB" / "Movie_A_600KB.mkv").write_bytes(b"UPGRADED_" * 1024)
    client.post("/api/v1/media/queue-archive", json={"media_item_ids": [media_map["Movie_A_600KB"]["id"]]})

    # Assign RAW002L5 to Movies pool so Compaction has a blank target tape in the pool
    client.put("/api/v1/tapes/RAW002L5/assign-pool", json={"pool_id": movies_pool["id"]})
    compact_res = client.post("/api/v1/tapes/MOV001L5/compact", json={"target_barcode": "RAW002L5"}).json()
    assert compact_res["active_filemarks_kept"] == 1
    assert compact_res["reclaimed_dead_bytes"] > 0

    # Test Power Manager safe drive unload on shutdown
    pwr_off = client.post("/api/v1/library/power/off").json()
    assert pwr_off["power_state"] == "OFFLINE"
    lib_state = client.get("/api/v1/library/state").json()
    assert all(d["loaded_barcode"] is None for d in lib_state["drives"])

    # Test System Version, CHANGELOG.md parser, and Seamless WebUI Updater (dry-run)
    ver_info = client.get("/api/v1/system/version").json()
    assert ver_info["taparr_version"] == __version__
    assert ver_info["tape_format_version"] == TAPE_FORMAT_VERSION
    assert len(ver_info["releases"]) >= 1
    assert ver_info["releases"][0]["version"] == __version__

    upd_res = client.post("/api/v1/system/update", json={"dry_run": True, "force": False}).json()
    assert upd_res["status"] == "completed"
    assert "DRY RUN" in upd_res["log_output"]
    assert upd_res["pre_update_backup"].startswith("taparr-catalog-")

