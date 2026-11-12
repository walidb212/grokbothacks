"""PBSA bed supply per city via Parallel Task API (one task per city, cited). Output: data/city_beds.csv.

  PARALLEL_API_KEY=... python -m scrapers.parallel_beds [--processor core] [--russell-group-only]
"""
import argparse
import csv
import json
import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from scrapers.common import DATA_DIR
from scrapers.parallel_check import API, run_task_body

CACHE = DATA_DIR / "parallel_beds_cache.json"
NUM = {"type": "number"}
SCHEMA = {
    "type": "object",
    "properties": {
        "total_student_beds": {**NUM, "description": "Total purpose-built student accommodation bed spaces in the city (private PBSA + university-owned halls). -1 if no published figure."},
        "private_pbsa_beds": {**NUM, "description": "Of which private/direct-let PBSA beds. -1 if not published."},
        "university_beds": {**NUM, "description": "Of which university-owned or -managed beds. -1 if not published."},
        "pipeline_beds": {**NUM, "description": "Beds under construction or with planning consent. -1 if not published."},
        "students_per_bed": {**NUM, "description": "Full-time students per PBSA bed as stated by the source. -1 if not stated."},
        "figure_year": {"type": "string", "description": "Year or academic year the figures refer to."},
        "source_name": {"type": "string", "description": "Publisher of the figure (e.g. Cushman & Wakefield, Knight Frank, StuRents, Cluttons, council report)."},
        "source_url": {"type": "string"},
    },
    "required": ["total_student_beds", "private_pbsa_beds", "university_beds", "pipeline_beds", "students_per_bed", "figure_year", "source_name", "source_url"],
    "additionalProperties": False,
}


def prompt(city):
    return (f"Purpose-built student accommodation (PBSA) supply in {city}, United Kingdom. Find the most recent published "
            f"figures (2024-2026 preferred) for total student bed spaces in {city} (private PBSA and university halls), "
            f"the split private vs university, the development pipeline, and the student-to-bed ratio if stated. Prefer "
            f"property consultancy reports (Cushman & Wakefield, Knight Frank, Savills, JLL, Cluttons, StuRents) or council "
            f"student housing studies. Use -1 when a figure is not published; do not estimate.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--processor", default="core")
    ap.add_argument("--russell-group-only", action="store_true")
    a = ap.parse_args()
    demand = list(csv.DictReader((DATA_DIR / "hesa_city_demand.csv").open(encoding="utf-8-sig")))
    if a.russell_group_only:
        demand = [d for d in demand if d["russell_group_students"] not in ("", "0")]
    cities = [d["city"] for d in demand]
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    todo = [c for c in cities if c not in cache]
    print(f"{len(cities)} cities, {len(todo)} to run on {a.processor}")
    with httpx.Client(headers={"x-api-key": os.environ["PARALLEL_API_KEY"]}, timeout=60) as client:
        def go(city):
            try:
                return city, run_task_body(client, {"input": prompt(city), "processor": a.processor,
                                                    "task_spec": {"output_schema": {"type": "json", "json_schema": SCHEMA}}})
            except Exception as e:
                return city, {"error": repr(e)}
        with ThreadPoolExecutor(10) as ex:
            for city, res in ex.map(go, todo):
                if "error" in res:
                    print(" !", city, res["error"])
                    continue
                cache[city] = res
                CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                print(" ok", city)

    rows = []
    for d in demand:
        res = cache.get(d["city"])
        if not res:
            continue
        out = res.get("output") or {}
        c = {k: (None if v == -1 else v) for k, v in (out.get("content") or {}).items()}
        basis = {b["field"]: b for b in out.get("basis") or []}
        tb = basis.get("total_student_beds", {})
        students = int(next(v for k, v in d.items() if k.startswith("students_")) or 0)
        beds = c.get("total_student_beds")
        rows.append({
            "city": d["city"], "students_hesa": students, "russell_group_share": d["russell_group_share"],
            "total_student_beds": beds, "private_pbsa_beds": c.get("private_pbsa_beds"),
            "university_beds": c.get("university_beds"), "pipeline_beds": c.get("pipeline_beds"),
            "students_per_bed_calc": round(students / beds, 2) if beds else None,
            "students_per_bed_source": c.get("students_per_bed"), "figure_year": c.get("figure_year"),
            "source_name": c.get("source_name"), "source_url": c.get("source_url"),
            "confidence": tb.get("confidence", ""), "citations": " ; ".join(x.get("url", "") for x in tb.get("citations") or []),
            "reasoning": (tb.get("reasoning") or "")[:300], "median_studio_pw": d["median_studio_pw_40w_plus"],
            "n_properties_scraped": d["n_properties_scraped"],
        })
    with (DATA_DIR / "city_beds.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"  {r['city']:<12} beds={r['total_student_beds']} students/bed={r['students_per_bed_calc']} ({r['figure_year']}, {r['source_name']}, {r['confidence']})")


if __name__ == "__main__":
    main()
