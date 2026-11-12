"""Yugo (yugo.com) PBSA scraper.

Uses Yugo's internal JSON endpoints (same ones the booking widget calls):
  /en-gb/countries                         -> countryId
  /en-gb/cities?countryId=                 -> cities (contentId)
  /en-gb/residences?cityId=                -> residences (id = base64 property id, contentId, amenities)
  /en-gb/map/<cityContentId>               -> residence address, lat/lng, property URL, nearby universities
  /en-gb/rooms?residenceId=                -> room types (min price, soldOut, count, bathroom arrangement)
  /en-gb/tenancyOptionsBySSId?residenceId=&residenceContentId=&roomId=
                                           -> academic years x tenancy options with weekly rent
Room size (sq m) and bed size are parsed from the public room-type pages, discovered via the sitemap.

Usage: python -m scrapers.yugo [--limit N] [--country gb]
"""
from __future__ import annotations

import argparse
import gzip
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from bs4 import BeautifulSoup

from scrapers.common import Room, normalize_category, save, session

OPERATOR = "yugo"
BASE = "https://yugo.com"
LOC = "/en-gb"
DELAY = 0.3
WORKERS = 3

COUNTRY_CODES = {
    "gb": "UK", "uk": "UK", "ie": "Ireland", "us": "USA", "es": "Spain", "de": "Germany",
    "fr": "France", "it": "Italy", "at": "Austria", "au": "Australia", "ae": "UAE", "pl": "Poland",
}

_tls = threading.local()


def _s():
    if not hasattr(_tls, "s"):
        _tls.s = session()
    return _tls.s


def get(path: str, params: dict | None = None, as_json: bool = True, retries: int = 3):
    url = path if path.startswith("http") else BASE + path
    for i in range(retries):
        time.sleep(DELAY)
        try:
            r = _s().get(url, params=params)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            if r.status_code == 404:
                return None
        except Exception as e:  # noqa: BLE001
            print(f"  ! {url} {e}")
        time.sleep(1.5 * (i + 1))
    print(f"  ! giving up {url} {params}")
    return None


# ---------- helpers ----------

def money(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"([\d,]+(?:\.\d+)?)", str(s))
    return float(m.group(1).replace(",", "")) if m else None


POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b")


def norm_name(s: str) -> str:
    s = (s or "").lower().replace("en-suite", "ensuite").replace("en suite", "ensuite")
    return re.sub(r"[^a-z0-9]", "", s)


def weeks_from(opt: dict) -> int | None:
    m = re.search(r"(\d+)\s*-?\s*week", opt.get("name") or "", re.I)
    if m:
        return int(m.group(1))
    try:
        d0, d1 = date.fromisoformat(opt["startDate"]), date.fromisoformat(opt["endDate"])
        if (opt.get("tenancyType") or "").upper() == "FIXED":
            return round(((d1 - d0).days + 1) / 7)
    except Exception:  # noqa: BLE001
        pass
    return None


SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*)?(?:sq\.?\s*m(?:etres|eters|etre|eter)?\b|sqm\b|m²|m2\b|square\s*met)",
    re.I,
)
BED_RE = re.compile(r"\b(small double|king[- ]size|king|queen|three[- ]quarter|double|single|twin)\s*(?:sized?\s*)?bed", re.I)


def parse_room_page(html: str) -> dict:
    out: dict = {}
    sa = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html)
    if sa:
        out["street_address"] = sa.group(1).strip()
    soup = BeautifulSoup(html, "html.parser")
    for x in soup(["svg", "script", "style", "noscript"]):
        x.decompose()
    txt = re.sub(r"\s+", " ", soup.get_text(" "))
    h1 = soup.find("h1")
    out["title"] = h1.get_text(" ", strip=True) if h1 else ""
    sizes = []
    for m in SIZE_RE.finditer(txt):
        a = float(m.group(1))
        b = float(m.group(2)) if m.group(2) else None
        if 5 <= a <= 120:
            sizes.append((a, b, txt[max(0, m.start() - 40): m.end() + 10]))
    if sizes:
        a, b, ctx = sizes[0]
        out["size_sqm"] = a
        out["size_max_sqm"] = b
        out["size_raw"] = ctx.strip()
    bm = BED_RE.search(txt)
    if bm:
        out["bed_size"] = bm.group(1).lower().replace("-", " ")
    return out


# ---------- discovery ----------

_sitemap_cache: list[str] | None = None


def sitemap_urls() -> list[str]:
    global _sitemap_cache
    if _sitemap_cache is not None:
        return _sitemap_cache
    urls: list[str] = []
    idx = get("/service-sitemap-en-gb-sitemap_index.xml", as_json=False) or ""
    for loc in re.findall(r"<loc>([^<]+)</loc>", idx):
        time.sleep(DELAY)
        try:
            c = _s().get(loc).content
            try:
                t = gzip.decompress(c).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                t = c.decode("utf-8", "replace")
            urls += re.findall(r"<loc>([^<]+)</loc>", t)
        except Exception as e:  # noqa: BLE001
            print(f"  ! sitemap {loc}: {e}")
    _sitemap_cache = urls
    print(f"[yugo] sitemap: {len(urls)} urls")
    return urls


def city_map(city_id) -> dict:
    """contentId -> {address, lat, lng, url}, plus list of universities."""
    d = get(f"{LOC}/map/{city_id}") or {}
    res, unis = {}, []
    for mk in d.get("markers", []):
        if mk.get("type") == "residence" and mk.get("link"):
            res[mk["link"].get("contentId")] = {
                "address": mk.get("subTitle") or "",
                "lat": float(mk["lat"]) if mk.get("lat") else None,
                "lng": float(mk["lng"]) if mk.get("lng") else None,
                "url": mk["link"].get("target") or "",
                "title": mk.get("title") or "",
            }
        elif mk.get("type") == "universities":
            unis.append(mk.get("title", "").strip())
    return {"residences": res, "universities": [u for u in unis if u]}


def residence_room_pages(prop_url: str) -> dict:
    """norm room name -> parsed room page dict (size, bed, url)."""
    if not prop_url:
        return {}
    prefix = prop_url.rstrip("/") + "/"
    out = {}
    for u in sitemap_urls():
        if not u.startswith(prefix) or "/" in u[len(prefix):]:
            continue
        html = get(u, as_json=False)
        if not html:
            continue
        info = parse_room_page(html)
        info["url"] = u
        slug = re.sub(r"-\d+$", "", u[len(prefix):])
        keys = {norm_name(slug)}
        if info.get("title"):
            keys.add(norm_name(info["title"]))
        for k in keys:
            out.setdefault(k, info)
    return out


def match_page(pages: dict, room_name: str) -> dict:
    k = norm_name(room_name)
    if k in pages:
        return pages[k]
    # fuzzy: page key contained in room name or vice versa (prefer longest)
    best = None
    for pk, info in pages.items():
        if pk and (pk in k or k in pk):
            if best is None or len(pk) > len(best[0]):
                best = (pk, info)
    return best[1] if best else {}


# ---------- scraping ----------

def availability(room: dict) -> str:
    if room.get("soldOut"):
        return "sold_out"
    if room.get("comingSoon"):
        return "coming_soon"
    if room.get("enquireNow"):
        return "enquire"
    return "available"


def scrape_residence(city: dict, res: dict, geo: dict, unis: list[str]) -> list[Room]:
    rows: list[Room] = []
    rid, cid = res["id"], res["contentId"]
    g = geo.get(cid, {})
    addr = g.get("address") or ""
    pc = POSTCODE_RE.search(addr)
    rooms = (get(f"{LOC}/rooms", {"residenceId": rid}) or {}).get("rooms", [])
    if not rooms:
        print(f"  - {city['name']} / {res['name']}: no room types listed")
        return rows
    pages = residence_room_pages(g.get("url", ""))
    if not addr:  # map marker had no subtitle: fall back to schema.org address on room pages
        addr = next((p["street_address"] for p in pages.values() if p.get("street_address")), "")
        pc = POSTCODE_RE.search(addr)
    base = dict(
        operator=OPERATOR, property_name=res["name"], city=city["name"],
        address=addr, postcode=pc.group(1) if pc else "",
        lat=g.get("lat"), lng=g.get("lng"),
        university_nearby="; ".join(unis[:6]),
        amenities=res.get("amenities") or [], property_url=g.get("url", ""),
    )
    for room in rooms:
        page = match_page(pages, room["name"])
        cat = normalize_category(room["name"])
        low = room["name"].lower()
        if cat == "non_ensuite" and "suite" in low and "non" not in low:
            cat = "ensuite"  # e.g. "Standard Ensuite" (common.py treats "standard" as non-ensuite)
        if cat == "other" and "private bathroom" in (room.get("bathroomArrangement") or "").lower():
            cat = "ensuite"
        common = dict(
            base, room_type=room["name"], room_category=cat,
            size_sqm=page.get("size_sqm"), bed_size=page.get("bed_size", ""),
            availability=availability(room), room_url=page.get("url", ""),
        )
        rextra = {
            "room_type_id": room.get("idOriginal"), "residence_content_id": cid,
            "beds_available_count": room.get("count"), "price_label": room.get("priceLabel"),
            "min_price_per_night": room.get("minPricePerNight"),
            "bathroom": room.get("bathroomArrangement"), "kitchen": room.get("kitchenArrangement"),
            "max_beds_in_flat": room.get("maxNumOfBedsInFlat"), "promo": room.get("promoMessage"),
            "size_max_sqm": page.get("size_max_sqm"), "size_raw": page.get("size_raw"),
        }
        ten = get(f"{LOC}/tenancyOptionsBySSId", {"residenceId": rid, "residenceContentId": cid, "roomId": room["id"]}) or {}
        n_opts = 0
        for ay in ten.get("tenancy-options") or []:
            year = f"{ay.get('fromYear')}/{str(ay.get('toYear'))[-2:]}" if ay.get("fromYear") else ""
            for opt in ay.get("tenancyOption") or []:
                n_opts += 1
                rows.append(Room(
                    **common,
                    price_per_week_gbp=money(opt.get("rent")) or room.get("minPriceForBillingCycle"),
                    tenancy_weeks=weeks_from(opt), academic_year=year,
                    extra=dict(rextra, tenancy_name=opt.get("name"), tenancy_type=opt.get("tenancyType"),
                               start_date=opt.get("startDate"), end_date=opt.get("endDate"),
                               min_nights=opt.get("tenancyLength") if opt.get("tenancyLengthUnit") == "NIGHTS" else None,
                               tenancy_label=opt.get("formattedLabel"), tenancy_status=opt.get("status"),
                               academic_year_enabled=ay.get("enabled")),
                ))
        if not n_opts:  # no tenancy data: still emit room with the "from" price
            rows.append(Room(**common, price_per_week_gbp=room.get("minPriceForBillingCycle"),
                             extra=dict(rextra, tenancy_error=(ten.get("error") or None))))
    print(f"  + {city['name']} / {res['name']}: {len(rooms)} room types, {len(rows)} rows")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max residences")
    ap.add_argument("--country", default="gb")
    args = ap.parse_args()

    cname = COUNTRY_CODES.get(args.country.lower(), args.country)
    countries = (get(f"{LOC}/countries") or {}).get("countries", [])
    country = next((c for c in countries if c["name"].lower() == cname.lower()), None)
    if not country:
        raise SystemExit(f"country {args.country} not found in {[c['name'] for c in countries]}")
    cities = (get(f"{LOC}/cities", {"countryId": country["countryId"]}) or {}).get("cities", [])
    print(f"[yugo] {country['name']}: {len(cities)} cities")

    jobs = []
    for city in cities:
        rs = (get(f"{LOC}/residences", {"cityId": city["contentId"]}) or {}).get("residences", [])
        if not rs:
            continue
        cm = city_map(city["contentId"])
        for r in rs:
            jobs.append((city, r, cm["residences"], cm["universities"]))
        if args.limit and len(jobs) >= args.limit:
            break
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"[yugo] {len(jobs)} residences to scrape")
    sitemap_urls()

    rows: list[Room] = []
    with ThreadPoolExecutor(WORKERS) as ex:
        for res in ex.map(lambda j: scrape_residence(*j), jobs):
            rows.extend(res)
    save(OPERATOR, rows)
    props = {r.property_name for r in rows}
    studios = [r for r in rows if r.room_category == "studio"]
    print(f"[yugo] {len(rows)} rows, {len(props)} properties, {len(studios)} studio rows")


if __name__ == "__main__":
    main()
