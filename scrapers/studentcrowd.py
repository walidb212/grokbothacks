"""StudentCrowd (studentcrowd.com) reputation scraper: UK student accommodation ratings + reviews.

Discovery: sitemap.xml -> ~204 sitemap-university-*.xml -> /hall-l<loc>-s<id>-<slug> URLs (~2.3k halls).
Per hall page (server-rendered HTML, curl_cffi passes Cloudflare):
  - JSON-LD Accommodation (postcode, lat/lng, website, phone) + Product.aggregateRating (overall, review count)
  - provider (operator) name from data-track JSON attributes ("provider": "Unite Students")
  - sub-ratings from the summary star widths (width% / 20 = x/5)
  - city league table rank (#N in "<City> Hall Rankings", only if >= 10 reviews)
  - "from £X per week" price
Reviews (--reviews): /profile-reviews-.../reviews-chunk?page=N turbo-frame. Page 1 (4 latest reviews, with
per-review sub-ratings) is open; page >= 2 is behind a Cloudflare JS challenge, so we stop at the first 403.

Usage: python -m scrapers.studentcrowd [--limit N] [--reviews] [--workers 3] [--max-reviews 30]
Outputs: data/studentcrowd_properties.*, data/studentcrowd_providers.*, data/studentcrowd_reviews.* (with --reviews)
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

from scrapers.common import save, session

BASE = "https://www.studentcrowd.com"
DELAY = 0.3

# StudentCrowd provider label -> operator slug used by the other scrapers in this repo
OPERATOR_MAP = [
    (r"\bunite\b", "unite"),
    (r"^iq\b|\biq student", "iq"),
    (r"homes for students", "homes_for_students"),
    (r"student roost|\broost\b", "student_roost"),
    (r"\byugo\b", "yugo"),
    (r"^crm\b|crm students|\bcrm\b", "crm"),
    (r"fresh student", "fresh"),
    (r"vita student", "vita"),
    (r"scape", "scape"),
    (r"chapter", "chapter"),
    (r"host student|^host\b", "host"),
    (r"collegiate", "collegiate"),
    (r"student castle", "student_castle"),
    (r"downing", "downing"),
    (r"prestige", "prestige"),
    (r"true student|^true\b", "true"),
    (r"novel student", "novel"),
    (r"urbanest", "urbanest"),
    (r"nido", "nido"),
    (r"liberty living", "unite"),
    (r"campus living villages|\bclv\b", "clv"),
    (r"mezzino", "mezzino"),
    (r"sanctuary", "sanctuary"),
    (r"livensa", "livensa"),
    (r"every student", "every_student"),
    (r"ziggurat", "ziggurat"),
    (r"empiric|hello student", "hello_student"),
    (r"abodus", "abodus"),
    (r"kexgill", "kexgill"),
    (r"uliving", "uliving"),
    (r"study inn", "study_inn"),
    (r"collective", "the_collective"),
    (r"canvas", "canvas"),
    (r"student cribs|^cribs", "student_cribs"),
    (r"\bcityblock\b", "cityblock"),
    (r"zest", "zest"),
    (r"x1 ", "x1"),
]


def operator_slug(provider: str) -> str:
    p = (provider or "").strip().lower()
    if not p:
        return ""
    for pat, slug in OPERATOR_MAP:
        if re.search(pat, p):
            return slug
    return re.sub(r"[^a-z0-9]+", "_", p).strip("_")


@dataclass
class PropertyReview:
    """Reputation layer for one property (hall) on StudentCrowd."""
    source: str = "studentcrowd"
    property_name: str = ""
    provider: str = ""                 # raw StudentCrowd provider label, e.g. "Unite Students"
    operator: str = ""                 # normalized slug to join operator scrapers (unite, iq, yugo...)
    hall_type: str = ""                # private | university | part_nominated | ...
    city: str = ""
    university: str = ""               # university the hall is listed under
    overall_rating: float | None = None   # /5
    review_count: int | None = None
    rating_value_for_money: float | None = None
    rating_location: float | None = None
    rating_property_management: float | None = None
    rating_cleaning: float | None = None
    rating_social_spaces: float | None = None
    rating_social_experience: float | None = None
    rating_internet_wifi: float | None = None
    rating_maintenance: float | None = None
    rating_staff: float | None = None
    rating_security: float | None = None
    city_rank: int | None = None       # rank in "<city> Hall Rankings" (needs >= 10 reviews)
    city_rank_label: str = ""
    awards: list[str] = field(default_factory=list)
    price_from_gbp_week: float | None = None
    capacity_students: int | None = None
    address: str = ""
    postcode: str = ""
    lat: float | None = None
    lng: float | None = None
    telephone: str = ""
    website: str = ""
    property_url: str = ""
    provider_url: str = ""
    studentcrowd_id: str = ""
    scraped_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    extra: dict = field(default_factory=dict)


@dataclass
class ReviewText:
    property_url: str
    property_name: str
    provider: str
    operator: str
    city: str
    review_id: str = ""
    rating: float | None = None
    date: str = ""
    title: str = ""
    text: str = ""
    author: str = ""
    sub_ratings: dict = field(default_factory=dict)
    scraped_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))


SUBRATING_FIELDS = {
    "value for money": "rating_value_for_money",
    "location": "rating_location",
    "property management": "rating_property_management",
    "management": "rating_property_management",
    "cleaning": "rating_cleaning",
    "cleanliness": "rating_cleaning",
    "social spaces": "rating_social_spaces",
    "social experience": "rating_social_experience",
    "internet / wifi": "rating_internet_wifi",
    "internet": "rating_internet_wifi",
    "maintenance": "rating_maintenance",
    "staff": "rating_staff",
    "security": "rating_security",
}

_tls = threading.local()


def sess():
    if not hasattr(_tls, "s"):
        _tls.s = session()
    return _tls.s


def get(url: str, headers: dict | None = None, retries: int = 3):
    last = None
    for i in range(retries):
        try:
            time.sleep(DELAY)
            r = sess().get(url, headers=headers or {})
            if r.status_code == 200:
                return r
            last = r
            if r.status_code in (403, 404, 410):
                return r
        except Exception as e:  # network hiccup
            last = e
        time.sleep(1.5 * (i + 1))
    return last if not isinstance(last, Exception) else None


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def to_float(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- discovery
def discover_halls() -> list[str]:
    idx = get(f"{BASE}/sitemap.xml").text
    sms = re.findall(r"<loc>\s*(\S+sitemap-university-\d+\.xml)\s*</loc>", idx)
    print(f"[studentcrowd] {len(sms)} university sitemaps")
    halls: set[str] = set()

    def one(u):
        r = get(u)
        return re.findall(r"<loc>\s*(\S+/hall-l\d+-s\d+-[^<\s]+)\s*</loc>", r.text) if r is not None and r.status_code == 200 else []

    with ThreadPoolExecutor(4) as ex:
        for lst in ex.map(one, sms):
            halls.update(lst)
    out = sorted(u for u in halls if u.startswith(f"{BASE}/hall-"))  # UK only (drops /de/, /us/... locales)
    print(f"[studentcrowd] {len(out)} hall URLs")
    return out


# ---------------------------------------------------------------- parsing
def jsonld(html: str) -> list[dict]:
    out = []
    for m in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(m)
            out.extend(d if isinstance(d, list) else [d])
        except Exception:
            pass
    return out


def parse_hall(url: str, html: str) -> PropertyReview:
    p = PropertyReview(property_url=url)
    m = re.search(r"/hall-(l\d+)-(s\d+)-", url)
    if m:
        p.studentcrowd_id = m.group(2)
    lds = jsonld(html)
    for d in lds:
        t = d.get("@type")
        if t == "Accommodation":
            p.property_name = d.get("name") or p.property_name
            p.website = d.get("url") or ""
            p.telephone = d.get("telephone") or ""
            a = d.get("address") or {}
            p.postcode = a.get("postalCode") or ""
            p.address = ", ".join(x for x in [a.get("streetAddress", ""), a.get("addressLocality", "")] if x)
            p.lat, p.lng = to_float(d.get("latitude")), to_float(d.get("longitude"))
            p.extra["amenities"] = [f.get("name") for f in d.get("amenityFeature") or [] if f.get("name")]
        elif t == "Product":
            p.property_name = p.property_name or d.get("name") or ""
            ar = d.get("aggregateRating") or {}
            p.overall_rating = to_float(ar.get("ratingValue"))
            rc = ar.get("reviewCount")
            p.review_count = int(rc) if rc is not None else None
            p.extra["description"] = d.get("description") or ""
            p.extra["_ld_reviews"] = d.get("review") or []
        elif t == "BreadcrumbList":
            items = d.get("itemListElement") or []
            for it in items:
                iid = (it.get("item") or {}).get("@id", "")
                nm = it.get("name", "")
                if "/location-l" in iid and not p.city:
                    p.city = nm
                elif "/student-accommodation-l" in iid and not p.city:
                    p.city = re.sub(r"^Best Student Accommodation\s*", "", nm).strip()

    if not p.property_name:
        m = re.search(r"<title>\s*(.*?)\s*-\s*\d+\s*Student Reviews", html, re.S)
        if m:
            p.property_name = clean(m.group(1))
    if p.review_count is None:
        m = re.search(r"<title>[^<]*?-\s*(\d+)\s*Student Reviews", html)
        if m:
            p.review_count = int(m.group(1))

    # provider + hall type from data-track JSON
    provs = re.findall(r"&quot;provider&quot;:&quot;(.*?)&quot;", html)
    provs = [htmllib.unescape(x) for x in provs if x]
    if provs:
        p.provider = max(set(provs), key=provs.count)
    p.operator = operator_slug(p.provider)
    hts = [x for x in re.findall(r"&quot;hall_type&quot;:&quot;(.*?)&quot;", html) if x]
    if hts:
        p.hall_type = max(set(hts), key=hts.count)

    # university listed under
    m = re.search(r'href="(/best-halls-l\d+-s\d+-[^"]+)"', html)
    if m:
        p.extra["university_url"] = BASE + m.group(1)
        slug = re.sub(r"^/best-halls-l\d+-s\d+-", "", m.group(1))
        city_slug = url.rsplit("-", 1)[-1]
        slug = re.sub(rf"-{re.escape(city_slug)}$", "", slug)
        p.university = slug.replace("_", " ").replace("-", " ").title()

    # sub-ratings: summary blocks with star width %
    for blk in re.findall(r'data-profile-summary-target="rating".*?</span>', html, re.S):
        w = re.search(r"width:\s*([\d.]+)%", blk)
        lab = re.search(r"<span[^>]*>([^<]+)</span>\s*$", blk)
        if not (w and lab):
            continue
        label = clean(lab.group(1)).lower()
        val = round(float(w.group(1)) / 20, 2)
        fld = SUBRATING_FIELDS.get(label)
        if fld and getattr(p, fld) is None:
            setattr(p, fld, val)
        else:
            p.extra.setdefault("other_ratings", {})[label] = val

    text = clean(re.sub(r"<(script|style|svg)\b.*?</\1>", " ", html, flags=re.S))
    m = re.search(r"from\s*£\s*([\d,]+(?:\.\d+)?)\s*per week", text)
    if m:
        p.price_from_gbp_week = to_float(m.group(1))
    prices = [to_float(x) for x in re.findall(r"from\s*£\s*([\d,]+(?:\.\d+)?)\s*per week", text)]
    prices = [x for x in prices if x]
    if prices:
        p.price_from_gbp_week = min(prices)
        p.extra["room_prices_gbp_week"] = prices[1:] if len(prices) > 1 else prices
    m = re.search(r"([\d,]+)\s+students that live there", text)
    if m:
        p.capacity_students = int(m.group(1).replace(",", ""))
    m = re.search(r"([\d.]+)\s*mi from nearest campus", text)
    if m:
        p.extra["distance_nearest_campus_mi"] = to_float(m.group(1))

    # city league table rank: the entry linking to this hall in "<City> Hall Rankings"
    path = re.sub(r"^https?://[^/]+", "", url)
    m = re.search(r"([A-Z][\w' .&-]{1,40}?) Hall Rankings", text)
    if m:
        p.city_rank_label = m.group(0)
    m = re.search(rf'href="{re.escape(path)}"[^>]*>\s*<div[^>]*>\s*<div[^>]*>\s*<span>&#35;(\d+)</span>', html)
    if m:
        p.city_rank = int(m.group(1))

    # awards / accreditations (images after the heading)
    i = html.find("ACCREDITATION")
    if i > 0:
        seg = html[i:i + 6000]
        seg = seg[: seg.find("Write a review")] if "Write a review" in seg else seg
        p.awards = sorted({clean(a) for a in re.findall(r'<img[^>]*alt="([^"]+)"', seg) if clean(a)}
                          | {clean(a) for a in re.findall(r'<img[^>]*title="([^"]+)"', seg) if clean(a)})
    if p.overall_rating is None:  # below display threshold: star widths are 0, not real ratings
        for f in SUBRATING_FIELDS.values():
            if getattr(p, f) == 0:
                setattr(p, f, None)
    m = re.search(r'src="(/profile-reviews-[^"]+/reviews-chunk)\?page=\d+"', html)
    if m:
        p.extra["reviews_chunk"] = m.group(1)
    return p


def parse_review_chunk(html: str, p: PropertyReview) -> list[ReviewText]:
    out = []
    parts = re.split(r'(?=<div[^>]*id="review_[0-9a-f-]+")', html)
    for blk in parts[1:]:
        rid = re.search(r'id="review_([0-9a-f-]+)"', blk).group(1)
        r = ReviewText(property_url=p.property_url, property_name=p.property_name, provider=p.provider,
                       operator=p.operator, city=p.city, review_id=rid)
        m = re.search(r"stars--mdl--(\d+(?:-\d+)?)", blk)
        if m:
            r.rating = to_float(m.group(1).replace("-", "."))
        m = re.search(r"tw-ml-4\">\s*([^<]+?)\s*</span>", blk)
        if m:
            r.date = clean(m.group(1))
        m = re.search(r'<span class="tw-text-sc-silver-sand">\s*([^<]+?)\s*</span>', blk)
        if m:
            r.author = clean(m.group(1))
        m = re.search(r"<h[34][^>]*>(.*?)</h[34]>", blk, re.S)
        if m:
            r.title = clean(m.group(1))
        m = re.search(r'<p class="tw-px-6 tw-mb-4">(.*?)</p>', blk, re.S)
        if m:
            body = re.sub(r"<a href=\"#\".*?read more</a>", "", m.group(1), flags=re.S)
            r.text = clean(body)
        for sv, lab in re.findall(r'class="tw-inline-block stars stars--(\d+)"></div>\s*<span[^>]*>([^<]+)</span>', blk):
            r.sub_ratings[clean(lab)] = int(sv)
        out.append(r)
    return out


def reviews_from_ld(p: PropertyReview) -> list[ReviewText]:
    out = []
    for d in p.extra.get("_ld_reviews") or []:
        out.append(ReviewText(property_url=p.property_url, property_name=p.property_name, provider=p.provider,
                              operator=p.operator, city=p.city,
                              rating=to_float((d.get("reviewRating") or {}).get("ratingValue")),
                              date=(d.get("datePublished") or "")[:10], text=d.get("reviewBody") or "",
                              author=(d.get("author") or {}).get("name", "")))
    return out


REVIEW_PAGES = 1  # page >= 2 of reviews-chunk is behind a Cloudflare JS challenge (403) as of 2026-11


def fetch_reviews(p: PropertyReview, cap: int) -> list[ReviewText]:
    chunk = p.extra.get("reviews_chunk")
    if not chunk or not p.review_count:
        return []
    out: list[ReviewText] = []
    page = 1
    while len(out) < cap and page <= REVIEW_PAGES:
        r = get(f"{BASE}{chunk}?page={page}", headers={"Turbo-Frame": "turbo-reviews", "Referer": p.property_url}, retries=1)
        if r is None or r.status_code != 200:
            p.extra["reviews_blocked_at_page"] = page
            break
        got = parse_review_chunk(r.text, p)
        if not got:
            break
        out.extend(got)
        if len(out) >= (p.review_count or 0):
            break
        page += 1
    if not out:
        out = reviews_from_ld(p)
    # dates in chunk are like "9th July 2026": normalise to ISO
    for r in out:
        m = re.match(r"(\d+)\w*\s+(\w+)\s+(\d{4})", r.date or "")
        if m:
            try:
                r.date = time.strftime("%Y-%m-%d", time.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"))
            except ValueError:
                pass
    return out[:cap]


# ---------------------------------------------------------------- providers
def aggregate_providers(props: list[PropertyReview]) -> list[dict]:
    g: dict[str, list[PropertyReview]] = defaultdict(list)
    for p in props:
        g[p.provider or "(none)"].append(p)
    rows = []
    sub = [f for f in PropertyReview.__dataclass_fields__ if f.startswith("rating_")]
    for prov, ps in g.items():
        rated = [p for p in ps if p.overall_rating is not None and p.review_count]
        n = sum(p.review_count for p in rated)
        row = {
            "provider": prov, "operator": operator_slug(prov) if prov != "(none)" else "",
            "properties": len(ps), "properties_rated": len(rated), "total_reviews": n,
            "weighted_rating": round(sum(p.overall_rating * p.review_count for p in rated) / n, 3) if n else None,
            "mean_property_rating": round(sum(p.overall_rating for p in rated) / len(rated), 3) if rated else None,
            "ranked_properties": sum(1 for p in ps if p.city_rank),
            "cities": sorted({p.city for p in ps if p.city}),
            "min_price_from_gbp_week": min([p.price_from_gbp_week for p in ps if p.price_from_gbp_week] or [None], key=lambda x: x or 0),
        }
        for f in sub:
            vals = [(getattr(p, f), p.review_count) for p in rated if getattr(p, f) is not None]
            w = sum(c for _, c in vals)
            row[f] = round(sum(v * c for v, c in vals) / w, 3) if w else None
        rows.append(row)
    rows.sort(key=lambda r: -(r["total_reviews"] or 0))
    return rows


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--reviews", action="store_true")
    ap.add_argument("--max-reviews", type=int, default=30)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--only-reviewed", action="store_true", help="drop halls with 0 reviews from output")
    ap.add_argument("--review-pages", type=int, default=1, help="reviews-chunk pages to try (>=2 is CF-challenged)")
    a = ap.parse_args()
    global REVIEW_PAGES
    REVIEW_PAGES = a.review_pages

    halls = discover_halls()
    if a.limit:
        # prefer reviewed halls first is unknowable before fetching; just take a spread sample
        step = max(1, len(halls) // a.limit) if a.limit < len(halls) else 1
        halls = halls[::step][: a.limit]

    props: list[PropertyReview] = []
    reviews: list[ReviewText] = []
    lock = threading.Lock()
    fails = 0

    def work(u):
        r = get(u)
        if r is None or r.status_code != 200:
            print(f"  ! {u}: {getattr(r, 'status_code', 'error')}")
            return None, []
        p = parse_hall(u, r.text)
        revs = fetch_reviews(p, a.max_reviews) if a.reviews else []
        p.extra.pop("_ld_reviews", None)
        return p, revs

    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(work, u): u for u in halls}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                p, revs = f.result()
            except Exception as e:
                print(f"  ! {futs[f]}: {e}")
                p, revs = None, []
            with lock:
                if p is None:
                    fails += 1
                else:
                    props.append(p)
                    reviews.extend(revs)
            if i % 100 == 0:
                print(f"  {i}/{len(halls)} done, {fails} failed, {time.time() - t0:.0f}s")

    if a.only_reviewed:
        props = [p for p in props if p.review_count]
    props.sort(key=lambda p: (p.city, -(p.review_count or 0)))
    print(f"[studentcrowd] {len(props)} properties ok, {fails} failed")
    save("studentcrowd_properties", props)
    save("studentcrowd_providers", aggregate_providers(props))
    if a.reviews:
        save("studentcrowd_reviews", reviews)


if __name__ == "__main__":
    main()
