"""
Taparr — LTO Tape Archival & Catalog Manager for Sonarr, Radarr, and Plex.
Supports LTO-1 (L1) through LTO-10 (LA) media and drives on the HP MSL4048.
"""

from typing import Dict, Any

__version__ = "0.2.0"
TAPE_FORMAT_VERSION = 1
TAPE_HEADER_MAGIC = "TAPARR_TAPE_HEADER"

# Complete LTO Generation Matrix up to LTO-10 (LA), including LTO-7 Type M (M8) and Cleaning (CLN/CU)
LTO_MEDIA_SPECS: Dict[str, Dict[str, Any]] = {
    "L1": {
        "generation": "LTO-1",
        "code": "L1",
        "native_capacity_gb": 100,
        "native_capacity_bytes": 100 * 1000**3,
        "native_speed_mbps": 20,
        "is_cleaning": False,
    },
    "L2": {
        "generation": "LTO-2",
        "code": "L2",
        "native_capacity_gb": 200,
        "native_capacity_bytes": 200 * 1000**3,
        "native_speed_mbps": 40,
        "is_cleaning": False,
    },
    "L3": {
        "generation": "LTO-3",
        "code": "L3",
        "native_capacity_gb": 400,
        "native_capacity_bytes": 400 * 1000**3,
        "native_speed_mbps": 80,
        "is_cleaning": False,
    },
    "L4": {
        "generation": "LTO-4",
        "code": "L4",
        "native_capacity_gb": 800,
        "native_capacity_bytes": 800 * 1000**3,
        "native_speed_mbps": 120,
        "is_cleaning": False,
    },
    "L5": {
        "generation": "LTO-5",
        "code": "L5",
        "native_capacity_gb": 1500,
        "native_capacity_bytes": 1500 * 1000**3,
        "native_speed_mbps": 140,
        "is_cleaning": False,
    },
    "L6": {
        "generation": "LTO-6",
        "code": "L6",
        "native_capacity_gb": 2500,
        "native_capacity_bytes": 2500 * 1000**3,
        "native_speed_mbps": 160,
        "is_cleaning": False,
    },
    "L7": {
        "generation": "LTO-7",
        "code": "L7",
        "native_capacity_gb": 6000,
        "native_capacity_bytes": 6000 * 1000**3,
        "native_speed_mbps": 300,
        "is_cleaning": False,
    },
    "M8": {
        "generation": "LTO-7 Type M",
        "code": "M8",
        "native_capacity_gb": 9000,
        "native_capacity_bytes": 9000 * 1000**3,
        "native_speed_mbps": 300,
        "is_cleaning": False,
    },
    "L8": {
        "generation": "LTO-8",
        "code": "L8",
        "native_capacity_gb": 12000,
        "native_capacity_bytes": 12000 * 1000**3,
        "native_speed_mbps": 360,
        "is_cleaning": False,
    },
    "L9": {
        "generation": "LTO-9",
        "code": "L9",
        "native_capacity_gb": 18000,
        "native_capacity_bytes": 18000 * 1000**3,
        "native_speed_mbps": 400,
        "is_cleaning": False,
    },
    "LA": {
        "generation": "LTO-10",
        "code": "LA",
        "native_capacity_gb": 30000,
        "native_capacity_bytes": 30000 * 1000**3,
        "native_speed_mbps": 400,
        "is_cleaning": False,
    },
    "CU": {
        "generation": "Universal Cleaning",
        "code": "CU",
        "native_capacity_gb": 0,
        "native_capacity_bytes": 0,
        "native_speed_mbps": 0,
        "is_cleaning": True,
    },
}

# Hardware Drive-to-Media Compatibility Matrix (LTO-1 through LTO-10)
LTO_DRIVE_COMPATIBILITY: Dict[str, Dict[str, list]] = {
    "LTO-1": {"read": ["L1"], "write": ["L1"]},
    "LTO-2": {"read": ["L2", "L1"], "write": ["L2", "L1"]},
    "LTO-3": {"read": ["L3", "L2", "L1"], "write": ["L3", "L2"]},
    "LTO-4": {"read": ["L4", "L3", "L2"], "write": ["L4", "L3"]},
    "LTO-5": {"read": ["L5", "L4", "L3"], "write": ["L5", "L4"]},
    "LTO-6": {"read": ["L6", "L5", "L4"], "write": ["L6", "L5"]},
    "LTO-7": {"read": ["L7", "L6", "L5"], "write": ["L7", "L6"]},
    "LTO-8": {"read": ["L8", "M8", "L7"], "write": ["L8", "M8", "L7"]},
    "LTO-9": {"read": ["L9", "L8"], "write": ["L9", "L8"]},
    "LTO-10": {"read": ["LA", "L9"], "write": ["LA", "L9"]},
}


def detect_media_generation_from_barcode(barcode: str) -> str:
    """
    Infers the LTO media code (L1..L9, LA, M8, CU) from a standard 8-character LTO barcode.
    Defaults to 'L5' if no known suffix is present.
    """
    b = (barcode or "").strip().upper()
    if b.startswith("CLN") or b.endswith("CU") or b.endswith("CL"):
        return "CU"
    if len(b) >= 2:
        suffix = b[-2:]
        if suffix in LTO_MEDIA_SPECS:
            return suffix
    return "L5"


def get_media_spec(media_code: str) -> Dict[str, Any]:
    code = (media_code or "L5").strip().upper()
    # Allow passing "LTO-5".. "LTO-10" as well
    for k, spec in LTO_MEDIA_SPECS.items():
        if code == k or code == spec["generation"].upper():
            return spec
    return LTO_MEDIA_SPECS["L5"]


def check_drive_media_compatibility(drive_generation: str, media_code: str) -> Dict[str, Any]:
    """
    Checks whether a given LTO drive generation (e.g. 'LTO-5', 'LTO-6', 'LTO-10')
    can read and/or write a cartridge of `media_code` ('L1'..'LA', 'M8', 'CU').
    """
    d_gen = (drive_generation or "LTO-5").strip().upper()
    m_spec = get_media_spec(media_code)
    m_code = m_spec["code"]

    if m_code == "CU":
        return {
            "can_read": False,
            "can_write": False,
            "can_clean": True,
            "drive_generation": d_gen,
            "media_code": m_code,
            "reason": "Universal cleaning cartridge compatible with all LTO drives.",
        }

    compat = LTO_DRIVE_COMPATIBILITY.get(d_gen)
    if not compat:
        return {
            "can_read": False,
            "can_write": False,
            "can_clean": False,
            "drive_generation": d_gen,
            "media_code": m_code,
            "reason": f"Unknown drive generation: {d_gen}",
        }

    can_read = m_code in compat["read"]
    can_write = m_code in compat["write"]
    if can_write:
        reason = f"{d_gen} drive supports full Read/Write on {m_spec['generation']} ({m_code}) media."
    elif can_read:
        reason = f"{d_gen} drive supports Read-Only (N-2) on {m_spec['generation']} ({m_code}) media."
    else:
        reason = f"{d_gen} drive is hardware-incompatible with {m_spec['generation']} ({m_code}) media."

    return {
        "can_read": can_read,
        "can_write": can_write,
        "can_clean": False,
        "drive_generation": d_gen,
        "media_code": m_code,
        "reason": reason,
    }
