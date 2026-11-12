"""iQ Student Accommodation scraper (operator id: iq).

Approach (no HTML price parsing needed):
  1. GET /api/explore/properties            -> all buildings (nid, city, coords, universities, priceFrom)
  2. GET / (drupalSettings.searchResults)   -> address + postcode per building nid
  3. GET /api/properties/{nid}/room-types   -> every room type with StarRez contracts
                                               (per-week Amount, weeks, dates, rooms left, deposit, offers)
  4. GET property page HTML                 -> building amenities (div.caption h4)

One row per room type x academic year x contract (tenancy length / start date).
If a room type has no contracts for a year (e.g. fully booked), one row is emitted with the
"price from" value for that year and tenancy_weeks empty.

Run: python -m scrapers.iq [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import time

from bs4 import BeautifulSoup

from .common import Room, normalize_category, save, session

OPERATOR = "iq"
BASE = "https://www.iqstudentaccommodation.com"
DELAY = 0.3

STATUS_MAP = {
    "available": "available",
    "limited_availability": "limited",
    "fully_booked": "sold_out",
    "sold_out": "sold_out",
}


def get_json(s, path, retries=3):
    for i in range(retries):
        try:
            r = s.get(BASE + path)
            if r.status_code == 200 and "json" in (r.headers.get("content-type") or ""):
                return r.json()
            print(f"  ! {path} -> HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            print(f"  ! {path} -> {e}")
        time.sleep(1.5 * (i + 1))
    return None


def drupal_settings(html: str) -> dict:
    m = re.search(r'data-drupal-selector="drupal-settings-json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else {}


def building_addresses(s) -> tuple[dict, dict]:
    """nid -> {address, postcode, ...} from homepage drupalSettings, plus academicYear settings."""
    try:
        ds = drupal_settings(s.get(BASE + "/").text)
    except Exception as e:  # noqa: BLE001
        print(f"  ! homepage settings: {e}")
        return {}, {}
    return (ds.get("searchResults") or {}).get("building") or {}, ds.get("academicYear") or {}


def property_amenities(s, url_path: str) -> list[str]:
    try:
        r = s.get(BASE + url_path)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for h in soup.select("div.caption h4"):
            t = h.get_text(" ", strip=True)
            if t and t not in out and len(t) < 60:
                out.append(t)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  ! amenities {url_path}: {e}")
        return []


def parse_size(txt: str) -> float | None:
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", txt or "")]
    if not nums:
        return None
    return round(sum(nums[:2]) / len(nums[:2]), 1)  # midpoint of "15-16 m²"


def categorize(title: str, flat_type: str, features: list[str]) -> str:
    cat = normalize_category(title)
    if cat != "other":
        return cat
    ft = (flat_type or "").lower()
    t = (title or "").lower()
    if "studio" in ft:
        return "studio"
    if "apartment" in ft or "apartment" in t:
        return "apartment"
    if "private room" in ft or "en-suite" in ft or "en suite" in ft:
        return "ensuite" if any("private bathroom" in f.lower() or "en-suite" in f.lower() for f in features) else "non_ensuite"
    if "twin" in ft or "twin" in t or "shared" in ft:
        return "other"
    return "other"


def by_year(lst, key):
    return {x.get("year"): x.get(key) for x in (lst or []) if isinstance(x, dict)}


def fmt_year(y: str) -> str:
    return (y or "").replace("-", "/")  # 2026-27 -> 2026/27


def nearest_uni(unis: dict) -> str:
    best = None
    for u in (unis or {}).values():
        d = u.get("distance") or {}
        mins = d.get("minutes")
        if best is None or (mins is not None and (best[1] is None or mins < best[1])):
            best = (u.get("name"), mins, d.get("miles"))
    if not best:
        return ""
    return f"{best[0]} ({best[1]} min walk, {best[2]} mi)" if best[1] is not None else best[0]


def scrape(limit: int | None = None) -> list[Room]:
    s = session()
    explore = get_json(s, "/api/explore/properties") or {}
    props = explore.get("properties") or []
    print(f"[iq] {len(props)} properties from /api/explore/properties")
    addr, ay = building_addresses(s)
    print(f"[iq] academicYear settings: {ay}; {len(addr)} building addresses")
    if limit:
        props = props[:limit]

    rows: list[Room] = []
    for i, p in enumerate(props, 1):
        nid = str(p.get("nid") or p.get("id"))
        a = p.get("address") or {}
        coords = a.get("coordinates") or [None, None]
        lng, lat = (coords + [None, None])[:2]
        b = addr.get(nid) or {}
        city = a.get("city") or b.get("city") or ""
        name = p.get("title") or b.get("title") or ""
        purl = BASE + (p.get("searchPageUrl") or "")
        unis = p.get("universities") or {}
        uni_list = [
            {"name": u.get("name"), **(u.get("distance") or {})} for u in unis.values()
        ]

        time.sleep(DELAY)
        rt = get_json(s, f"/api/properties/{nid}/room-types") or {}
        time.sleep(DELAY)
        amenities = property_amenities(s, p.get("searchPageUrl") or "")
        rtypes = rt.get("room_types") or []
        n_before = len(rows)

        for r in rtypes:
            ff = r.get("flatFeatures") or {}
            feats = ff.get("featured") or []
            feat_names = [f.get("name", "") for f in feats]
            size_raw = next((f.get("name") for f in feats if f.get("type") == "Room size" or f.get("icon") == "size"), "")
            bed = next((f.get("name") for f in feats if f.get("type") == "Bed size"), "")
            room_feats = [f for f in feat_names if f not in (size_raw, bed)]
            title = (r.get("title") or "").strip()
            flat_type = ff.get("flatType") or ""
            cat = categorize(title, flat_type, feat_names)
            price_from = by_year(r.get("priceFrom"), "price")
            avail = by_year(r.get("availability"), "status")
            avail_rooms = by_year(r.get("availableRooms"), "count")
            contracts_by_year = by_year(r.get("contracts"), "contracts")
            years = sorted(set(price_from) | set(avail) | set(contracts_by_year), reverse=False)
            base_kw = dict(
                operator=OPERATOR,
                property_name=name,
                city=city,
                room_type=title,
                room_category=cat,
                size_sqm=parse_size(size_raw),
                bed_size=bed,
                address=(b.get("address") or "").strip(),
                postcode=b.get("postcode") or "",
                lat=lat,
                lng=lng,
                university_nearby=nearest_uni(unis),
                amenities=list(dict.fromkeys(amenities + [f"room: {f}" for f in room_feats])),
                property_url=purl,
                room_url=BASE + (r.get("url") or ""),
            )
            for y in years:
                status = avail.get(y) or ""
                common_extra = {
                    "property_nid": nid,
                    "room_type_id": r.get("id"),
                    "starrez_room_type_id": r.get("starRezId"),
                    "flat_type": flat_type,
                    "size_raw": size_raw,
                    "dependent_locality": a.get("dependentLocality"),
                    "price_from_year": price_from.get(y),
                    "available_rooms_year": avail_rooms.get(y),
                    "availability_raw": status,
                    "universities": uni_list,
                }
                seen = set()
                emitted = False
                for c in contracts_by_year.get(y) or []:
                    try:
                        amt = float(c.get("Amount") or 0) or None
                    except ValueError:
                        amt = None
                    weeks = int(c["weeks"]) if str(c.get("weeks") or "").isdigit() else None
                    key = (weeks, amt, c.get("ContractDateStart"), c.get("ContractDateEnd"))
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        n_rooms = int(c.get("NumberOfRooms") or 0)
                    except ValueError:
                        n_rooms = None
                    offers = [
                        {k: o.get(k) for k in ("promotionName", "promotionValue", "promotionCode", "minContractLength")}
                        for o in (c.get("offers") or [])
                        if str(o.get("termId")) == str(c.get("TermID"))
                    ]
                    rows.append(Room(
                        **base_kw,
                        price_per_week_gbp=amt,
                        tenancy_weeks=weeks,
                        academic_year=fmt_year(y),
                        availability=("sold_out" if n_rooms == 0 else STATUS_MAP.get(status, status)),
                        extra={
                            **common_extra,
                            "term": (c.get("TermDescription") or "").lstrip("*"),
                            "contract_start": c.get("ContractDateStart"),
                            "contract_end": c.get("ContractDateEnd"),
                            "rooms_left_contract": n_rooms,
                            "deposit_gbp": float(c["DepositAmount"]) if c.get("DepositAmount") else None,
                            "allow_web_booking": c.get("AllowWebBooking"),
                            "offers": offers,
                        },
                    ))
                    emitted = True
                if not emitted:
                    pf = price_from.get(y)
                    rows.append(Room(
                        **base_kw,
                        price_per_week_gbp=float(pf) if pf else None,
                        academic_year=fmt_year(y),
                        availability=STATUS_MAP.get(status, status),
                        extra={**common_extra, "price_is_from": True},
                    ))
        print(f"[iq] {i}/{len(props)} {city} / {name}: {len(rtypes)} room types, {len(rows) - n_before} rows")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only first N properties")
    args = ap.parse_args()
    rows = scrape(args.limit)
    save(OPERATOR, rows)
    props = {r.property_url for r in rows}
    studios = [r for r in rows if r.room_category == "studio"]
    print(f"[iq] {len(rows)} rows, {len(props)} properties, {len(studios)} studio rows")


if __name__ == "__main__":
    main()
