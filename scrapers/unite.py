"""Unite Students (unitestudents.com) scraper.

Approach: sitemap.xml -> property pages (/student-accommodation/<city>/<slug>).
Each property page embeds a schema.org JSON-LD @graph with:
  - AggregateOffer.itemOffered (LocalBusiness): name, address, postcode, description, amenityFeature
  - ItemList "#roomList": one Offer per room class x tenancy length x academic year, with
    price/week, availability, room label ("STUDIO, Standard Floor, 23.8m2, Double"),
    and a ReserveAction URL carrying buildingCode / roomType / roomClass / academicYear / dates.
Lat/lng come from the property map markers embedded in the Next.js RSC payload.
No /api/* calls (disallowed in robots.txt).

Usage: python -m scrapers.unite [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

from .common import Room, normalize_category, save, session

OPERATOR = "unite"
BASE = "https://www.unitestudents.com"
DELAY = 0.6

LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
MARKER_RE = re.compile(
    r'\\?"name\\?":\\?"((?:[^"\\]|\\\\.)*?)\\?",\\?"latitude\\?":(-?[\d.]+),\\?"longitude\\?":(-?[\d.]+),'
    r'\\?"propertyId\\?":\\?"([^"\\]+)\\?",\\?"soldOut\\?":(true|false)'
)
WEEKS_RE = re.compile(r"(\d+)\s*weeks?", re.I)
YEAR_RE = re.compile(r"\bin (\d{2})/(\d{2})\b")
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b")
SQM_RE =re.compile(r"([\d.]+)\s*m(?:2|²)", re.I)

AVAIL_MAP = {
    "InStock": "available",
    "LimitedAvailability": "limited",
    "SoldOut": "sold_out",
    "OutOfStock": "sold_out",
    "PreOrder": "waitlist",
}


def property_urls(s) -> list[str]:
    r = s.get(f"{BASE}/sitemap.xml")
    r.raise_for_status()
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
    out = []
    for u in locs:
        p = urlparse(u).path.strip("/").split("/")
        if len(p) == 3 and p[0] == "student-accommodation":
            out.append(f"{BASE}/{'/'.join(p)}")
    return sorted(set(out))


def _graph(html: str) -> list[dict]:
    nodes = []
    for raw in LD_RE.findall(html):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for x in (d if isinstance(d, list) else [d]):
            nodes.extend(x.get("@graph", [x]))
    return nodes


def _type(n: dict) -> list[str]:
    t = n.get("@type", [])
    return t if isinstance(t, list) else [t]


def _amenities(feats) -> list[str]:
    out = []
    for f in feats or []:
        name = (f or {}).get("name")
        if name and str(f.get("value", True)).lower() != "false" and name not in out:
            out.append(name)
    return out


def _markers(html: str) -> dict:
    res = {}
    for name, lat, lng, pid, sold in MARKER_RE.findall(html):
        res.setdefault(pid, (name.replace("\\u0026", "&"), float(lat), float(lng), sold == "true"))
    return res


def _year(label: str, params: dict) -> str:
    m = YEAR_RE.search(label)
    if m:
        return f"20{m.group(1)}/{m.group(2)}"
    ay = (params.get("academicYear") or [""])[0]
    if ay.isdigit():
        end = int(ay)
        return f"20{end - 1:02d}/{end:02d}"
    return ""


def parse_property(url: str, html: str) -> list[Room]:
    nodes = _graph(html)
    city_slug = urlparse(url).path.strip("/").split("/")[1]
    biz, agg, roomlist, crumbs = {}, {}, None, []
    for n in nodes:
        t = _type(n)
        if "AggregateOffer" in t:
            agg = n
            biz = n.get("itemOffered") or {}
        elif "ItemList" in t and str(n.get("@id", "")).endswith("#roomList"):
            roomlist = n
        elif "WebPage" in t:
            crumbs = ((n.get("breadcrumb") or {}).get("itemListElement")) or []
    fallback_geo = (None, None)
    if not biz:
        # Sold-out / nominations-only pages: WebPage.mainEntity is a Product whose offers carry
        # address, lat/lng and property features (no room offers).
        for n in nodes:
            me = n.get("mainEntity") if "WebPage" in _type(n) else None
            if isinstance(me, dict) and me.get("name"):
                place = next(((o.get("areaServed") or {}) for o in me.get("offers") or []
                              if isinstance(o, dict) and o.get("areaServed")), {})
                a = dict(place.get("address") or {})
                street = a.get("streetAddress") or ""
                pc = POSTCODE_RE.search(street)
                if pc:
                    a["postalCode"] = pc.group(1)
                af = place.get("amenityFeature") or {}
                vals = af.get("value") if isinstance(af, dict) else None
                biz = {
                    "name": me["name"],
                    "address": a,
                    "description": me.get("description"),
                    "amenityFeature": [{"name": v, "value": True} for v in vals] if isinstance(vals, list) else af,
                }
                fallback_geo = (place.get("latitude"), place.get("longitude"))
                break
    if not biz:
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        print(f"  ! no property JSON-LD on {url} ({title.group(1).strip() if title else ''})")
        return []

    addr = biz.get("address") or {}
    name = biz.get("name") or ""
    city = addr.get("addressLocality") or (crumbs[1]["name"] if len(crumbs) > 1 else city_slug.title())
    prop_amen = _amenities(biz.get("amenityFeature"))
    markers = _markers(html)

    offers = [(e.get("item") or {}) for e in (roomlist or {}).get("itemListElement", [])]
    building_code = ""
    for o in offers:
        for a in o.get("potentialAction") or []:
            q = parse_qs(urlparse((a.get("target") or {}).get("url", "")).query)
            if q.get("buildingCode"):
                building_code = q["buildingCode"][0]
                break
        if building_code:
            break
    lat = lng = None
    prop_sold_out = None
    mk = markers.get(building_code) or next((v for v in markers.values() if v[0] == name), None)
    if mk:
        _, lat, lng, prop_sold_out = mk
    if lat is None and fallback_geo[0] is not None:
        lat, lng = float(fallback_geo[0]), float(fallback_geo[1])

    common = dict(
        operator=OPERATOR,
        property_name=name,
        city=city,
        address=(addr.get("streetAddress") or "").replace("\n", ", "),
        postcode=addr.get("postalCode") or "",
        lat=lat,
        lng=lng,
        amenities=prop_amen,
        property_url=url,
    )
    base_extra = {
        "building_code": building_code,
        "property_low_price": agg.get("lowPrice"),
        "property_high_price": agg.get("highPrice"),
        "property_sold_out": prop_sold_out,
    }

    rows: list[Room] = []
    for o in offers:
        label = o.get("name") or ""
        item = o.get("itemOffered") or {}
        room_label = item.get("name") or ""
        params = {}
        book_url = ""
        for a in o.get("potentialAction") or []:
            book_url = (a.get("target") or {}).get("url", "")
            if book_url:
                params = parse_qs(urlparse(book_url).query)
                break
        room_type_code = (params.get("roomType") or [""])[0]
        if not room_type_code:
            m = re.search(r"in an? (.+?) room at", label)
            room_type_code = m.group(1) if m else ""
        room_class = (params.get("roomClass") or [""])[0]
        parts = [p.strip() for p in room_label.split(",")]
        sqm = SQM_RE.search(room_label)
        bed = ""
        for p in parts[1:]:
            if any(b in p.lower() for b in ("single", "double", "king", "queen", "small double", "bunk")):
                bed = p
        price = ((o.get("priceSpecification") or {}).get("price"))
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        wk = WEEKS_RE.search(label)
        avail_raw = str(o.get("availability") or "").rsplit("/", 1)[-1]
        cat = normalize_category(room_type_code) if room_type_code else normalize_category(room_label)
        rows.append(Room(
            room_type=room_label or room_type_code.title(),
            room_category=cat,
            price_per_week_gbp=price,
            tenancy_weeks=int(wk.group(1)) if wk else None,
            academic_year=_year(label, params),
            size_sqm=float(sqm.group(1)) if sqm else None,
            bed_size=bed,
            availability=AVAIL_MAP.get(avail_raw, avail_raw.lower()),
            room_url=urljoin(BASE, book_url) if book_url else "",
            extra={
                **base_extra,
                "room_type_code": room_type_code,
                "room_class": room_class,
                "floor": next((p for p in parts[1:] if "floor" in p.lower()), ""),
                "offer_name": label,
                "check_in": (params.get("checkinDate") or [""])[0] or (o.get("validFrom") or "")[:10],
                "check_out": (params.get("checkoutDate") or [""])[0] or (o.get("validThrough") or "")[:10],
                "tenancy_type": (params.get("tenancyType") or [""])[0],
                "room_amenities": _amenities(item.get("amenityFeature")),
                "room_description": item.get("description") or "",
            },
            **common,
        ))

    if not rows:
        # Property listed but no bookable offers embedded (fully sold out / not yet on sale).
        rows.append(Room(
            room_type="",
            room_category="other",
            availability="sold_out" if prop_sold_out else "no_offers_listed",
            extra={**base_extra, "note": "no room offers in page JSON-LD"},
            **common,
        ))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    s = session()
    urls = property_urls(s)
    print(f"[{OPERATOR}] {len(urls)} property URLs in sitemap")
    if args.limit:
        urls = urls[: args.limit]

    rows: list[Room] = []
    for i, url in enumerate(urls, 1):
        try:
            r = None
            for attempt in range(3):  # retry transient 5xx / truncated responses
                try:
                    r = s.get(url)
                    if r.status_code < 500:
                        break
                except Exception:
                    if attempt == 2:
                        raise
                time.sleep(3 * (attempt + 1))
            if r.status_code != 200:
                print(f"  [{i}/{len(urls)}] {url} -> HTTP {r.status_code}, skipped")
            else:
                got = parse_property(url, r.text)
                rows.extend(got)
                print(f"  [{i}/{len(urls)}] {url.rsplit('/', 2)[-2:]} -> {len(got)} rows")
        except Exception as e:  # keep going on single-page failures
            print(f"  [{i}/{len(urls)}] {url} ERROR {e!r}")
        time.sleep(DELAY)

    save(OPERATOR, rows)
    studios = sum(1 for r in rows if r.room_category == "studio")
    print(f"[{OPERATOR}] {len(rows)} rows, {len({r.property_url for r in rows})} properties, {studios} studio rows")


if __name__ == "__main__":
    main()
