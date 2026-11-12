"""Merge all operator outputs into data/all_rooms.csv + studio benchmark per city/operator."""
import csv
import json
import statistics
from collections import defaultdict

from scrapers.common import DATA_DIR

SKIP = {"all_rooms", "studentcrowd_properties", "studentcrowd_reviews", "studio_benchmark"}


def _pkey(r):
    return (r["city"].strip().lower(), r["property_name"].strip().lower())


def dedupe_crm_yugo(rows):
    """CRM is a Yugo brand: most CRM rows are scraped from yugo.com. Keep the yugo row, flag it crm_brand."""
    yugo_props = {_pkey(r) for r in rows if r["operator"] == "yugo"}
    crm_props = {_pkey(r) for r in rows if r["operator"] == "crm"}
    out = []
    for r in rows:
        if r["operator"] == "crm" and _pkey(r) in yugo_props:
            continue
        if r["operator"] == "yugo" and _pkey(r) in crm_props:
            r.setdefault("extra", {})["crm_brand"] = True
        out.append(r)
    print(f"crm/yugo dedupe: dropped {len(rows) - len(out)} crm rows duplicated in yugo")
    return out


def main():
    rows = []
    for p in sorted(DATA_DIR.glob("*.json")):
        if p.stem in SKIP or p.stem.startswith("studentcrowd"):
            continue
        rows += json.loads(p.read_text(encoding="utf-8"))
    if not rows:
        print("no data")
        return
    rows = dedupe_crm_yugo(rows)
    keys = list(rows[0].keys())
    with (DATA_DIR / "all_rooms.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in r.items()})

    groups = defaultdict(list)
    for r in rows:
        if r.get("room_category") == "studio" and r.get("price_per_week_gbp"):
            groups[(r["city"].strip().title(), r["operator"])].append(r)
    out = []
    for (city, op), rs in sorted(groups.items()):
        prices = [r["price_per_week_gbp"] for r in rs]
        sizes = [r["size_sqm"] for r in rs if r.get("size_sqm")]
        out.append({
            "city": city, "operator": op, "n_studio_rows": len(rs),
            "n_properties": len({r["property_name"] for r in rs}),
            "min_pw": min(prices), "median_pw": statistics.median(prices), "max_pw": max(prices),
            "median_sqm": statistics.median(sizes) if sizes else "",
            "median_gbp_per_sqm_pw": round(statistics.median(r["price_per_week_gbp"] / r["size_sqm"] for r in rs if r.get("size_sqm")), 2) if sizes else "",
        })
    if out:
        with (DATA_DIR / "studio_benchmark.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
            w.writeheader()
            w.writerows(out)
    by_op = defaultdict(int)
    for r in rows:
        by_op[r["operator"]] += 1
    print(f"{len(rows)} rows", dict(by_op), f"| studio benchmark: {len(out)} city x operator cells")


if __name__ == "__main__":
    main()
