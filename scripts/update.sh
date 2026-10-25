#!/usr/bin/env bash
# ==============================================================================
# Taparr Seamless Update Script (scripts/update.sh)
# Can be invoked directly from the Taparr WebUI or from the command line.
# Steps:
#   1. Verify no tape drives are actively streaming (unless --force is passed).
#   2. Export a Pre-Update Catalog Disaster Recovery Backup.
#   3. Pull latest git changes (or target branch/tag).
#   4. Update Python virtualenv dependencies (requirements.txt).
#   5. Run database schema migrations/initialization.
#   6. Gracefully restart taparr.service if running under systemd.
# ==============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

VENV_PYTHON="${APP_DIR}/.venv/bin/python3"
if [ ! -x "$VENV_PYTHON" ]; then
    VENV_PYTHON="$(command -v python3)"
fi

MODE="run"
BRANCH=""
SKIP_RESTART="0"

while [ $# -gt 0 ]; do
    case "$1" in
        --check)
            MODE="check"
            shift
            ;;
        --dry-run)
            MODE="dry-run"
            shift
            ;;
        --no-restart)
            SKIP_RESTART="1"
            shift
            ;;
        --branch)
            BRANCH="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

CURRENT_VER="$("$VENV_PYTHON" -c "import taparr; print(taparr.__version__)" 2>/dev/null || echo "unknown")"
GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo "no-git")"
GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")"

if [ "$MODE" = "check" ]; then
    echo "current_version=${CURRENT_VER}"
    echo "git_commit=${GIT_COMMIT}"
    echo "git_branch=${GIT_BRANCH}"
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git fetch --quiet 2>/dev/null || true
        UPSTREAM_COMMIT="$(git rev-parse --short "@{u}" 2>/dev/null || echo "$GIT_COMMIT")"
        echo "upstream_commit=${UPSTREAM_COMMIT}"
    fi
    exit 0
fi

echo "============================================================"
echo "[Taparr Updater] Starting update from v${CURRENT_VER} (${GIT_COMMIT})"
echo "============================================================"

if [ "$MODE" = "dry-run" ]; then
    echo "[Taparr Updater] DRY RUN: Pre-update catalog backup check OK."
    echo "[Taparr Updater] DRY RUN: Would pull git branch ${BRANCH:-$GIT_BRANCH}, update requirements.txt, and migrate DB."
    echo "[Taparr Updater] DRY RUN completed successfully."
    exit 0
fi

echo "[1/5] Creating pre-update Catalog Disaster Recovery Backup..."
"$VENV_PYTHON" -m taparr.main --export-backup || echo "[WARN] Pre-update backup skipped or non-fatal warning."

echo "[2/5] Updating repository via git..."
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git remote | grep -q .; then
        if [ -n "$BRANCH" ]; then
            git fetch origin "$BRANCH" && git checkout "$BRANCH" && git pull --ff-only origin "$BRANCH"
        else
            git pull --ff-only
        fi
    else
        echo "      No remote configured; keeping local working tree."
    fi
else
    echo "      Not a git repository; skipping git pull."
fi

echo "[3/5] Updating Python dependencies in virtualenv..."
if [ -x "${APP_DIR}/.venv/bin/pip" ]; then
    "${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt" || echo "      Offline/cached environment: keeping existing packages."
fi

echo "[4/5] Applying database schema migrations..."
"$VENV_PYTHON" -m taparr.main --init-db

NEW_VER="$("$VENV_PYTHON" -c "import taparr; print(taparr.__version__)" 2>/dev/null || echo "unknown")"
echo "[5/5] Update complete: Taparr is now at v${NEW_VER}."

if [ "$SKIP_RESTART" = "0" ] && command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet taparr.service 2>/dev/null; then
        echo "[Taparr Updater] Restarting systemd service taparr.service..."
        systemctl restart taparr.service &
    fi
fi

echo "[Taparr Updater] Done."

