"""Flatten all operator rows into the team's competitor format: data/competitors.csv.

Columns agreed with the team: price/week, tenancy length, tenure (year + fixed/flexible), room type,
size sqm, deposit, offers, beds left (rarely disclosed), location, amenity flags (gym, cinema,
concierge, security...)."""
import csv
import json
import re
from datetime import datetime

from scrapers.common import DATA_DIR

AMENITY_FLAGS = {
    "gym": r"\bgym|fitness|wellness",
    "cinema": r"cinema|screening|movie",
    "concierge": r"concierge|reception|on.?site (service )?(team|staff)|front desk|onsite service",
    "security_24h": r"24.?(hour|hr|/7)|security|cctv|secure (entry|access)|fob",
    "study_space": r"study",
    "laundry": r"laundr",
    "bike_storage": r"bike|cycle",
    "parking": r"parking|car park",
    "games_room": r"games|pool table|gaming",
    "social_space": r"common room|lounge|social|communal",
    "outdoor_space": r"courtyard|terrace|garden|roof",
    "bills_included": r"bills.{0,15}includ|all.?inclusive|inclusive of bills|utilit|electric.*water",
    "wifi": r"wi.?fi|broadband|internet",
}

COLUMNS = [
    "operator", "property_name", "city", "postcode", "address", "lat", "lng",
    "room_type", "room_category", "price_per_week_gbp", "tenancy_weeks", "academic_year",
    "tenancy_type", "start_date", "end_date", "annual_rent_gbp", "size_sqm", "bed_size",
    "deposit_gbp", "offer", "beds_left", "availability", "university_nearby",
    *[f"has_{k}" for k in AMENITY_FLAGS], "amenities", "property_url", "room_url", "scraped_at",
]


def _date(v):
    if not v:
        return ""
    v = str(v)[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return v


def _offer(x):
    for k in ("offers", "specials", "promo"):
        v = x.get(k)
        if not v:
            continue
        if isinstance(v, list):
            return " | ".join(o.get("promotionName", "") if isinstance(o, dict) else str(o) for o in v)
        return str(v)
    return ""


def _beds_left(x):
    for k in ("rooms_left_contract", "rooms_available", "beds_available_count", "available_count"):
        if x.get(k) not in (None, ""):
            return int(float(x[k]))
    return ""


def flatten(r):
    x = r.get("extra") or {}
    amen = list(r.get("amenities") or [])
    for k in ("property_amenities", "room_amenities", "standard_features"):
        amen += [a for a in (x.get(k) or []) if isinstance(a, str)]
    amen = list(dict.fromkeys(a.strip() for a in amen if a and a.strip()))
    text = " ; ".join(a for a in amen if "other property" not in a.lower()).lower() + " " + str(x.get("overview", "")).lower()
    pw, wk = r.get("price_per_week_gbp"), r.get("tenancy_weeks")
    out = {k: r.get(k, "") for k in COLUMNS if k in r}
    out.update(
        tenancy_type=x.get("tenancy_type", ""),
        start_date=_date(x.get("start_date") or x.get("contract_start") or x.get("check_in")),
        end_date=_date(x.get("end_date") or x.get("contract_end") or x.get("check_out")),
        annual_rent_gbp=round(pw * wk, 2) if pw and wk else "",
        deposit_gbp=x.get("deposit_gbp", ""),
        offer=_offer(x),
        beds_left=_beds_left(x),
        amenities=" ; ".join(amen),
    )
    for k, pat in AMENITY_FLAGS.items():
        out[f"has_{k}"] = int(bool(re.search(pat, text)))
    return out


def write(rows):
    flat = [flatten(r) for r in rows]
    with (DATA_DIR / "competitors.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(flat)
    return flat
