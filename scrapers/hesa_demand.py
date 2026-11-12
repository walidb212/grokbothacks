"""Demand side: HESA student enrolments per city vs our PBSA supply (competitors.csv).

Input: HESA DT051 Table 1 CSV (CC BY 4.0, "Source: HESA") in data/hesa/. Providers are mapped to cities
(1) via universities our properties list as nearby in that city, (2) via the city name in the provider name.
Output: data/hesa_city_demand.csv + data/hesa_provider_city.csv (the mapping, for audit).
"""
import csv
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from scrapers.common import DATA_DIR

DEFAULT = DATA_DIR / "hesa" / "dt051-table-1.csv"
# The 24 Russell Group universities (HESA provider names).
RUSSELL_GROUP = {
    "The University of Birmingham", "The University of Bristol", "The University of Cambridge", "Cardiff University",
    "University of Durham", "The University of Edinburgh", "The University of Exeter", "The University of Glasgow",
    "Imperial College of Science, Technology and Medicine", "King's College London", "The University of Leeds",
    "The University of Liverpool", "London School of Economics and Political Science", "The University of Manchester",
    "Newcastle University", "University of Nottingham", "The University of Oxford", "Queen Mary University of London",
    "Queen's University Belfast", "The University of Sheffield", "The University of Southampton",
    "University College London", "The University of Warwick", "The University of York",
}

# Distance-learning / multi-site providers: no single city, excluded from city demand.
EXCLUDE = {"The Open University", "Arden University", "BPP University", "The University of Law"}
# Fixes where automatic matching is wrong or misses a big provider. Value "" = leave unmapped.
OVERRIDES = {
    "Imperial College of Science, Technology and Medicine": "London",
    "Middlesex University": "London",
    "Roehampton University": "London",
    "The University of St. Andrews": "",
    "Canterbury Christ Church University": "Canterbury",
    "The University of Kent": "Canterbury",
    "Loughborough University": "Loughborough",
    "Teesside University": "Middlesbrough",
    "The University of Hull": "Hull",
}
# Franchise-heavy providers: headcount includes students taught by partners in other cities.
FRANCHISE_HEAVY = {"Bath Spa University", "Anglia Ruskin University", "University of Wales Trinity Saint David",
                   "Leeds Trinity University", "Buckinghamshire New University", "University of Suffolk"}


def load_hesa(path: Path):
    lines = path.read_text(encoding="utf-8-sig").lstrip("﻿").splitlines()
    meta = {l.split(",", 1)[0].strip(":"): l.split(",", 1)[1].strip('" ') for l in lines[:9] if "," in l}
    start = next(i for i, l in enumerate(lines) if l.startswith('"UKPRN"'))
    rows = []
    for r in csv.DictReader(lines[start:]):
        if r["UKPRN"]:
            rows.append({"ukprn": r["UKPRN"], "provider": r["HE provider"], "students": int(r["Total"].replace(",", ""))})
    year = next((l.split(",")[2].strip() for l in lines if l.startswith(",Academic year")), "")
    return rows, year, meta


def norm(s: str) -> str:
    s = re.sub(r"\(.*?\)|\d+\s*min.*|[^a-z ]", " ", s.lower().replace("&", " and "))
    s = re.sub(r"\b(walk|to|the)\b", " ", s)
    return " ".join(s.split())


def cited_universities(comp):
    """city -> Counter(normalized university name) from university_nearby."""
    out = defaultdict(Counter)
    for r in comp:
        for u in re.split(r";|, (?=[A-Z])", r["university_nearby"] or ""):
            n = norm(u)
            if len(n) > 6 and "city centre" not in n and "station" not in n:
                out[r["city"].strip().title()][n] += 1
    return out


def map_providers(providers, comp):
    cited = cited_universities(comp)
    cities = sorted(cited, key=len, reverse=True)
    mapping = {}
    for p in providers:
        if p["provider"] in EXCLUDE:
            continue
        if p["provider"] in OVERRIDES:
            if OVERRIDES[p["provider"]]:
                mapping[p["ukprn"]] = (OVERRIDES[p["provider"]], "manual")
            continue
        pn = norm(p["provider"])
        best = None
        for city, names in cited.items():  # (1) exact normalized match with a cited university
            if pn in names and (best is None or names[pn] > best[1]):
                best = (city, names[pn], "cited_nearby")
        if not best:  # (2) city name inside provider name, e.g. "Leeds Beckett University"
            for city in cities:
                if re.search(rf"\b{re.escape(city.lower())}\b", pn):
                    best = (city, 0, "name_contains_city")
                    break
        if best:
            mapping[p["ukprn"]] = (best[0], best[2])
    return mapping


def main(path=DEFAULT):
    providers, year, meta = load_hesa(Path(path))
    comp = list(csv.DictReader((DATA_DIR / "competitors.csv").open(encoding="utf-8-sig")))
    mapping = map_providers(providers, comp)

    with (DATA_DIR / "hesa_provider_city.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ukprn", "provider", "students", "city", "match_method", "russell_group"])
        for p in sorted(providers, key=lambda p: -p["students"]):
            c, m = mapping.get(p["ukprn"], ("", ""))
            if p["provider"] in EXCLUDE:
                m = "excluded_distance_or_multisite"
            w.writerow([p["ukprn"], p["provider"], p["students"], c, m, int(p["provider"] in RUSSELL_GROUP)])

    demand = defaultdict(lambda: {"students": 0, "providers": []})
    for p in providers:
        if p["ukprn"] in mapping:
            d = demand[mapping[p["ukprn"]][0]]
            d["students"] += p["students"]
            d["providers"].append(p["provider"])
            if p["provider"] in RUSSELL_GROUP:
                d["rg_students"] = d.get("rg_students", 0) + p["students"]
                d.setdefault("rg", []).append(p["provider"])
            if p["provider"] in FRANCHISE_HEAVY:
                d["franchise_students"] = d.get("franchise_students", 0) + p["students"]

    supply = defaultdict(lambda: {"props": set(), "ops": set(), "studio_pw": []})
    for r in comp:
        s = supply[r["city"].strip().title()]
        s["props"].add((r["operator"], r["property_name"]))
        s["ops"].add(r["operator"])
        if r["room_category"] == "studio" and r["price_per_week_gbp"] and float(r["tenancy_weeks"] or 0) >= 40:
            s["studio_pw"].append(float(r["price_per_week_gbp"]))

    out = []
    for city in sorted(set(demand) | set(supply)):
        d, s = demand.get(city, {"students": 0, "providers": []}), supply.get(city)
        n_props = len(s["props"]) if s else 0
        out.append({
            "city": city, f"students_{year.replace('/', '_')}": d["students"], "n_providers": len(d["providers"]),
            "n_properties_scraped": n_props, "operators": ",".join(sorted(s["ops"])) if s else "",
            "russell_group_students": d.get("rg_students", 0),
            "russell_group_share": round(d.get("rg_students", 0) / d["students"], 2) if d["students"] else "",
            "russell_group_universities": " ; ".join(d.get("rg", [])),
            "of_which_franchise_heavy_providers": d.get("franchise_students", 0),
            "students_per_scraped_property": round(d["students"] / n_props) if n_props and d["students"] else "",
            "median_studio_pw_40w_plus": statistics.median(s["studio_pw"]) if s and s["studio_pw"] else "",
            "providers": " ; ".join(d["providers"]),
        })
    out.sort(key=lambda r: -list(r.values())[1])
    with (DATA_DIR / "hesa_city_demand.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    total = sum(p["students"] for p in providers)
    mapped = sum(p["students"] for p in providers if p["ukprn"] in mapping)
    print(f"HESA {year}: {len(providers)} providers, {total:,} students; mapped to a city: "
          f"{len(mapping)} providers, {mapped:,} students ({mapped / total:.0%}). Source: HESA ({meta.get('Licence', '')})")
    for r in out[:15]:
        print(f"  {r['city']:<14} {list(r.values())[1]:>8,} students  {r['n_properties_scraped']:>3} props  "
              f"studio median {r['median_studio_pw_40w_plus'] or '-'}  {r['students_per_scraped_property']}/prop")


if __name__ == "__main__":
    main(*sys.argv[1:])
