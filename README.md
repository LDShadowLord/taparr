# Taparr 📼

**Taparr** is a self-hosted media tape archival and catalog management platform inspired by the `*arr` stack (`Sonarr` / `Radarr`), built for an **HP MSL4048** 48-slot tape library with **LTO-1 (`L1`) through LTO-10 (`LA`)** tape drive and media support on **Debian 13 (Trixie)**.

---

## Key Features

1. **Versioned Raw `tar` On-Tape Format (`tape_format_version: 1`)**:
   - **Filemark 0 (`fm0`)**: 1 KB JSON/text descriptor storing `taparr_version` (e.g., `0.1.0`), `tape_format_version` (`1`), cartridge barcode, UUID, LTO media generation (`L1`..`LA`), Tape Pool, TAR blocking factor (`-b 2048`), and bare-metal recovery instructions.
   - **Filemarks `1..N` (`fm1..fmN`)**: One unencrypted POSIX/GNU `tar` stream per Movie folder, TV Season folder, or Unmanaged folder, separated by hardware EOF filemarks (`mt-st`).
   - **Trailing Self-Describing Manifest (`fm(N+1)`)**: Written at the end of every batch (and overwritten on append) containing `tape-manifest.json`, `tape-manifest.csv`, and `RECOVERY.txt`. Clicking **Close / Seal Tape** (or reaching pool capacity threshold) seals the cartridge.
2. **Full LTO-1 through LTO-10 (`LA`) Generation Support**:
   - Supports `L1`, `L2`, `L3`, `L4`, `L5`, `L6`, `L7`, `M8` (LTO-7 Type M), `L8`, `L9`, `LA` (LTO-10), and `CU`/`CLN` cleaning cartridges, enforcing exact read/write drive-to-media compatibility before loading.
3. **System Pools (`Unassigned` & Protected `Used By Other`) + Custom Pools**:
   - **`Unassigned`**: Holding pool for newly discovered cartridges so you can manually assign tapes to pools using your own barcode conventions.
   - **`Used By Other`**: Protected system pool for cartridges used by other backup software sharing the library (e.g., Veeam, PBS, Bareos). Supports auto-matching barcode regexes and hard-locks Taparr from ever loading, moving, or modifying those tapes.
   - **Queue Bin-Packing, Dual-Drive Cloning & Compaction**: Never splits a Movie or Season across cartridges; packs smaller queued items when a tape nears capacity, clones tapes drive-to-drive for offsite storage, and compacts dead space across two drives.
4. **Pipelined Local SSD Scratch Stager & Instant Re-Eviction**:
   - Stages items to a fast local NVMe/SSD buffer (with configurable GB cap) while computing inline SHA-256 checksums to prevent tape shoe-shining.
   - Restores items in a **single forward pass** (sorted by ascending filemark `#`) and tracks `ON_DISK_AND_TAPE` status for **instant re-eviction** without rewriting unchanged files to tape.
5. **Sonarr, Radarr & Plex Integrations**:
   - Multi-instance Sonarr/Radarr support with path mapping, `to-tape` tag detection, automatic unmonitoring & tagging, configurable **Playable `.mkv` Video Stubs** (via `ffmpeg`) or **Clean Deletion**, targeted Plex folder refresh, and a `/api/v1/webhooks/plex` stub playback trigger.
6. **HP MSL4048 Power Manager & Bare-Metal Disaster Recovery**:
   - Wake-on-Job via HTTP Webhook (Home Assistant, Shelly, Tasmota) or shell script, `mtx status` boot polling, and safe idle power-off (unloads all drives back to slots before cutting power).
   - Automatic export of Disaster Recovery Catalog Bundles (`catalog-files.csv`, `catalog-files.json`, `tapes-inventory.csv`, `taparr-pgdump.sql`, `RECOVERY.md`, and standalone POSIX `recover-bare.sh`).
7. **Seamless In-WebUI Updater & `CHANGELOG.md`**:
   - View `CHANGELOG.md` and run `scripts/update.sh` directly from the WebUI (with active-drive lock protection and automatic pre-update catalog backup).

---

## Quick Start (Development / Virtual MSL4048 Simulator)

```bash
./.venv/bin/uvicorn taparr.main:app --host 0.0.0.0 --port 8686
```

Run the test suite:
```bash
./.venv/bin/pytest -v
```

