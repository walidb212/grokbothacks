"""OPEX benchmark from Cortile (synthetic UK building-management data, API read-only).
Service charge actuals per unit per year across real-looking UK blocks -> benchmark for our OPEX per bed.
Data is fetched at runtime and cached in data/ (gitignored): never committed."""
import json, pathlib, statistics
import requests
from engine.model import fig
from engine.v7 import _env

API = "https://hackathon.cortile.com/v1"
CACHE = (pathlib.Path("/app/cache") if pathlib.Path("/app/cache").exists() else pathlib.Path(__file__).resolve().parents[1] / "data") / "cortile_cache.json"


def _all(path, h):
    out, off = [], 0
    while True:
        for attempt in range(6):
            resp = requests.get(f"{API}/{path}", params={"limit": 100, "offset": off}, headers=h, timeout=30)
            r = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if "data" in r:
                break
            import time; time.sleep(2 * (attempt + 1))  # rate limited
        else:
            raise RuntimeError(f"Cortile {path} failed: {resp.status_code} {resp.text[:120]}")
        out += r["data"]
        p = r.get("pagination", {})
        if not p.get("has_more") or p["next_offset"] >= 10000:  # API cap; enough for a benchmark
            return out
        off = p["next_offset"]


def _load():
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    key = _env().get("CORTILE_API_KEY")
    if not key:
        return None
    h = {"x-api-key": key}
    d = {"buildings": _all("buildings", h), "charges": _all("service-charges", h)}
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(d), encoding="utf-8")
    return d


def benchmark(city="London"):
    # Cortile's API sits behind a Cloudflare challenge that blocks datacenter IPs: the server can use a
    # pre-computed aggregate (medians only, no raw rows) passed via env CORTILE_BENCHMARK_JSON.
    pre = _env().get("CORTILE_BENCHMARK_JSON")
    if pre:
        try:
            return json.loads(pre)
        except Exception:
            pass
    d = _load()
    if not d:
        return None
    blds = {b["id"]: b for b in d["buildings"]}
    latest = {}
    for c in d["charges"]:
        if c.get("actual_gbp") is None:
            continue
        k = c["building_id"]
        latest.setdefault(k, {}).setdefault(c["financial_year_end"], []).append(c)
    per_unit, cats = {}, {}
    for bid, years in latest.items():
        b = blds.get(bid)
        if not b or not b.get("unit_count"):
            continue
        fy = max(years)
        per_unit[bid] = sum(c["actual_gbp"] for c in years[fy]) / b["unit_count"]
        for c in years[fy]:
            cats.setdefault(c["category"], []).append(c["actual_gbp"] / b["unit_count"])
    local = [v for k, v in per_unit.items() if (blds[k].get("city") or "").lower() == city.lower()]
    sample = local if len(local) >= 5 else list(per_unit.values())
    scope = city if len(local) >= 5 else "UK"
    return {"median_per_unit": statistics.median(sample), "n": len(sample), "scope": scope,
            "by_category": {k: round(statistics.median(v)) for k, v in cats.items()}}


def compare(opex_total, beds, city="London"):
    """Our OPEX per bed vs Cortile service charges per unit. Returns figures."""
    bm = benchmark(city)
    if not bm:
        return []
    ours = opex_total / beds
    ratio = ours / bm["median_per_unit"]
    ref = f"Cortile API, {bm['n']} {bm['scope']} buildings, latest FY service-charge actuals"
    return [fig("opex_per_bed", "Our OPEX per bed", round(ours), "GBP/yr", "calc", ref="35% OPEX ratio assumption"),
            fig("cortile_service_charge_per_unit", f"Cortile median service charge per unit ({bm['scope']})",
                round(bm["median_per_unit"]), "GBP/yr", "cortile", ref=ref),
            fig("opex_vs_cortile", "Our OPEX vs Cortile benchmark", round(ratio, 2), "x", "cortile", ref=ref,
                status="green" if ratio >= 1 else "amber"),
            fig("cortile_by_category", "Cortile cost lines per unit (median)", bm["by_category"], "GBP/yr", "cortile", ref=ref)]
