"""Fresh Student Living (thisisfresh.com) scraper.

Site runs on Yardi RentCafe. Approach (public pages only):
  1. Property list = sitemap.xml URLs ending in /rooms  U  JSON-LD ApartmentComplex graph
     embedded in /student/searchlisting (gives address, postcode, lat/lng).
  2. For each property and each academic year (ids read from the rooms page radio
     "atGroupId"), GET {property}/rooms?academicYear=<id> -> room-type cards.
  3. For each card, GET its gradedetail page -> one row per tenancy option
     (weeks, dates, price/week, bookable or not), plus overview text and
     property features (amenities) and property address.

Run: python -m scrapers.fresh [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scrapers.common import Room, normalize_category, save, session

OPERATOR = "fresh"
BASE = "https://www.thisisfresh.com"
DELAY = 0.5

S = session()


def get(url: str) -> str | None:
    for attempt in range(3):
        try:
            r = S.get(url)
            time.sleep(DELAY)
            if r.status_code == 200:
                return r.text
            if r.status_code in (401, 403, 404, 410):
                print(f"  ! {r.status_code} {url}", file=sys.stderr)
                return None
        except Exception as e:  # network hiccup
            print(f"  ! {e} {url}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return None


def clean(s: str | None) -> str:
    return " ".join((s or "").replace("\xa0", " ").split())


def money(s: str) -> tuple[float | None, str]:
    m = re.search(r"([£€$])\s*([\d,]+(?:\.\d+)?)", s or "")
    if not m:
        return None, ""
    return float(m.group(2).replace(",", "")), {"£": "GBP", "€": "EUR", "$": "USD"}[m.group(1)]


SQM_RE = re.compile(r"(\d+(?:\.\d+)?)(?:\s*(?:-|to|–)\s*(\d+(?:\.\d+)?))?\s*(?:sq\.?\s?m\b|sqm|m²|m2\b|square met)", re.I)
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}|[A-Z]\d{2}\s?[A-Z\d]{4})\b")
SQFT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:sq\.?\s?ft|sqft|square f)", re.I)


def find_sqm(text: str) -> tuple[float | None, float | None]:
    """Returns (min_sqm, max_sqm); ranges like '14.2 - 16.8 sqm' give both."""
    m = SQM_RE.search(text or "")
    if m:
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        return (lo, hi) if 5 <= lo <= 200 else (None, None)
    m = SQFT_RE.search(text or "")
    if m:
        v = round(float(m.group(1)) * 0.092903, 1)
        return (v, v) if 5 <= v <= 200 else (None, None)
    return None, None


def bed_size(features: list[str]) -> str:
    for f in features:
        if re.search(r"\bbed\b", f, re.I) and not re.search(r"under|bedding|linen|storage|\d+\s*bed", f, re.I):
            return f
    return ""


# ---------------------------------------------------------------- property list

def property_base(url: str) -> str | None:
    """Normalize any property URL to https://www.thisisfresh.com/<city>/<slug>"""
    m = re.match(r"https?://www\.thisisfresh\.com/([^/?#]+)/([^/?#]+)", url)
    if not m or m.group(1) in ("blog", "student", "fresh-residents-student-app"):
        return None
    return f"{BASE}/{m.group(1)}/{m.group(2)}"


def list_properties() -> dict[str, dict]:
    props: dict[str, dict] = {}
    # JSON-LD on the search listing page (address + geo)
    html = get(f"{BASE}/student/searchlisting") or ""
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data, _ = json.JSONDecoder().raw_decode(m.group(1).strip())
        except Exception:
            continue
        for node in data.get("@graph", [data]) if isinstance(data, dict) else []:
            if node.get("@type") != "ApartmentComplex":
                continue
            base = property_base(node.get("url", ""))
            if not base:
                continue
            a = node.get("address") or {}
            g = node.get("geo") or {}
            props[base] = {
                "name": clean(a.get("name")),
                "city": clean(a.get("addressLocality")),
                "street": clean(a.get("streetAddress")),
                "postcode": clean(a.get("postalCode")),
                "lat": float(g["latitude"]) if g.get("latitude") else None,
                "lng": float(g["longitude"]) if g.get("longitude") else None,
                "phone": clean(a.get("telephone")),
            }
    # sitemap: every property exposing a /rooms page
    sm = get(f"{BASE}/sitemap.xml") or ""
    for loc in re.findall(r"<loc>([^<]+)</loc>", sm):
        if loc.rstrip("/").endswith("/rooms"):
            base = property_base(loc)
            if base and base not in props:
                props[base] = {}
    return props


# ---------------------------------------------------------------- parsing

def parse_years(soup: BeautifulSoup) -> dict[str, str]:
    years = {}
    for inp in soup.select('input[name="atGroupId"]'):
        if inp.get("value") and inp.get("data-name"):
            years[inp["value"]] = clean(inp["data-name"])
    return years


def parse_cards(soup: BeautifulSoup, page_url: str) -> list[dict]:
    out = []
    for c in soup.select("li.card.property-box"):
        h = c.select_one("h2")
        if not h:
            continue
        a = h.find("a")
        name = clean(h.get_text(" "))
        last = c.select(".card-body > div.w-100")
        price_block = clean(last[-1].get_text(" ")) if last else ""
        footer = c.select_one(".card-footer")
        ftxt = clean(footer.get_text(" ")) if footer else ""
        bookable = "Select Your Room" in ftxt
        amen = []
        modal = c.select_one(".modal-body")
        for sp in (modal or c).select("span.d-block"):
            t = clean(sp.get_text())
            if t and t not in amen:
                amen.append(t)
        prices = [float(x.replace(",", "")) for x in re.findall(r"[£€]\s*([\d,]+\.?\d*)", price_block)]
        weeks = [int(x) for x in re.findall(r"\b(\d{1,2})\b(?=[^£€]*Weeks)", price_block.split("£")[0].split("€")[0])]
        out.append({
            "name": name,
            "url": urljoin(page_url, a["href"]) if a and a.get("href") else "",
            "bookable": bookable,
            "price_text": price_block,
            "min_price": min(prices) if prices else None,
            "max_price": max(prices) if prices else None,
            "currency": money(price_block)[1],
            "weeks": weeks,
            "amenities": amen,
            "specials": "Specials available" in price_block,
        })
    return out


def parse_grade(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    res: dict = {"tenancies": [], "overview": "", "room_features": [], "property_features": [], "address": ""}
    for li in soup.select("#gradeAtPricing > li"):
        txt = clean(li.get_text(" "))
        wk = re.search(r"(\d+)\s*Weeks?", txt)
        dates = re.search(r"(\d{2}/\d{2}/\d{4})\s*to\s*(\d{2}/\d{2}/\d{4})", txt)
        price, cur = money(txt)
        book = li.select_one("a.book-now")
        btn_txt = clean(" ".join(x.get_text(" ") for x in li.select("a.btn, button.btn")))
        badges = [clean(b.get_text()) for b in li.select(".badge")]
        res["tenancies"].append({
            "weeks": int(wk.group(1)) if wk else None,
            "start": dates.group(1) if dates else "",
            "end": dates.group(2) if dates else "",
            "price": price,
            "currency": cur,
            "bookable": bool(book),
            "button": btn_txt,
            "specials": badges,
        })
    for h in soup.find_all("h2"):
        title = clean(h.get_text())
        sib = h.find_next_sibling()
        if title == "Overview" and sib:
            res["overview"] = clean(sib.get_text(" "))
        elif title in ("Room Type Features", "Property Features") and sib:
            items = [clean(x.get_text()) for x in sib.select("span.d-block")]
            key = "room_features" if title.startswith("Room") else "property_features"
            res[key] = [i for i in items if i]
    addr = soup.select_one("address")
    if addr:
        res["address"] = clean(addr.get_text(" "))
    res["sqm"] = find_sqm(res["overview"] + " " + " ".join(res["room_features"]))
    return res


def availability(bookable: bool, button: str) -> str:
    if bookable:
        return "available"
    b = (button or "").lower()
    if "wait" in b:
        return "waitlist"
    if "contact" in b or "enquire" in b or not b:
        return "sold_out"
    return b


# ---------------------------------------------------------------- main

def scrape_property(base: str, meta: dict) -> list[Room]:
    rows: list[Room] = []
    first = get(f"{base}/rooms")
    if not first:
        return rows
    soup = BeautifulSoup(first, "html.parser")
    years = parse_years(soup)
    name = meta.get("name") or ""
    if not name:
        t = soup.find("title")
        name = clean(t.get_text()).split("Accommodation")[-1].strip() if t else base.rsplit("/", 1)[-1]
    city_slug = base.split("/")[-2]
    city = meta.get("city") or city_slug.replace("-", " ").title()

    year_pages: list[tuple[str, BeautifulSoup, str]] = []
    if years:
        for yid, yname in years.items():
            url = f"{base}/rooms?academicYear={yid}"
            html = get(url)
            if html:
                year_pages.append((yname, BeautifulSoup(html, "html.parser"), url))
    else:
        year_pages.append(("", soup, f"{base}/rooms"))

    if meta.get("lat") is None:
        mh = get(f"{base}/mapsanddirections") or ""
        la = re.search(r"(?i)lat(?:itude)?[\"']?\s*[:=]\s*[\"']?(-?\d{1,2}\.\d{3,})", mh)
        lo = re.search(r"(?i)(?:lng|lon(?:gitude)?)[\"']?\s*[:=]\s*[\"']?(-?\d{1,3}\.\d{3,})", mh)
        if la and lo:
            meta["lat"], meta["lng"] = float(la.group(1)), float(lo.group(1))

    prop_amen: list[str] = []
    address = meta.get("street", "")
    for yname, ysoup, yurl in year_pages:
        for card in parse_cards(ysoup, yurl):
            g = None
            if card["url"] and "gradedetail" in card["url"]:
                gh = get(card["url"])
                g = parse_grade(gh) if gh else None
            if g and g["property_features"] and not prop_amen:
                prop_amen = g["property_features"]
            if g and g["address"] and not address:
                address = g["address"]
            common = dict(
                operator=OPERATOR,
                property_name=name,
                city=city,
                room_type=card["name"],
                room_category=normalize_category(card["name"]),
                academic_year=yname,
                address=address,
                postcode=meta.get("postcode", ""),
                lat=meta.get("lat"),
                lng=meta.get("lng"),
                property_url=base + "/",
                room_url=card["url"],
            )
            room_amen = (g["room_features"] if g and g["room_features"] else card["amenities"])
            sqm, sqm_max = g["sqm"] if g else (None, None)
            bed = bed_size(room_amen)
            tens = g["tenancies"] if g else []
            if tens:
                for t in tens:
                    gbp = t["price"] if t["currency"] in ("GBP", "") else None
                    rows.append(Room(
                        **common,
                        price_per_week_gbp=gbp,
                        tenancy_weeks=t["weeks"],
                        size_sqm=sqm,
                        bed_size=bed,
                        availability=availability(t["bookable"], t["button"]),
                        amenities=room_amen,
                        extra={
                            "currency": t["currency"] or card["currency"],
                            "price_per_week_local": t["price"],
                            "start_date": t["start"],
                            "end_date": t["end"],
                            "specials": t["specials"],
                            "size_sqm_max": sqm_max,
                            "overview": g["overview"] if g else "",
                            "property_amenities": [],
                        },
                    ))
            else:
                # fallback: card-level data (min price, weeks list)
                cur = card["currency"]
                rows.append(Room(
                    **common,
                    price_per_week_gbp=card["min_price"] if cur in ("GBP", "") else None,
                    tenancy_weeks=max(card["weeks"]) if card["weeks"] else None,
                    size_sqm=sqm,
                    bed_size=bed,
                    availability=availability(card["bookable"], "contact" if not card["bookable"] else ""),
                    amenities=room_amen,
                    extra={
                        "currency": cur,
                        "price_per_week_local": card["min_price"],
                        "price_max_local": card["max_price"],
                        "tenancy_options_weeks": card["weeks"],
                        "price_text": card["price_text"],
                        "source": "rooms_card",
                        "property_amenities": [],
                    },
                ))
    for r in rows:
        r.extra["property_amenities"] = prop_amen
        if not r.address:
            r.address = address
        if not r.postcode:
            m = POSTCODE_RE.search(r.address.upper())
            r.postcode = m.group(1) if m else ""
        # merged amenity list: room features + property features
        r.amenities = list(dict.fromkeys(r.amenities + prop_amen))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    props = list_properties()
    bases = sorted(props)
    print(f"[fresh] {len(bases)} properties found")
    if args.limit:
        bases = bases[: args.limit]
    rows: list[Room] = []
    for i, b in enumerate(bases, 1):
        try:
            pr = scrape_property(b, props[b])
        except Exception as e:
            print(f"  ! {b}: {e}", file=sys.stderr)
            pr = []
        print(f"[{i}/{len(bases)}] {b} -> {len(pr)} rows")
        rows.extend(pr)
    save(OPERATOR, rows)


if __name__ == "__main__":
    main()
