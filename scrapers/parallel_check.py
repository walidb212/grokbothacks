"""Cross-check a sample of scraped rows against Parallel's Task API (independent web research + citations).

For each sampled row, Parallel researches the operator's public listing and returns price/week, tenancy,
size, deposit and offers, with per-field citations and confidence. We compare with our scrape and write
data/parallel_check.csv: one line per row x field, verdict match / mismatch / not_found + source URLs.

Usage (worktree root):
  python -m scrapers.parallel_check --dry-run            # show the sample + payload, no API call
  PARALLEL_API_KEY=... python -m scrapers.parallel_check --per-operator 3 --processor base
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from scrapers.common import DATA_DIR

API = "https://api.parallel.ai/v1/tasks/runs"
CACHE = DATA_DIR / "parallel_cache.json"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "price_per_week_gbp": {"type": "number", "description": "Weekly rent in GBP for this exact room type, academic year and tenancy length. null if not published."},
        "tenancy_weeks": {"type": "number", "description": "Tenancy length in weeks for the price above."},
        "academic_year": {"type": "string", "description": "Academic year of the price, e.g. 2027/28."},
        "size_sqm": {"type": "number", "description": "Room size in square metres as published (smallest if a range). null if not published."},
        "deposit_gbp": {"type": "number", "description": "Deposit or booking fee in GBP required to book. 0 if explicitly none, null if not published."},
        "offer": {"type": "string", "description": "Current promotion or cashback for this room/property, empty string if none."},
        "source_url": {"type": "string", "description": "URL of the page where the price was found."},
    },
    "required": ["price_per_week_gbp", "tenancy_weeks", "academic_year", "size_sqm", "deposit_gbp", "offer", "source_url"],
    "additionalProperties": False,
}

# field -> (tolerance, kind)
COMPARE = {"price_per_week_gbp": (0.02, "rel"), "tenancy_weeks": (0, "abs"), "size_sqm": (1.0, "abs"), "deposit_gbp": (0, "abs")}


def sample(per_operator: int, category: str, seed: int) -> list[dict]:
    rows = list(csv.DictReader((DATA_DIR / "competitors.csv").open(encoding="utf-8-sig")))
    rows = [r for r in rows if r["price_per_week_gbp"] and (not category or r["room_category"] == category)]
    by_op: dict[str, list] = {}
    for r in rows:
        by_op.setdefault(r["operator"], []).append(r)
    rnd = random.Random(seed)
    out = []
    for op, rs in sorted(by_op.items()):
        out += rnd.sample(rs, min(per_operator, len(rs)))
    return out


def task_input(r: dict) -> str:
    return (
        f"UK student accommodation operator '{r['operator']}', property '{r['property_name']}' in {r['city']} "
        f"(listing: {r['property_url'] or 'unknown'}). Room type '{r['room_type']}', academic year {r['academic_year']}, "
        f"{r['tenancy_weeks']}-week tenancy. Find the currently published weekly price, tenancy length, room size, "
        f"deposit and any current offer for exactly this room type, from the operator's official website."
    )


def row_key(r: dict) -> str:
    return "|".join([r["operator"], r["city"], r["property_name"], r["room_type"], r["academic_year"], r["tenancy_weeks"]])


def run_task(client: httpx.Client, r: dict, processor: str) -> dict:
    body = {"input": task_input(r), "processor": processor,
            "task_spec": {"output_schema": {"type": "json", "json_schema": OUTPUT_SCHEMA}}}
    run = client.post(API, json=body).raise_for_status().json()
    for _ in range(30):  # result endpoint blocks up to `timeout` seconds per call
        res = client.get(f"{API}/{run['run_id']}/result", params={"timeout": 120}, timeout=150)
        if res.status_code == 200:
            return res.json()
        if res.status_code not in (202, 404, 408):
            res.raise_for_status()
        time.sleep(2)
    raise TimeoutError(run["run_id"])


def verdict(field: str, ours, theirs) -> str:
    if theirs in (None, "") or (field == "price_per_week_gbp" and float(theirs) < 50):
        return "not_found"  # Parallel answers 0 / tiny numbers when no exact price is published
    if ours in (None, ""):
        return "parallel_only"
    tol, kind = COMPARE[field]
    a, b = float(ours), float(theirs)
    diff = abs(a - b) / max(a, 1e-9) if kind == "rel" else abs(a - b)
    return "match" if diff <= tol else "mismatch"


def compare(r: dict, result: dict) -> list[dict]:
    out = result.get("output") or {}
    content = out.get("content") or {}
    basis = {b["field"]: b for b in out.get("basis") or []}
    lines = []
    for field in [*COMPARE, "offer"]:
        b = basis.get(field, {})
        theirs = content.get(field)
        lines.append({
            "operator": r["operator"], "property_name": r["property_name"], "city": r["city"],
            "room_type": r["room_type"], "academic_year": r["academic_year"], "tenancy_weeks": r["tenancy_weeks"],
            "field": field, "scraped": r.get(field, ""), "parallel": "" if theirs is None else theirs,
            "verdict": ("info" if field == "offer" else verdict(field, r.get(field), theirs)),
            "confidence": b.get("confidence", ""),
            "citations": " ; ".join(c.get("url", "") for c in b.get("citations") or []),
            "reasoning": (b.get("reasoning") or "")[:300],
            "scraped_url": r["room_url"] or r["property_url"], "parallel_source_url": content.get("source_url", ""),
            "run_id": result.get("run", {}).get("run_id", ""),
        })
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-operator", type=int, default=3)
    ap.add_argument("--category", default="studio", help="room_category filter, '' for all")
    ap.add_argument("--processor", default="base", help="lite | base | core | pro | ultra (cost/depth)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rows = sample(a.per_operator, a.category, a.seed)
    print(f"sample: {len(rows)} rows across {len({r['operator'] for r in rows})} operators")
    if a.dry_run:
        for r in rows[:3]:
            print(" -", task_input(r))
        print(json.dumps(OUTPUT_SCHEMA)[:300], "...")
        return

    key = os.environ.get("PARALLEL_API_KEY")
    if not key:
        raise SystemExit("PARALLEL_API_KEY not set")
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    todo = [r for r in rows if row_key(r) not in cache]
    print(f"{len(rows) - len(todo)} cached, {len(todo)} to run on processor={a.processor}")

    with httpx.Client(headers={"x-api-key": key}, timeout=60) as client:
        def go(r):
            try:
                return r, run_task(client, r, a.processor)
            except Exception as e:  # keep going, report at the end
                return r, {"error": repr(e)}
        with ThreadPoolExecutor(a.workers) as ex:
            for r, res in ex.map(go, todo):
                if "error" in res:
                    print(" !", row_key(r), res["error"])
                    continue
                cache[row_key(r)] = res
                CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    lines = [l for r in rows if row_key(r) in cache for l in compare(r, cache[row_key(r)])]
    if not lines:
        return
    with (DATA_DIR / "parallel_check.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(lines[0]))
        w.writeheader()
        w.writerows(lines)
    counts: dict[str, dict[str, int]] = {}
    for l in lines:
        counts.setdefault(l["field"], {}).setdefault(l["verdict"], 0)
        counts[l["field"]][l["verdict"]] += 1
    for f, c in counts.items():
        print(f"{f:20s} {c}")
    print(f"-> {DATA_DIR / 'parallel_check.csv'}")


if __name__ == "__main__":
    main()
