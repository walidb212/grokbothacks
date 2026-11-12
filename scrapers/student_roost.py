"""Student Roost (studentroost.co.uk) scraper.

Data sources (no HTML parsing):
  * Craft CMS GraphQL (public, cached): POST https://www.studentroost.co.uk/api/gql
      - section "properties": property meta (address, lat/lng, Kx/Entrata ids, amenities)
      - sections "rooms" (Kinetics booking engine, 2026/27) and "roomTypes" (Entrata, 2027/28):
        room types with kxRoomType id, sold out flags, bullet list (size m2, tenancy weeks, bed)
  * Live pricing proxy (Nuxt server API):
      - GET /api/kx?property=<kxProperty|entrataId> -> [{roomTypeId, year, pricePerWeek, roomsAvailable}]
      - GET /api/rawKx?property=<kxPropertyTitle>&year=2026 - 2027 -> per tenancy option
        (contractLength weeks, startDate, endDate, pricePerWeek, roomsAvailable). Kinetics only.

Run: python -m scrapers.student_roost [--limit N]
"""
from __future__ import annotations

import argparse
import re
import time

from scrapers.common import Room, normalize_category, save, session

OP = "student_roost"
BASE = "https://www.studentroost.co.uk"
GQL = BASE + "/api/gql"
DELAY = 0.3

PROPS_Q = """
{ entries(siteId: "1", section: "properties") {
    id title url
    ... on propertiesEntry_Entry {
      kxProperty kxPropertyTitle kxPriceFrom kxRoomTypes entrataId isSoldOut hideRooms
      latitude longitude plainText studentCrowdRating
      entriesLocations { id title ... on locationsEntry_Entry { latitude longitude } }
      entriesUniversities { title }
      categoriesRoomType { title }
      categoriesAcademicYear { title }
      listDetails { ... on list_Entry { title listSimple { ... on listItem_Entry { plainTextMultiLine } } } }
      listSimple { ... on listItem_Entry { plainTextMultiLine } }
    }
} }
"""

ROOM_FIELDS = """
    id title url sectionHandle
    ... on room_Entry {
      kxRoomType kxRoomTitle roomTypeExternalId soldOut almostGone availability priceOverride accessible
      categoriesAcademicYear { title }
      entriesRoomGroups { id title ... on roomGroups_Entry { heading entriesProperties { id } } }
      listSimple { ... on listItem_Entry { plainTextMultiLine } }
    }
"""
ROOMS_Q = "{ rooms: entries(siteId: \"1\", section: \"rooms\") {%s} roomTypes: entries(siteId: \"1\", section: \"roomTypes\") {%s} }" % (ROOM_FIELDS, ROOM_FIELDS)

YEARS_Q = '{ categories(siteId: "1", group: "academicYear") { title ... on academicYear_Category { restricted bookingEngine } } }'


def gql(s, q: str) -> dict:
    r = s.post(GQL, json={"query": q})
    r.raise_for_status()
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(j["errors"])
    return j["data"]


def get_json(s, path, params=None):
    time.sleep(DELAY)
    try:
        r = s.get(BASE + path, params=params)
        if r.status_code != 200:
            return None
        j = r.json()
        return None if isinstance(j, dict) and "Error" in j else j
    except Exception as e:  # noqa: BLE001
        print(f"  ! {path} {params}: {e}")
        return None


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def year_label(y: str) -> str:
    """'2026 - 2027' -> '2026/27'."""
    m = re.match(r"\s*(\d{4})\s*-\s*(\d{4})", y or "")
    return f"{m.group(1)}/{m.group(2)[2:]}" if m else (y or "")


def bullets(items) -> list[str]:
    return [i["plainTextMultiLine"].replace("\ufeff", "").strip() for i in (items or []) if i and i.get("plainTextMultiLine")]


SQM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(\d+(?:\.\d+)?)?\s*(?:m2|m²|m²|sq\s*m|sqm|m�|square met)", re.I)


def parse_size(lines):
    for l in lines:
        m = SQM_RE.search(l)
        if m:
            lo = float(m.group(1))
            hi = float(m.group(2)) if m.group(2) else lo
            return round((lo + hi) / 2, 1), l
    return None, ""


def parse_weeks(lines):
    for l in lines:
        if "week" in l.lower():
            nums = [int(n) for n in re.findall(r"\b(\d{2})\b", l) if 20 <= int(n) <= 55]
            if nums:
                return nums, l
    return [], ""


def parse_bed(lines):
    for l in lines:
        m = re.search(r"((?:small |king|queen|super ?king )?(?:double|single|three-quarter|3/4|king|queen)[- ]?(?:size )?bed)", l, re.I)
        if m:
            return m.group(1)
    return ""


def fix_lng(lng, loc_lng):
    """Some CMS entries drop the minus sign on longitude; trust the city's sign if magnitudes match."""
    if lng is None or loc_lng is None:
        return lng if lng is not None else loc_lng
    if (lng > 0) != (loc_lng > 0) and abs(abs(lng) - abs(loc_lng)) < 0.5:
        return -lng
    return lng


def avail_label(rooms_available, sold_out=False, almost_gone=False):
    if rooms_available is not None:
        n = int(rooms_available)
        if n <= 0:
            return "sold_out"
        if n <= 5 or almost_gone:
            return "limited"
        return "available"
    if sold_out:
        return "sold_out"
    if almost_gone:
        return "limited"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max properties")
    args = ap.parse_args()
    s = session()
    s.headers.update({"Accept": "application/json", "Referer": BASE + "/"})

    try:
        years = gql(s, YEARS_Q)["categories"]
        year_titles = [y["title"] for y in years if not y.get("restricted")]
    except Exception:  # noqa: BLE001
        year_titles = []
    if not year_titles:
        year_titles = ["2026 - 2027", "2027 - 2028"]
    print(f"[{OP}] academic years: {year_titles}")

    props = [p for p in gql(s, PROPS_Q)["entries"] if p.get("title")]
    print(f"[{OP}] {len(props)} properties")
    rd = gql(s, ROOMS_Q)
    all_rooms = (rd.get("rooms") or []) + (rd.get("roomTypes") or [])
    print(f"[{OP}] {len(all_rooms)} room entries (rooms + roomTypes)")

    rooms_by_prop: dict[str, list] = {}
    for r in all_rooms:
        pids = {p["id"] for g in (r.get("entriesRoomGroups") or []) for p in (g.get("entriesProperties") or [])}
        for pid in pids:
            rooms_by_prop.setdefault(pid, []).append(r)

    if args.limit:
        props = props[: args.limit]

    rows: list[Room] = []
    for i, p in enumerate(props, 1):
        loc = (p.get("entriesLocations") or [{}])[0]
        city = loc.get("title", "")
        lat = fnum(p.get("latitude")) or fnum(loc.get("latitude"))
        lng = fix_lng(fnum(p.get("longitude")), fnum(loc.get("longitude")))
        address = (p.get("plainText") or "").strip()
        pc = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", address.upper())
        amenities = []
        for d in p.get("listDetails") or []:
            amenities += bullets(d.get("listSimple"))
        amenities += bullets(p.get("listSimple"))
        amenities = list(dict.fromkeys(amenities))
        unis = ", ".join(u["title"] for u in (p.get("entriesUniversities") or []) if u.get("title"))

        # live price summaries: try both Kinetics property id and Entrata id
        kx_rows = []
        for pid in dict.fromkeys(str(x) for x in (p.get("kxProperty"), p.get("entrataId")) if x):
            if pid:
                kx_rows += get_json(s, "/api/kx", {"property": str(pid)}) or []
        kx_idx = {}
        for k in kx_rows:
            kx_idx.setdefault(str(k.get("roomTypeId")), []).append(k)

        # per-tenancy options (Kinetics only)
        raw_idx: dict[tuple, list] = {}
        if p.get("kxPropertyTitle"):
            years_with_data = {k.get("year") for k in kx_rows} or set(year_titles)
            for y in year_titles:
                if y not in years_with_data:
                    continue
                for o in get_json(s, "/api/rawKx", {"property": p["kxPropertyTitle"], "year": y}) or []:
                    raw_idx.setdefault((str(o.get("roomTypeId")), y), []).append(o)

        prop_rooms = rooms_by_prop.get(p["id"], [])
        n0 = len(rows)
        for r in prop_rooms:
            rtid = str(r.get("kxRoomType") or r.get("roomTypeExternalId") or "")
            label = r.get("kxRoomTitle") or r.get("title") or ""
            groups = r.get("entriesRoomGroups") or []
            gtitle = (groups[0].get("heading") or groups[0].get("title")) if groups else ""
            cat = normalize_category(label)
            if cat == "other":
                cat = normalize_category(gtitle)
            lines = bullets(r.get("listSimple"))
            size, size_raw = parse_size(lines)
            weeks_list, weeks_raw = parse_weeks(lines)
            excluded_years = [c["title"] for c in (r.get("categoriesAcademicYear") or [])]
            base = dict(
                operator=OP, property_name=p["title"], city=city, room_type=label, room_category=cat,
                size_sqm=size, bed_size=parse_bed(lines), address=address,
                postcode=pc.group(1) if pc else "", lat=lat, lng=lng, university_nearby=unis,
                amenities=amenities, property_url=p.get("url") or "", room_url=r.get("url") or "",
            )
            extra_base = {
                "cms_section": r.get("sectionHandle"), "cms_room_id": r.get("id"), "kx_room_type_id": rtid,
                "room_group": gtitle, "room_bullets": lines, "size_raw": size_raw,
                "tenancy_weeks_options_cms": weeks_list, "tenancy_raw": weeks_raw,
                "accessible": r.get("accessible"), "cms_sold_out": r.get("soldOut"),
                "almost_gone": r.get("almostGone"), "hidden_for_years": excluded_years,
                "kx_property": p.get("kxProperty"), "entrata_id": p.get("entrataId"),
                "property_price_from": fnum(p.get("kxPriceFrom")),
            }
            summaries = kx_idx.get(rtid, [])
            emitted = False
            for k in summaries:
                y = k.get("year", "")
                if y not in year_titles:  # skip past academic years still cached in Kx
                    continue
                opts = raw_idx.get((rtid, y), [])
                if opts:
                    dedup = {}
                    for o in opts:
                        key = (o.get("contractLength"), o.get("startDate"), o.get("endDate"), o.get("pricePerWeek"))
                        dedup[key] = o
                    for (wk, sd, ed, pr), o in sorted(dedup.items(), key=lambda kv: (-(int(kv[0][0] or 0)), kv[0][1] or "")):
                        rows.append(Room(
                            **base, price_per_week_gbp=fnum(pr), tenancy_weeks=int(wk) if wk else None,
                            academic_year=year_label(y),
                            availability=avail_label(fnum(o.get("roomsAvailable")), r.get("soldOut"), r.get("almostGone")),
                            extra={**extra_base, "start_date": sd, "end_date": ed,
                                   "rooms_available": fnum(o.get("roomsAvailable")),
                                   "price_from_week": fnum(k.get("pricePerWeek")), "source": "rawKx"},
                        ))
                else:
                    rows.append(Room(
                        **base, price_per_week_gbp=fnum(k.get("pricePerWeek")),
                        tenancy_weeks=max(weeks_list) if weeks_list else None,
                        academic_year=year_label(y),
                        availability=avail_label(fnum(k.get("roomsAvailable")), r.get("soldOut"), r.get("almostGone")),
                        extra={**extra_base, "rooms_available": fnum(k.get("roomsAvailable")),
                               "price_is": "from_price_per_week", "source": "kx"},
                    ))
                emitted = True
            if not emitted:
                rows.append(Room(
                    **base, price_per_week_gbp=fnum(r.get("priceOverride")),
                    tenancy_weeks=max(weeks_list) if weeks_list else None,
                    academic_year="", availability=avail_label(None, r.get("soldOut"), r.get("almostGone")),
                    extra={**extra_base, "source": "cms_only"},
                ))
        # same kx room type can be both a "rooms" and "roomTypes" entry for same year: dedupe
        uniq, keys = [], set()
        for row in rows[n0:]:
            key = (row.room_type, row.academic_year, row.tenancy_weeks, row.price_per_week_gbp,
                   row.extra.get("start_date"), row.extra.get("kx_room_type_id"))
            if key not in keys:
                keys.add(key)
                uniq.append(row)
        rows[n0:] = uniq
        print(f"  [{i}/{len(props)}] {city} / {p['title']}: {len(prop_rooms)} room entries, {len(kx_rows)} kx prices -> {len(rows) - n0} rows")

    save(OP, rows)
    studios = [r for r in rows if r.room_category == "studio"]
    print(f"[{OP}] rows={len(rows)} properties={len({r.property_name for r in rows})} studios={len(studios)}")


if __name__ == "__main__":
    main()
