#!/usr/bin/env bash
# ==============================================================================
# Taparr Debian 13 (Trixie) Automated Installer
# ==============================================================================
set -euo pipefail

echo "[Taparr Installer] Installing Debian 13 system packages..."
apt-get update
apt-get install -y \
    python3 python3-venv python3-pip \
    postgresql postgresql-client \
    mtx mt-st tar mbuffer sg3-utils lsscsi \
    ffmpeg git curl

INSTALL_DIR="/opt/taparr"
CONFIG_DIR="/etc/taparr"
DATA_DIR="/var/lib/taparr"

if ! id -u taparr >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --create-home --shell /bin/bash -G tape,disk,video taparr
fi

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$DATA_DIR/scratch" "$DATA_DIR/backups"
cp -r . "$INSTALL_DIR/"
chown -R taparr:tape "$INSTALL_DIR" "$DATA_DIR"

echo "[Taparr Installer] Setting up Python virtualenv..."
sudo -u taparr python3 -m venv "$INSTALL_DIR/.venv"
sudo -u taparr "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u taparr "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    cp "$INSTALL_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
    chown taparr:tape "$CONFIG_DIR/config.yaml"
fi

echo "[Taparr Installer] Ensuring PostgreSQL database and user exist..."
su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='taparr'\" | grep -q 1 || psql -c \"CREATE USER taparr WITH PASSWORD 'taparr';\""
su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='taparr'\" | grep -q 1 || psql -c \"CREATE DATABASE taparr OWNER taparr;\""

cp "$INSTALL_DIR/deploy/taparr.service" /etc/systemd/system/taparr.service
systemctl daemon-reload
systemctl enable taparr.service

echo "[Taparr Installer] Installation complete! Start with: systemctl start taparr.service"

