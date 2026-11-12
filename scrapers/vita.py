"""Vita Student (vitastudent.com) scraper.

Approach (public pages only):
  1. Public WP REST `wp-json/wp/v2/developments?lang=en` -> every property with ACF fields
     (Salesforce development_id, address, latitude/longitude, room_count, academic years).
  2. Each property page is rendered in headless Chromium (Playwright). We read the JSON-LD
     (LodgingBusiness: address/postcode/amenities) and the `room-types-app` mounts
     (room type + size), and passively record the JSON the page itself loads
     (`student-accommodation/v1/rooms`: child room types, "from" price, availability).
     Rendered once per academic year via the site's own `?academicYear=` URL param.
  3. For each child room type, the public "view room" page is rendered and its own
     `room` + `booking` responses are recorded: size, floor, status, and every contract
     length (e.g. 51 / 44 weeks) with the weekly price (Annual instalment, 1 student).

Usage: python -m scrapers.vita [--limit N] [--years "2026 / 27,2027 / 28"] [--include-non-uk]
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from .common import Room, normalize_category, save, session

OP = "vita"
BASE = "https://www.vitastudent.com"
DEFAULT_YEARS = ["2026 / 27", "2027 / 28"]
DELAY = 0.5
BLOCK_TYPES = {"image", "media", "font"}
NON_UK_CITIES = {"barcelona", "madrid"}


# ---------------------------------------------------------------- helpers
def money(s) -> float | None:
    if s is None:
        return None
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)", str(s))
    return float(m.group(1).replace(",", "")) if m else None


def sqm(s) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*sq", str(s or ""), re.I)
    return float(m.group(1)) if m else None


def year_label(y: str) -> str:  # "2026 / 27" -> "2026/27"
    return re.sub(r"\s+", "", y or "")


def vita_category(label: str) -> str:
    """Vita's room names (Classic, Premium, Deluxe, Lite, Ultimate...) are almost all studios."""
    l = (label or "").lower()
    if "cluster" in l:
        return "ensuite"
    if "bedroom" in l or "apartment" in l:
        return "apartment"
    if "twin" in l:
        return "twodio"
    cat = normalize_category(label)
    return "studio" if cat == "other" else cat


def clean(s: str) -> str:
    s = htmllib.unescape(htmllib.unescape(s or ""))
    s = s.replace("–", "-").replace("—", "-").replace("�", "'")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- step 1: property list
def list_developments(s) -> list[dict]:
    r = s.get(f"{BASE}/wp-json/wp/v2/developments", params={"per_page": 100, "lang": "en"})
    r.raise_for_status()
    devs = json.loads(r.content.decode("utf-8", "replace"))
    out = []
    for d in devs:
        acf = d.get("acf") or {}
        link = d.get("link") or ""
        m = re.search(r"/cities/([^/]+)/([^/]+)/", link)
        if not m or not acf.get("development_id"):
            continue
        out.append({
            "wp_id": d.get("id"),
            "name": clean((d.get("title") or {}).get("rendered", "")),
            "url": link,
            "city_slug": m.group(1),
            "dev_id": acf.get("development_id"),
            "address_acf": clean(acf.get("address") or ""),
            "lat": acf.get("latitude"),
            "lng": acf.get("longitude"),
            "room_count": acf.get("room_count"),
            "floor_count": acf.get("floor_count"),
            "coming_soon": acf.get("coming_soon"),
            "register_interest": acf.get("register_your_interest"),
            "walk_uni_min": acf.get("walking_distance_university"),
            "walk_centre_min": acf.get("walking_distance_city_centre"),
            "matchmaker_stats": acf.get("matchmaker_stats") or [],
            "academic_year_flags": acf.get("academic_year"),
        })
    out.sort(key=lambda x: (x["city_slug"], x["name"]))
    return out


# ---------------------------------------------------------------- page parsing
def parse_property_html(h: str) -> dict:
    info = {"amenities": [], "address": "", "postcode": "", "city": "", "country": "", "ld_name": "",
            "room_types": []}
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', h, re.S):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        for node in (d.get("@graph") or [d]) if isinstance(d, dict) else d:
            if isinstance(node, dict) and node.get("@type") == "LodgingBusiness":
                a = node.get("address") or {}
                info.update(address=clean(a.get("streetAddress", "")), postcode=a.get("postalCode", ""),
                            city=a.get("addressLocality", ""), country=a.get("addressCountry", ""),
                            ld_name=clean(node.get("name", "")))
                info["amenities"] = [clean(f.get("name", "")) for f in node.get("amenityFeature") or []
                                     if f.get("name")]
                info["number_of_rooms"] = node.get("numberOfRooms")
    for m in re.finditer(r'class="room-types-app"(.*?)>', h, re.S):
        a = {k: htmllib.unescape(v) for k, v in re.findall(r'data-([a-z_-]+)\s*=\s*"([^"]*)"', m.group(1))}
        try:
            sizes = json.loads(a.get("sizes") or "{}")
        except Exception:
            sizes = {}
        try:
            feats = [clean(x) for x in json.loads(a.get("standardfeatures") or "[]")]
        except Exception:
            feats = []
        info["room_types"].append({"type": a.get("type", ""), "title": a.get("title", ""), "size": a.get("size", ""),
                                   "sizes": sizes, "std_features": feats, "page_year": a.get("year", "")})
    return info


# ---------------------------------------------------------------- browser
class Browser:
    def __init__(self, pw):
        self.b = pw.chromium.launch(headless=True)
        self.ctx = self.b.new_context(locale="en-GB")
        self.ctx.route("**/*", self._route)
        self.page = self.ctx.new_page()
        self.captured: list[dict] = []
        self.page.on("response", self._on_resp)

    @staticmethod
    def _route(route):
        req = route.request
        if req.resource_type in BLOCK_TYPES or "vitastudent.com" not in req.url:
            return route.abort()
        return route.continue_()

    def _on_resp(self, r):
        if "/student-accommodation/v1/" not in r.url:
            return
        try:
            body = r.json()
        except Exception:
            body = None
        self.captured.append({"url": r.url, "status": r.status, "body": body})

    def load(self, url: str, settle_ms: int = 1500, timeout_ms: int = 25000,
             need: set | None = None) -> tuple[str, list[dict]]:
        self.captured = []
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"  ! goto failed {url}: {e}")
            return "", []
        # wait until the page's own API calls stop arriving
        t0 = time.time()
        last_n, last_change = -1, time.time()
        while time.time() - t0 < timeout_ms / 1000:
            n = len(self.captured)
            if need and need <= {endpoint(c) for c in self.captured}:
                break
            if n != last_n:
                last_n, last_change = n, time.time()
            elif n > 0 and (time.time() - last_change) * 1000 > settle_ms:
                break
            self.page.wait_for_timeout(250)
        try:
            content = self.page.content()
        except Exception:
            content = ""
        time.sleep(DELAY)
        return content, list(self.captured)

    def close(self):
        self.b.close()


def endpoint(c: dict) -> str:
    m = re.search(r"/student-accommodation/v1/([a-z]+)", c["url"])
    return m.group(1) if m else ""


def qparam(url: str, key: str) -> str:
    from urllib.parse import parse_qs, urlparse
    return (parse_qs(urlparse(url).query).get(key) or [""])[0]


# ---------------------------------------------------------------- main scrape
def scrape(limit: int | None, years: list[str], include_non_uk: bool) -> list[Room]:
    s = session()
    devs = list_developments(s)
    print(f"[vita] {len(devs)} developments from WP REST")
    if not include_non_uk:
        devs = [d for d in devs if d["city_slug"] not in NON_UK_CITIES]
        print(f"[vita] {len(devs)} UK developments")
    rows: list[Room] = []
    with sync_playwright() as pw:
        br = Browser(pw)
        done = 0
        for d in devs:
            if limit and done >= limit:
                break
            first_info = None
            prop_rows = 0
            for yr in years:
                url = f"{d['url']}?academicYear={quote(yr)}"
                h, caps = br.load(url)
                info = parse_property_html(h) if h else {"room_types": [], "amenities": []}
                if first_info is None:
                    first_info = info
                    non_uk = d["city_slug"] in NON_UK_CITIES or (info.get("country") or "GB") not in ("GB", "UK")
                    if non_uk and not include_non_uk:
                        print(f"[vita] skip non-UK {d['name']} ({info['country']})")
                        break
                city = d["city_slug"].replace("-", " ").title()
                size_by_type = {rt["type"].lower(): rt for rt in info["room_types"]}

                # collect child room types from the page's own /rooms responses
                children: dict[str, dict] = {}
                avail_types = set()
                for c in caps:
                    if endpoint(c) != "rooms" or not isinstance(c["body"], list):
                        continue
                    if qparam(c["url"], "year").strip() != yr.strip():
                        continue
                    parent = qparam(c["url"], "type")
                    is_avail_q = qparam(c["url"], "availability") == "true"
                    for it in c["body"]:
                        name = it.get("name") or parent
                        key = name.lower()
                        if is_avail_q:
                            avail_types.add(key)
                        prev = children.get(key)
                        if prev and prev.get("_avail_q") and not is_avail_q:
                            continue
                        children[key] = {**it, "parent": parent, "_avail_q": is_avail_q}
                print(f"[vita] {d['name']} {yr}: {len(info['room_types'])} types on page, "
                      f"{len(children)} child types from page data")

                for key, ch in children.items():
                    if key in avail_types:
                        availability = "limited" if (ch.get("last_one") or ch.get("low_availability")) else "available"
                    else:
                        availability = "sold_out"
                    room_url = f"{d['url']}{ch['link']}" if ch.get("link") else ""
                    room_data, booking = {}, {}
                    if room_url:
                        _, rcaps = br.load(room_url, settle_ms=1200, timeout_ms=10000, need={"room", "booking"})
                        for c in rcaps:
                            ep = endpoint(c)
                            if ep == "room" and c["body"]:
                                b = c["body"][0] if isinstance(c["body"], list) else c["body"]
                                if isinstance(b, dict):
                                    room_data = b
                            elif ep == "booking" and isinstance(c["body"], dict):
                                booking = c["body"]
                    rt_meta = size_by_type.get((ch.get("parent") or "").lower(), {})
                    size_key = key.replace(" ", "_")
                    size = sqm(room_data.get("size")) or sqm((rt_meta.get("sizes") or {}).get(size_key)) \
                        or sqm(rt_meta.get("size"))
                    common = dict(
                        operator=OP, property_name=d["name"], city=city, room_type=ch.get("name") or key,
                        room_category=vita_category(ch.get("name") or key), academic_year=year_label(yr),
                        size_sqm=size, availability=availability,
                        address=d["address_acf"] or info.get("address", ""), postcode=info.get("postcode", ""),
                        lat=d["lat"], lng=d["lng"],
                        university_nearby="; ".join(f"{x.get('top_line','')} {x.get('bottom_line','')}".strip()
                                                    for x in d["matchmaker_stats"]),
                        amenities=(first_info or info).get("amenities", []),
                        property_url=d["url"], room_url=room_url,
                    )
                    extra_base = {
                        "parent_type": ch.get("parent"), "from_range": clean(ch.get("range") or ""),
                        "room_code": room_data.get("code"), "floor": room_data.get("floor"),
                        "room_status": room_data.get("status"), "beds": room_data.get("beds"),
                        "standard_features": room_data.get("standard_features") or rt_meta.get("std_features"),
                        "dev_id": d["dev_id"], "room_count": d["room_count"],
                        "walk_uni_min": d["walk_uni_min"], "walk_centre_min": d["walk_centre_min"],
                        "currency": booking.get("currency") or room_data.get("currency"),
                    }
                    terms = booking.get("terms") or []
                    if not terms:
                        rows.append(Room(**common, price_per_week_gbp=money(ch.get("range")) or money(room_data.get("price")),
                                         extra=extra_base))
                        prop_rows += 1
                        continue
                    for t in terms:
                        inst = (t.get("instalments") or [])
                        annual = next((i for i in inst if (i.get("name") or "").lower() == "annual"), inst[0] if inst else {})
                        amts = sorted(annual.get("amounts") or [], key=lambda a: a.get("students") or 99)
                        amt1 = next((a for a in amts if a.get("students") == 1), amts[0] if amts else None)
                        price = money(amt1.get("weeklyPrice")) if amt1 else None
                        wk = re.search(r"(\d+)\s*week", t.get("name") or "", re.I)
                        rows.append(Room(
                            **common, price_per_week_gbp=price, tenancy_weeks=int(wk.group(1)) if wk else None,
                            extra={**extra_base, "contract": t.get("name"), "start_date": t.get("startDate"),
                                   "end_date": t.get("endDate"), "term_premium_pct": t.get("premium"),
                                   "instalment_options": {i.get("name"): money(next((a.get("weeklyPrice") for a in i.get("amounts") or [] if a.get("students") == 1), None)) for i in inst},
                                   "two_students_weekly": money(next((a.get("weeklyPrice") for a in annual.get("amounts") or [] if a.get("students") == 2), None)),
                                   "price_students": amt1.get("students") if amt1 else None,
                                   "promotion_pct": booking.get("promotionPercentage")}))
                        prop_rows += 1
            done += 1
            print(f"[vita] {d['name']}: {prop_rows} rows (total {len(rows)})")
        br.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--years", default=",".join(DEFAULT_YEARS))
    ap.add_argument("--include-non-uk", action="store_true", help="also scrape Spain (Barcelona/Madrid)")
    a = ap.parse_args()
    rows = scrape(a.limit, [y.strip() for y in a.years.split(",") if y.strip()], a.include_non_uk)
    save(OP, rows)


if __name__ == "__main__":
    main()
