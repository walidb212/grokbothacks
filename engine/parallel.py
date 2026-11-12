"""Parallel web search (with a source on every result): tenant news and local market signals."""
import requests
from engine.model import fig
from engine.v7 import _env

API = "https://api.parallel.ai/v1beta/search"


def configured():
    return bool(_env().get("PARALLEL_API_KEY"))


def search(objective, queries, max_results=5):
    r = requests.post(API, headers={"x-api-key": _env()["PARALLEL_API_KEY"], "Content-Type": "application/json"},
                      json={"objective": objective, "search_queries": queries, "max_results": max_results}, timeout=60)
    r.raise_for_status()
    return r.json().get("results", [])


def tenant_check(tenants, area):
    """Recent news on the tenants and on PBSA supply in the area. Returns traced figures."""
    out = []
    for t in [x.strip() for x in str(tenants or "").split(",") if x.strip() and "not stated" not in x.lower()][:2]:
        res = search(f"Recent financial health, store closures or insolvency news about {t} (UK retailer)", [f"{t} news 2026", f"{t} administration closures"], 3)
        if res:
            out.append(fig(f"tenant_news_{t[:12].lower().replace(' ', '_')}", f"Tenant news: {t}", res[0].get("title"), None, "web",
                           ref=res[0].get("url"), status="amber"))
    res = search(f"New purpose-built student accommodation (PBSA) schemes planned or approved near {area}", [f"{area} student accommodation planning approved 2026"], 3)
    for r in res[:2]:
        out.append(fig("pipeline_news", "PBSA pipeline nearby", r.get("title"), None, "web", ref=r.get("url")))
    return out
