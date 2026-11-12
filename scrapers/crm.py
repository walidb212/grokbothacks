"""CRM Students scraper (operator id: crm).

CRM Students (crm-students.com) is part of Yugo and runs on the same CoreMedia platform.
Today almost every CRM city page (crm-students.com/<city>) 301-redirects to the Yugo city page
(yugo.com/en-gb/global/united-kingdom/<city>); only a couple of residences still live natively on
crm-students.com (found via CRM's own search JSON).

Pipeline (all plain HTTP + JSON, no browser):
  1. CRM city slugs        <- crm-students.com homepage nav (fallback: hardcoded list)
  2. city -> residences    <- yugo.com city page -> /en-gb/map/<cityContentId> JSON (name, lat, lng, link)
     + native CRM residences <- crm-students.com/service/search/json/719806 (productGroups)
  3. residence -> rooms    <- <residence>/rooms page, embedded `searchData.results` JSON (roomTypes)
     residence address     <- residence page `.address-desc`
  4. room -> tenancies     <- /en-gb/tenancyOptionsByContentId/<roomContentId> JSON
                              (academic years, tenancy options, rent/week, availability count, arrangements)
     room size / bed       <- room page description ("Room sizes can vary from 17.1m2 to 18.8m2", "3/4 double bed")

One row per room type x tenancy option x academic year.

Usage: python -m scrapers.crm [--limit N]   (N = max residences)
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

from scrapers.common import Room, normalize_category, save, session

OP = "crm"
CRM = "https://www.crm-students.com"
YUGO = "https://yugo.com"
DELAY = 0.3
WORKERS = 4

FALLBACK_CITIES = [
    "aldershot", "bristol", "canterbury", "cardiff", "colchester", "edinburgh", "exeter", "falmouth",
    "hatfield", "kingston", "london", "newcastle", "oxford", "penryn", "plymouth", "sheffield", "swansea",
]

_TL = threading.local()


def S():
    if not hasattr(_TL, "s"):
        _TL.s = session()
    return _TL.s


def get(url: str, as_json=False, tries=3):
    for i in range(tries):
        try:
            time.sleep(DELAY)
            r = S().get(url, allow_redirects=True)
            if r.status_code == 200:
                return (r.json() if as_json else r.text), str(r.url)
            if r.status_code in (404, 410):
                return None, str(r.url)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {url}: {e}", file=sys.stderr)
        time.sleep(1.5 * (i + 1))
    return None, url


def money(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"([\d,]+(?:\.\d+)?)", str(s))
    return float(m.group(1).replace(",", "")) if m else None


def fnum(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def geocode_postcode(pc: str):
    if not pc:
        return None, None
    d, _ = get("https://api.postcodes.io/postcodes/" + pc.replace(" ", ""), as_json=True)
    r = (d or {}).get("result") or {}
    return r.get("latitude"), r.get("longitude")


def search_data(page: str) -> dict:
    i = page.find("searchData.results = ")
    if i < 0:
        return {}
    try:
        d, _ = json.JSONDecoder().raw_decode(page[i + len("searchData.results = "):])
        return d
    except Exception:  # noqa: BLE001
        return {}


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


UK_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b")


# ---------------------------------------------------------------- discovery
def crm_cities() -> list[str]:
    page, _ = get(CRM + "/")
    if not page:
        return FALLBACK_CITIES
    found = []
    for slug in re.findall(r'href="/([a-z\-]+)"', page):
        if slug in FALLBACK_CITIES and slug not in found:
            found.append(slug)
    # any extra single-segment city slugs we don't know get picked up only if in fallback; keep union
    for c in FALLBACK_CITIES:
        if c not in found:
            found.append(c)
    return found


def city_residences(slug: str) -> list[dict]:
    """Follow crm-students.com/<city> (redirects to yugo) and read the city map JSON."""
    page, final = get(f"{CRM}/{slug}")
    if not page:
        return []
    base = re.match(r"https?://[^/]+", final).group(0)
    path = re.sub(r"https?://[^/]+", "", final).rstrip("/")
    m = re.search(r'href="' + re.escape(path) + r'"[^>]*data-attr="(/[^"]*map/\d+)"', page)
    out = []
    if m:
        mp, _ = get(base + m.group(1), as_json=True)
        for mk in (mp or {}).get("markers", []):
            if mk.get("type") != "residence":
                continue
            link = (mk.get("link") or {})
            out.append({
                "name": mk.get("title", ""), "url": link.get("target", ""),
                "content_id": link.get("contentId"),
                "lat": fnum(mk.get("lat")), "lng": fnum(mk.get("lng")),
                "city_slug": slug, "source": "yugo.com (crm city redirect)" if "yugo.com" in final else "crm-students.com",
                "universities": [x.get("title", "") for x in mp.get("markers", []) if x.get("type") == "universities"],
            })
    if not out:  # fallback: residence links from the city page
        for href in sorted(set(re.findall(r'href="([^"]*/' + re.escape(slug) + r'/[a-z0-9\-]+)/rooms"', page))):
            out.append({"name": href.rstrip("/").split("/")[-1].replace("-", " ").title(), "url": urljoin(base, href),
                        "content_id": None, "lat": None, "lng": None, "city_slug": slug,
                        "source": "yugo.com (crm city redirect)" if "yugo.com" in final else "crm-students.com",
                        "universities": []})
    return out


def native_crm_residences() -> list[dict]:
    d, _ = get(f"{CRM}/service/search/json/719806?query=***&facetFilters=documenttype%3AGSARoomType", as_json=True)
    out = []
    for g in (d or {}).get("productGroups", []) or (d or {}).get("groups", []):
        ri = g.get("residenceInfo", {})
        out.append({"name": ri.get("residenceName", ""), "url": ri.get("residenceLink", ""), "content_id": None,
                    "lat": None, "lng": None, "city_slug": (ri.get("cityName") or "").lower(),
                    "source": "crm-students.com", "universities": [], "alt_groups": [g]})
    return out


# ---------------------------------------------------------------- per residence
def parse_room_page(page: str) -> dict:
    info = {}
    m = re.search(r'<div class="product__description[^"]*"[^>]*>(.*?)</div>', page, re.S)
    desc = text_of(m.group(1)) if m else ""
    if not desc:
        m = re.search(r'"@type": "Product".*?"description": "(.*?)",\s*\n', page, re.S)
        desc = htmllib.unescape(m.group(1)) if m else ""
    sizes = [float(x) for x in re.findall(r"(\d{1,3}(?:\.\d+)?)\s*(?:m2|m²|sqm|sq\.?\s?m\b|square met)", desc, re.I)]
    sizes = [x for x in sizes if 5 <= x <= 150]
    if sizes:
        info["size_sqm"] = min(sizes)
        if len(sizes) > 1:
            info["size_sqm_max"] = max(sizes)
    bm = re.search(r"((?:3/4|small|king|queen|single|double|twin)[\w /]*?bed)\b", desc, re.I)
    if bm:
        info["bed_size"] = bm.group(1).strip()
    info["description"] = desc[:600]
    m = re.search(r'tenancies-link="([^"]+)"', page)
    if m:
        info["tenancies_link"] = m.group(1)
    return info


def scrape_room(rt: dict, base: str) -> tuple[dict, dict | None]:
    page, _ = get(rt["link"]) if rt.get("link") else (None, None)
    info = parse_room_page(page) if page else {}
    link = info.get("tenancies_link") or f"/en-gb/tenancyOptionsByContentId/{rt['contentId']}"
    ten, _ = get(urljoin(base, link), as_json=True)
    return info, ten


def year_label(fy, ty) -> str:
    try:
        return f"{int(fy)}/{str(int(ty))[-2:]}" if int(fy) else ""
    except Exception:  # noqa: BLE001
        return ""


def weeks_from(opt: dict) -> int | None:
    m = re.search(r"(\d{1,2})\s*week", opt.get("name") or "", re.I)
    if m:
        return int(m.group(1))
    try:
        from datetime import date
        a, b = date.fromisoformat(opt["startDate"]), date.fromisoformat(opt["endDate"])
        if opt.get("tenancyLengthType") != "EQ" and (b - a).days > 0:
            return round((b - a).days / 7)
    except Exception:  # noqa: BLE001
        pass
    n = opt.get("tenancyLength")
    if n and (opt.get("tenancyLengthUnit") or "").upper() == "NIGHTS" and opt.get("tenancyLengthType") == "EQ":
        return round(int(n) / 7)
    return None


def scrape_residence(res: dict) -> list[Room]:
    url = res["url"].rstrip("/")
    rooms_page, _ = get(url + "/rooms")
    sd = search_data(rooms_page or "")
    groups = sd.get("groups") or sd.get("productGroups") or res.get("alt_groups") or []
    base = re.match(r"https?://[^/]+", url).group(0)
    if not groups:
        print(f"  - {res['name']}: no room data", file=sys.stderr)
        return []
    res_page, _ = get(url)
    address = ""
    if res_page:
        m = re.search(r'class="address-desc[^"]*">(.*?)</div>', res_page, re.S)
        address = text_of(m.group(1)) if m else ""
    pc = UK_POSTCODE.search(address.upper())
    if res["lat"] is None and pc:
        res["lat"], res["lng"] = geocode_postcode(pc.group(1))

    rows: list[Room] = []
    for g in groups:
        ri = g.get("residenceInfo", {})
        amenities = [a.get("name", "") for a in ri.get("amenities", []) if a.get("name")]
        city = ri.get("cityName") or res["city_slug"].title()
        rname = ri.get("residenceName") or res["name"]
        for rt in g.get("roomTypes", []):
            info, ten = scrape_room(rt, base)
            room = (ten or {}).get("room") or {}
            resd = (ten or {}).get("residence") or {}
            if rt.get("soldOut") or room.get("soldOut"):
                avail = "sold_out"
            elif rt.get("enquireNow") or room.get("enquireNow"):
                avail = "enquire"
            elif rt.get("comingSoon") or room.get("comingSoon"):
                avail = "coming_soon"
            else:
                avail = "available"
            common = dict(
                operator=OP, property_name=rname, city=city, room_type=rt.get("name", ""),
                room_category=normalize_category(rt.get("name", "")),
                size_sqm=info.get("size_sqm"), bed_size=info.get("bed_size", ""),
                address=address, postcode=pc.group(1) if pc else "",
                lat=res["lat"], lng=res["lng"], university_nearby="; ".join(res.get("universities", [])[:5]),
                amenities=amenities, property_url=url, room_url=rt.get("link", ""),
            )
            extra_base = {
                "source": res["source"],
                "residence_content_id": resd.get("contentId") or res.get("content_id"),
                "room_content_id": rt.get("contentId"),
                "room_type_id": room.get("idOriginal"),
                "listing_price": rt.get("priceInfo", ""),
                "min_price_per_week": room.get("minPriceForBillingCycle"),
                "available_count": room.get("count"),
                "bathroom": room.get("bathroomArrangement"), "kitchen": room.get("kitchenArrangement"),
                "beds_in_flat": room.get("maxNumOfBedsInFlat"),
                "promo": rt.get("promoMessage") or rt.get("imagePromoMessage") or "",
                "size_sqm_max": info.get("size_sqm_max"),
                "description": info.get("description", ""),
            }
            if avail == "available" and room.get("count") is not None and int(room.get("count") or 0) <= 3:
                avail = "limited" if int(room.get("count") or 0) > 0 else "sold_out"
            emitted = False
            for grp in (ten or {}).get("tenancy-options", []) or []:
                ay = year_label(grp.get("fromYear"), grp.get("toYear"))
                for opt in grp.get("tenancyOption", []) or []:
                    price = money(opt.get("rent")) or room.get("minPriceForBillingCycle") or money(rt.get("priceInfo"))
                    rows.append(Room(
                        **common, price_per_week_gbp=price, tenancy_weeks=weeks_from(opt), academic_year=ay,
                        availability=avail,
                        extra={**extra_base, "tenancy_name": opt.get("name"), "tenancy_type": opt.get("tenancyType"),
                               "start_date": opt.get("startDate"), "end_date": opt.get("endDate"),
                               "tenancy_label": opt.get("formattedLabel"), "rolling": opt.get("rolling")},
                    ))
                    emitted = True
            if not emitted:
                rows.append(Room(**common, price_per_week_gbp=money(rt.get("priceInfo")) or room.get("minPriceForBillingCycle"),
                                 availability=avail, extra=extra_base))
    print(f"  + {res['city_slug']:<11} {res['name']:<35} {len(rows)} rows")
    return rows


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max residences")
    a = ap.parse_args(argv)

    cities = crm_cities()
    print(f"[crm] {len(cities)} CRM cities: {', '.join(cities)}")
    residences, seen = [], set()
    with ThreadPoolExecutor(WORKERS) as ex:
        for lst in ex.map(city_residences, cities):
            for r in lst:
                key = r["url"].rstrip("/").split("/")[-1]
                if r["url"] and key not in seen:
                    seen.add(key)
                    residences.append(r)
    by_key = {r["url"].rstrip("/").split("/")[-1]: r for r in residences}
    for r in native_crm_residences():
        key = r["url"].rstrip("/").split("/")[-1]
        if key in by_key:
            by_key[key]["alt_groups"] = r["alt_groups"]
        elif r["url"] and key not in seen:
            seen.add(key)
            residences.append(r)
    print(f"[crm] {len(residences)} residences")
    if a.limit:
        residences = residences[: a.limit]

    rows: list[Room] = []
    with ThreadPoolExecutor(WORKERS) as ex:
        for rs in ex.map(scrape_residence, residences):
            rows.extend(rs)
    save(OP, rows)
    studios = [r for r in rows if r.room_category == "studio"]
    print(f"[crm] rows={len(rows)} properties={len({r.property_name for r in rows})} studio_rows={len(studios)}")


if __name__ == "__main__":
    main()
