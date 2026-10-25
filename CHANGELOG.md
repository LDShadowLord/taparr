# Changelog

All notable changes to **Taparr** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-25

### Added
- **Full Physical SCSI Hardware Execution (`mtx`, `mt-st`, `tar`, `mbuffer`, `sg_logs`)**:
  - Real hardware block position reading via `mt -f /dev/nstX tell` / `status`.
  - Physical `/dev/nstX` TAR streaming for trailing self-describing manifests (`write_trailing_manifest`).
  - Physical drive-to-drive `mbuffer` / `dd` filemark streaming for **Tape Cloning** and **Dual-Drive Tape Compaction**.
  - Physical SCSI **TapeAlert (`sg_logs -p 0x2e`)** health & cleaning-required detection plus automated **Drive Cleaning (`clean_drive`)** with `CU` cleaning cartridges.
- **Production PostgreSQL & Portable SQL Dump Engine**:
  - Live database engine telemetry (`PostgreSQL` vs `SQLite fallback`, server version, pool metrics, row counts) in Dashboard & Settings.
  - Configurable `strict_postgres` mode in `config.yaml` alongside native `pg_dump` + pure-SQLAlchemy portable PostgreSQL SQL `INSERT` dump generation (`taparr-pgdump.sql`).
- **Background Worker & Idle Auto-Shutdown Loop**:
  - Non-blocking background job execution option for multi-hour LTO tape batches, periodic Sonarr/Radarr `to-tape` tag polling, and automatic MSL4048 idle cooldown power-off.
- **Expanded WebUI Controls**:
  - Direct browser download of Disaster Recovery Catalog Backup files (`catalog-files.csv`, `catalog-files.json`, `RECOVERY.md`, `recover-bare.sh`, `taparr-pgdump.sql`).
  - WebUI forms for toggling Physical SCSI vs. Virtual Simulator mode, configuring `/dev/tape/by-id/...` drive paths & LTO-1..LTO-10 generations, editing pool regex/thresholds, and adding unmanaged folders.

## [0.1.0] - 2026-10-25

### Added
- **On-Tape Versioned Format (`tape_format_version: 1`)**:
  - `fm0` 1 KB JSON/text descriptor storing `taparr_version`, `tape_format_version`, barcode, generation, pool, UUID, TAR blocking factor, and bare-metal recovery instructions.
  - Per-Movie and Per-Season raw `tar` filemarks (`fm1..fmN`) with trailing self-describing JSON/CSV manifest (`fm(N+1)`) overwritten on append and sealed on tape close.
- **Full LTO Generation Matrix (`LTO-1` through `LTO-10` / `LA`)**:
  - Support for `L1`, `L2`, `L3`, `L4`, `L5`, `L6`, `L7`, `M8` (LTO-7 Type M), `L8`, `L9`, `LA` (LTO-10), and `CLN`/`CU` cleaning cartridges with strict drive-to-media read/write hardware compatibility enforcement.
- **System & Custom Tape Pools**:
  - Built-in **`Unassigned`** pool for newly discovered cartridges.
  - Built-in **`Used By Other`** protected pool (with optional barcode prefix/regex auto-matching) that hard-locks foreign backup software tapes from ever being loaded, moved, or modified by Taparr.
  - Custom user Tape Pools (`Movies`, `TV Shows`, `Anime`, etc.) with queue bin-packing, dual-drive scheduling, drive-to-drive tape cloning, and dual-drive dead-space compaction.
- **HP MSL4048 Robotics, Mailslot & Power Manager**:
  - 48-slot rack visualizer, persistent `/dev/tape/by-id/` device mapping, `IMPORT/EXPORT` mailslot actions, and `Vaulted / Offsite` cartridge tracking.
  - Wake-on-Job power-on via HTTP Webhook or shell script, SCSI bus rescan, `mtx status` boot inventory polling, safe drive unload + idle cooldown auto-power-off, and Maintenance Lock mode.
  - Built-in **Virtual MSL4048 Simulator** for end-to-end testing without spinning physical robotics.
- **Pipelined Local SSD Scratch Stager & Integrity Engine**:
  - Rolling SSD scratch buffer with configurable GB cap, inline SHA-256 hashing, hardware `mt` / `sg_logs` verification, optional read-back verification, and single-pass ascending filemark restore.
  - Tracks `Restored (On Disk + On Tape)` status for instant re-eviction without rewriting unchanged data to tape.
- **Sonarr, Radarr & Plex Integrations**:
  - Multi-instance Sonarr & Radarr v3/v4 support with path mappings, `to-tape` tag polling, auto-unmonitor & tagging, and unmanaged folder support.
  - Configurable **Clean Deletion** or **FFmpeg Playable `.mkv` Video Stubs** on disk, targeted Plex folder refresh, and `/api/v1/webhooks/plex` stub playback trigger.
- **Disaster Recovery Catalog Exporter & Seamless WebUI Updater**:
  - Automated export of `catalog-files.csv`, `catalog-files.json`, `tapes-inventory.csv`, `taparr-pgdump.sql`, `RECOVERY.md`, and standalone POSIX `recover-bare.sh`.
  - In-WebUI seamless update system (`scripts/update.sh`) with pre-update catalog backup, active-drive safety lock, and rendered `CHANGELOG.md` viewer.

