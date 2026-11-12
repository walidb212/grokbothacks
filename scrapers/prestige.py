"""Prestige Student Living (prestigestudentliving.com) scraper.

The site is a Nuxt front-end over a headless WordPress at api.prestigestudentliving.com.
Public JSON endpoints used (the same ones the site's pages fetch):
  - /wp-json/basepms/v1/rooms?page=N        every room type + contracts (price, weeks, year, size, availability)
  - /wp-json/hfs/v2/properties/<slug>       property address, coordinates, universities, room slugs
Plus the public property HTML page for the amenities list and JSON-LD postcode.

Run: python -m scrapers.prestige [--limit N]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import time

from .common import Room, normalize_category, save, session

OPERATOR = "prestige"
SITE = "https://prestigestudentliving.com"
API = "https://api.prestigestudentliving.com/wp-json"
DELAY = 0.5
UK_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)


def get_json(s, url, **params):
    for attempt in range(3):
        try:
            r = s.get(url, params=params or None)
            time.sleep(DELAY)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 401, 404):
                return None
        except Exception as e:  # network hiccup
            print(f"  ! {url}: {e}")
        time.sleep(2 * (attempt + 1))
    return None


def fetch_all_room_types(s) -> list[dict]:
    rooms, page = [], 1
    while True:
        j = get_json(s, f"{API}/basepms/v1/rooms", page=page, per_page=100)
        if not j or not j.get("data"):
            break
        rooms += j["data"]
        print(f"  basepms rooms page {page}/{j.get('pages')}: {len(rooms)} so far")
        if page >= (j.get("pages") or 1):
            break
        page += 1
    return rooms


def parse_property_html(s, url: str) -> dict:
    out = {"amenities": [], "postcode": "", "street": "", "price_range": ""}
    try:
        r = s.get(url)
        time.sleep(DELAY)
        if r.status_code != 200:
            return out
        t = r.content.decode("utf-8", "replace")
    except Exception as e:
        print(f"  ! html {url}: {e}")
        return out
    names = re.findall(r'class="Amenities__name[^"]*"[^>]*>([^<]*)<', t)
    seen = []
    for n in names:
        n = html.unescape(n).strip().replace("�", "").replace("†", "").strip()
        if n and n not in seen:
            seen.append(n)
    out["amenities"] = seen
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', t, re.S):
        try:
            d = json.loads(block)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("address"):
            a = d["address"]
            out["postcode"] = a.get("postalCode", "") or ""
            out["street"] = a.get("streetAddress", "") or ""
            out["price_range"] = d.get("priceRange", "") or ""
            if not out["postcode"]:
                m = UK_POSTCODE.search(a.get("addressLocality", "") or "")
                out["postcode"] = m.group(1).upper() if m else ""
    return out


def size_of(sz) -> tuple[float | None, str]:
    if not sz:
        return None, ""
    if sz.get("type") == "exact" and sz.get("exact"):
        return float(sz["exact"]), f"{sz['exact']}"
    if sz.get("type") == "range" and sz.get("min"):
        lo, hi = sz.get("min"), sz.get("max") or sz.get("min")
        return float(lo), f"{lo}-{hi}"
    return None, ""


SIZE_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)(?:\s*(?:-|–|to|and)\s*(\d{1,3}(?:\.\d+)?))?\s*(?:square\s*met|sq\.?\s*m|sqm|m²|m2)", re.I)


def size_from_text(s, wp_slug: str) -> tuple[float | None, str]:
    """Fallback: room description text on the public room endpoint often states the size."""
    if not wp_slug:
        return None, ""
    j = get_json(s, f"{API}/hfs/v2/rooms/{wp_slug}")
    if not j:
        return None, ""
    txt = re.sub(r"<[^>]+>", " ", html.unescape(j.get("content") or ""))
    for m in SIZE_RE.finditer(txt):
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        if 5 <= lo <= 150:
            return lo, (f"{m.group(1)}-{m.group(2)}" if m.group(2) else m.group(1)) + " (text)"
    return None, ""


def norm_year(y: str) -> str:
    m = re.match(r"\s*(\d{4})\s*/\s*(\d{4})", y or "")
    return f"{m.group(1)}/{m.group(2)[2:]}" if m else (y or "")


def availability_of(c: dict, room: dict) -> str:
    if c.get("enquiry_only"):
        return "enquiry_only" if c.get("available") else "sold_out"
    if c.get("available"):
        pct = c.get("stock_remaining_percent")
        try:
            if pct not in (None, "") and float(pct) <= 20:
                return "limited"
        except ValueError:
            pass
        return "available"
    return "sold_out"


def main(limit: int | None = None):
    s = session()
    print("[prestige] fetching room types from basepms API")
    room_types = fetch_all_room_types(s)

    # group by property (location)
    by_prop: dict[str, list[dict]] = {}
    for rt in room_types:
        loc = rt.get("location") or {}
        if loc.get("slug"):
            by_prop.setdefault(loc["slug"], []).append(rt)
    prop_slugs = sorted(by_prop)
    if limit:
        prop_slugs = prop_slugs[:limit]
    print(f"[prestige] {len(room_types)} room types across {len(by_prop)} properties; scraping {len(prop_slugs)}")

    rows: list[Room] = []
    for i, ps in enumerate(prop_slugs, 1):
        rts = by_prop[ps]
        city_obj = rts[0].get("city") or {}
        city_name = city_obj.get("name", "")
        city_slug = city_obj.get("slug", "")
        prop = get_json(s, f"{API}/hfs/v2/properties/{ps}") or {}
        if prop.get("city"):
            city_name = prop["city"].get("title") or city_name
            city_slug = prop["city"].get("slug") or city_slug
        display_slug = prop.get("displaySlug") or ps
        prop_url = prop.get("externalUrl") or f"{SITE}/student-accommodation/{city_slug}/{display_slug}"
        page = parse_property_html(s, prop_url) if prop and not prop.get("externalUrl") else {"amenities": [], "postcode": "", "street": "", "price_range": ""}
        address = prop.get("address") or page["street"]
        postcode = page["postcode"]
        if not postcode:
            m = UK_POSTCODE.search(address or "")
            postcode = m.group(1).upper() if m else ""
        coords = prop.get("coordinates") or {}
        unis = [u.get("title", "").strip() for u in prop.get("universities") or [] if u.get("title")]
        ratings = {r.get("platform"): r.get("value") for r in prop.get("ratings") or []}
        offers = [f"{o.get('title','')}: {o.get('content','')}".replace("£", "GBP ").replace("�", "GBP ") for o in prop.get("offers") or []]
        wp_room_slugs = {(r.get("title") or "").strip().lower(): r.get("slug") for r in prop.get("rooms") or []}
        name = prop.get("title") or (rts[0].get("location") or {}).get("name", ps)
        name = html.unescape(name)
        print(f"  [{i}/{len(prop_slugs)}] {name} ({city_name}): {len(rts)} room types, {len(page['amenities'])} amenities")

        for rt in rts:
            label = html.unescape(rt.get("name") or "").strip()
            for suffix in (f", {name}", f" - {name}", f" {name}"):
                if label.lower().endswith(suffix.lower()) and len(label) > len(suffix):
                    label = label[: -len(suffix)].strip(" ,-")
                    break
            raw_label = html.unescape(rt.get("name") or "").strip()
            wp_slug = wp_room_slugs.get(raw_label.lower()) or wp_room_slugs.get(label.lower()) or rt.get("slug") or ""
            short = wp_slug[: -len(ps) - 1] if wp_slug.endswith("-" + ps) else wp_slug
            room_url = f"{prop_url}/{short}" if short and not prop.get("externalUrl") else ""
            sqm, size_raw = size_of(rt.get("size"))
            if sqm is None and prop and not prop.get("externalUrl"):
                sqm, size_raw = size_from_text(s, wp_slug)
            room_amen = [a.get("name") for a in rt.get("amenities") or [] if a.get("name")]
            bed = next((a for a in room_amen if "bed" in a.lower() and ("double" in a.lower() or "single" in a.lower() or "king" in a.lower() or "queen" in a.lower())), "")
            contracts = rt.get("contracts") or [{}]
            for c in contracts:
                price = c.get("price")
                try:
                    ppw = round(float(price) / 100, 2) if price not in (None, "") and float(price) > 0 else None
                except ValueError:
                    ppw = None
                weeks = c.get("contract_length")
                try:
                    weeks = int(weeks) if weeks not in (None, "") else None
                except ValueError:
                    weeks = None
                rows.append(Room(
                    operator=OPERATOR,
                    property_name=name,
                    city=city_name,
                    room_type=label,
                    room_category=normalize_category(label),
                    price_per_week_gbp=ppw,
                    tenancy_weeks=weeks,
                    academic_year=norm_year(c.get("academic_year", "")),
                    size_sqm=sqm,
                    bed_size=bed,
                    availability=availability_of(c, rt) if c else ("available" if rt.get("available") else "sold_out"),
                    address=address,
                    postcode=postcode,
                    lat=float(coords["lat"]) if coords.get("lat") not in (None, "") else None,
                    lng=float(coords["lng"]) if coords.get("lng") not in (None, "") else None,
                    university_nearby="; ".join(unis),
                    amenities=page["amenities"],
                    property_url=prop_url,
                    room_url=room_url,
                    extra={
                        "basepms_room_type_id": rt.get("basepms_room_type_id"),
                        "basepms_property_id": rt.get("basepms_property_id"),
                        "contract_name": c.get("name"),
                        "start_date": c.get("start_date"),
                        "end_date": c.get("end_date"),
                        "weeks_remaining": c.get("weeks_remaining"),
                        "price_type": c.get("price_type"),
                        "original_price_gbp": round(float(c["original_price"]) / 100, 2) if c.get("original_price") not in (None, "") else None,
                        "stock_remaining_percent": c.get("stock_remaining_percent"),
                        "enquiry_only": c.get("enquiry_only"),
                        "booking_url": c.get("base_hub_url"),
                        "size_raw": size_raw,
                        "room_amenities": room_amen,
                        "room_type_available": rt.get("available"),
                        "ratings": ratings,
                        "offers": offers,
                        "price_range": page["price_range"].replace("£", "GBP ").replace("�", "GBP "),
                    },
                ))
    save(OPERATOR, rows)
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="max number of properties")
    main(ap.parse_args().limit)
