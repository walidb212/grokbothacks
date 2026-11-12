"""Market evidence from the team's scrapers (market/*.csv): PBSA rent comps near the asset + city demand (HESA)."""
import csv, json, math, pathlib, re, statistics
from collections import Counter
import requests
from engine.model import fig

M = pathlib.Path(__file__).resolve().parents[1] / "market"
PC = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)
MIN_WEEKS = 38  # academic-year tenancies only
CATS = {"studio": ["studio"], "ensuite": ["ensuite"], "cluster": ["non_ensuite", "ensuite"]}


def _rows(name):
    p = M / name
    return list(csv.DictReader(open(p, encoding="utf-8-sig"))) if p.exists() else []


def geocode(text):
    m = PC.search(str(text or ""))
    if not m:
        return None
    try:
        r = requests.get(f"https://api.postcodes.io/postcodes/{m.group(1).replace(' ', '')}", timeout=10).json()["result"]
        return {"postcode": r["postcode"], "lat": r["latitude"], "lng": r["longitude"], "region": r.get("region"),
                "district": r.get("admin_district")}
    except Exception:
        return None


def _km(a, b, c, d):
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _images():
    p = M / "property_images.json"  # property_url -> og:image, built from the operators' pages
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def comps(lat, lng, k=5, year="2026/27"):
    """k nearest scraped PBSA buildings with priced rooms; per room category: median weekly rate + weeks.
    One academic year per building (the requested one if scraped, else its latest) so years never mix."""
    props = {}
    for r in _rows("competitors.csv"):
        if not (r["price_per_week_gbp"] and r["lat"] and r["lng"]):
            continue
        if r["tenancy_weeks"] and float(r["tenancy_weeks"]) < MIN_WEEKS:
            continue  # summer / short lets would drag the academic-year rate down
        props.setdefault((r["operator"], r["property_name"]), []).append(r)
    ranked = sorted(props.items(), key=lambda kv: _km(lat, lng, float(kv[1][0]["lat"]), float(kv[1][0]["lng"])))[:k]
    images = _images()
    out = []
    for (op, name), rows in ranked:
        years = Counter(r["academic_year"] for r in rows if r["academic_year"])
        yr = year if year in years else (years.most_common(1)[0][0] if years else "")
        rows = [r for r in rows if r["academic_year"] == yr] or rows
        d = _km(lat, lng, float(rows[0]["lat"]), float(rows[0]["lng"]))
        rooms = {}
        for cat in {r["room_category"] for r in rows}:
            rr = [r for r in rows if r["room_category"] == cat]
            prices = [float(r["price_per_week_gbp"]) for r in rr]
            weeks = [int(float(r["tenancy_weeks"])) for r in rr if r["tenancy_weeks"]]
            sizes = [float(r["size_sqm"]) for r in rr if r.get("size_sqm")]
            rooms[cat] = {"median_pw": statistics.median(prices), "min_pw": min(prices), "max_pw": max(prices),
                          "weeks": statistics.median(weeks) if weeks else None,
                          "size_sqm": round(statistics.median(sizes), 1) if sizes else None, "n": len(rr)}
        url = rows[0]["property_url"]
        img = images.get(url)
        out.append({"operator": op, "property_name": name, "postcode": rows[0]["postcode"], "city": rows[0]["city"],
                    "address": rows[0]["address"], "distance_km": round(d, 2), "academic_year": yr,
                    "rooms": rooms, "url": url, "image": img if str(img).startswith("http") else None})
    return out


def rates_from_comps(cs, defaults):
    """Median of the comps per room type -> {type: (rate, weeks, source_ref)}."""
    res = {}
    for t, cats in CATS.items():
        for c in cats:
            vals = [(x["rooms"][c]["median_pw"], x["rooms"][c]["weeks"]) for x in cs if c in x["rooms"]]
            if vals:
                w = [v[1] for v in vals if v[1]]
                res[t] = (statistics.median(v[0] for v in vals), int(statistics.median(w)) if w else 51,
                          f"median of {len(vals)} nearby comps ({c})")
                break
        else:
            res[t] = (defaults[t], 51, None)
    return res


def demand(city="London"):
    h = next((r for r in _rows("hesa_city_demand.csv") if r["city"].lower() == city.lower()), None)
    b = next((r for r in _rows("city_beds.csv") if r["city"].lower() == city.lower()), None)
    out = []
    if h:
        out += [fig("students", f"Students in {city} (HESA 2024/25)", int(h["students_2024_25"]), "students", "web", ref="HESA"),
                fig("russell_group_share", "Russell Group share of students", float(h["russell_group_share"]), "%", "web", ref="HESA"),
                fig("russell_group_universities", "Russell Group universities", h["russell_group_universities"], None, "web", ref="HESA")]
    if b:
        out += [fig("pbsa_beds", f"PBSA beds in {city}", float(b["total_student_beds"]), "beds", "web", ref=b["source_url"].split(" ; ")[0]),
                fig("students_per_bed", "Students per PBSA bed", float(b["students_per_bed_calc"]), "ratio", "web",
                    ref=b["source_url"].split(" ; ")[0],
                    status="green" if float(b["students_per_bed_calc"]) >= 3 else "amber"),
                fig("pipeline_beds", "Pipeline beds", float(b["pipeline_beds"] or 0), "beds", "web", ref=b["source_url"].split(" ; ")[0])]
    return out
