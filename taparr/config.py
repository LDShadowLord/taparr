"""
Bootstrap configuration loader for Taparr.
Reads /etc/taparr/config.yaml or ./config.yaml (or TAPARR_CONFIG env variable).
All operational settings are stored in the database and managed via the WebUI.
"""

import os
from pathlib import Path
from typing import Dict, Any
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_bootstrap_config() -> Dict[str, Any]:
    config_paths = [
        os.environ.get("TAPARR_CONFIG", ""),
        str(PROJECT_ROOT / "config.yaml"),
        "/etc/taparr/config.yaml",
    ]
    loaded: Dict[str, Any] = {}
    for p in config_paths:
        if p and os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            break

    server = loaded.get("server", {})
    database = loaded.get("database", {})
    paths = loaded.get("paths", {})

    data_dir = Path(os.environ.get("TAPARR_DATA_DIR", paths.get("data_dir", str(PROJECT_ROOT / "data")))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    default_db_url = os.environ.get(
        "TAPARR_DATABASE_URL",
        database.get("url", f"sqlite:///{data_dir / 'taparr.db'}"),
    )

    return {
        "server": {
            "host": os.environ.get("TAPARR_HOST", server.get("host", "0.0.0.0")),
            "port": int(os.environ.get("TAPARR_PORT", server.get("port", 8686))),
            "log_level": server.get("log_level", "info"),
        },
        "database": {
            "url": default_db_url,
            "pool_size": int(database.get("pool_size", 10)),
            "max_overflow": int(database.get("max_overflow", 20)),
        },
        "paths": {
            "data_dir": str(data_dir),
            "scratch_dir": str(data_dir / "scratch"),
            "backup_dir": str(data_dir / "backups"),
            "virtual_library_dir": str(data_dir / "virtual_msl4048"),
        },
    }
