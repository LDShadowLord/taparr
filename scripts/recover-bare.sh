#!/usr/bin/env sh
# ==============================================================================
# Taparr Bare-Metal Disaster Recovery Utility (recover-bare.sh)
# Zero-dependency POSIX shell recovery tool using only: mtx, mt (mt-st), tar
# Can recover any filemark from any Taparr LTO cartridge WITHOUT Taparr installed.
# ==============================================================================
set -eu

usage() {
    cat <<EOF
Usage:
  $0 --inspect-header <tape_dev_or_dir>
      Rewinds tape and reads the 1 KB fm0 Taparr JSON/text descriptor header
      (shows taparr_version, tape_format_version, barcode, pool, block factor).

  $0 --extract <tape_dev_or_dir> <filemark_num> <dest_dir> [tar_block_factor]
      Rewinds tape, fast-forwards to <filemark_num> via hardware EOF filemarks,
      and extracts the raw TAR archive into <dest_dir>.
      Default tar_block_factor is 2048 (1 MiB blocks).

  $0 --read-manifest <tape_dev_or_dir> <manifest_filemark_num> <dest_dir> [tar_block_factor]
      Extracts the trailing self-describing JSON/CSV tape manifest at <manifest_filemark_num>.

  $0 --load <changer_dev> <slot_num> [drive_index]
      Uses mtx to load a cartridge from <slot_num> into <drive_index> (default 0).

  $0 --unload <changer_dev> <tape_dev> <slot_num> [drive_index]
      Rewinds/offlines <tape_dev> and unloads from <drive_index> to <slot_num>.
EOF
    exit 1
}

if [ $# -lt 2 ]; then
    usage
fi

MODE="$1"
shift

case "$MODE" in
    --inspect-header)
        TARGET="$1"
        if [ -d "$TARGET" ]; then
            # Virtual simulator cartridge directory
            if [ -f "$TARGET/fm0000.header.json" ]; then
                cat "$TARGET/fm0000.header.json"
                exit 0
            else
                echo "ERROR: No fm0000.header.json found in $TARGET" >&2
                exit 2
            fi
        fi
        echo "[recover-bare] Rewinding $TARGET to Filemark 0 (fm0)..." >&2
        mt -f "$TARGET" rewind
        echo "[recover-bare] Reading 1 KB Taparr Tape Header from $TARGET:" >&2
        dd if="$TARGET" bs=1024 count=1 status=none
        echo "" >&2
        ;;

    --extract|--read-manifest)
        if [ $# -lt 3 ]; then
            usage
        fi
        TARGET="$1"
        FM_NUM="$2"
        DEST_DIR="$3"
        BLOCK_FACTOR="${4:-2048}"
        mkdir -p "$DEST_DIR"

        if [ -d "$TARGET" ]; then
            # Virtual simulator cartridge directory
            FM_PADDED=$(printf "%04d" "$FM_NUM")
            TAR_PATH="$TARGET/fm${FM_PADDED}.tar"
            if [ ! -f "$TAR_PATH" ]; then
                echo "ERROR: Filemark archive $TAR_PATH not found" >&2
                exit 2
            fi
            echo "[recover-bare] Extracting virtual filemark $FM_NUM ($TAR_PATH) to $DEST_DIR..."
            tar -b "$BLOCK_FACTOR" -xvf "$TAR_PATH" -C "$DEST_DIR"
            echo "[recover-bare] Extraction complete."
            exit 0
        fi

        echo "[recover-bare] Rewinding $TARGET..."
        mt -f "$TARGET" rewind
        echo "[recover-bare] Fast-forwarding hardware filemarks: mt -f $TARGET fsf $FM_NUM"
        mt -f "$TARGET" fsf "$FM_NUM"
        echo "[recover-bare] Extracting TAR stream (blocking factor -b $BLOCK_FACTOR) to $DEST_DIR..."
        tar -b "$BLOCK_FACTOR" -xvf "$TARGET" -C "$DEST_DIR"
        echo "[recover-bare] Extraction of Filemark #$FM_NUM complete."
        ;;

    --load)
        if [ $# -lt 2 ]; then
            usage
        fi
        CHANGER="$1"
        SLOT="$2"
        DRIVE="${3:-0}"
        echo "[recover-bare] Loading Slot $SLOT into Drive $DRIVE via $CHANGER..."
        mtx -f "$CHANGER" load "$SLOT" "$DRIVE"
        ;;

    --unload)
        if [ $# -lt 3 ]; then
            usage
        fi
        CHANGER="$1"
        TAPE_DEV="$2"
        SLOT="$3"
        DRIVE="${4:-0}"
        echo "[recover-bare] Rewinding and setting $TAPE_DEV offline..."
        mt -f "$TAPE_DEV" offline || mt -f "$TAPE_DEV" rewind
        echo "[recover-bare] Unloading Drive $DRIVE to Slot $SLOT via $CHANGER..."
        mtx -f "$CHANGER" unload "$SLOT" "$DRIVE"
        ;;

    *)
        usage
        ;;
esac

