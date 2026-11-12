"""Shared schema + helpers for UK student accommodation (PBSA) competitor scrapers."""
from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from curl_cffi import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UA_IMPERSONATE = "chrome"


@dataclass
class Room:
    """One bookable room type at one property (one row per room type x tenancy option if prices differ)."""
    operator: str                 # unite, iq, homes_for_students, student_roost, yugo, crm
    property_name: str
    city: str
    room_type: str                # raw label from site, e.g. "Gold Studio", "Classic En-suite"
    room_category: str = ""       # normalized: studio | ensuite | non_ensuite | apartment | twodio | other
    price_per_week_gbp: float | None = None
    tenancy_weeks: int | None = None
    academic_year: str = ""       # e.g. "2026/27"
    size_sqm: float | None = None
    bed_size: str = ""
    availability: str = ""        # available | sold_out | waitlist | limited | raw text
    address: str = ""
    postcode: str = ""
    lat: float | None = None
    lng: float | None = None
    university_nearby: str = ""
    amenities: list[str] = field(default_factory=list)
    property_url: str = ""
    room_url: str = ""
    scraped_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    extra: dict = field(default_factory=dict)


def normalize_category(label: str) -> str:
    l = (label or "").lower()
    if "twodio" in l or "two bed studio" in l:
        return "twodio"
    if "studio" in l:
        return "studio"
    if "non" in l and "suite" in l or "standard" in l or "shared bathroom" in l:
        return "non_ensuite"
    if "suite" in l:
        return "ensuite"
    if "apartment" in l or "flat" in l or "bed" in l:
        return "apartment"
    return "other"


def session() -> requests.Session:
    return requests.Session(impersonate=UA_IMPERSONATE, timeout=40)


def save(operator: str, rows: list, fmt=("json", "csv")) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    dicts = [asdict(r) if hasattr(r, "__dataclass_fields__") else r for r in rows]
    base = DATA_DIR / operator
    if "json" in fmt:
        base.with_suffix(".json").write_text(json.dumps(dicts, ensure_ascii=False, indent=1), encoding="utf-8")
    if "csv" in fmt and dicts:
        keys = list(dicts[0].keys())
        with base.with_suffix(".csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for d in dicts:
                w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in d.items()})
    print(f"[{operator}] saved {len(dicts)} rows -> {base}.json/.csv")
    return base
