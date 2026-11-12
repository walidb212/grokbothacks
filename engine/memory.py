"""Deal memory with cognee: every analysed deal goes into a knowledge graph
(asset, location, tenants, verdict, returns) so we can ask questions across the whole deal flow.
Needs in .env: LLM_API_KEY=... (cognee uses it for cognify + embeddings)."""
import asyncio, json, os
from engine.v7 import _env


def _cloud():
    e = _env()
    if e.get("COGNEE_API_KEY") and e.get("COGNEE_API_URL"):
        h = {"X-Api-Key": e["COGNEE_API_KEY"]}
        if e.get("COGNEE_TENANT_ID"):
            h["X-Tenant-Id"] = e["COGNEE_TENANT_ID"]
        return e["COGNEE_API_URL"].rstrip("/") + "/api/v1", h
    return None


def _ready():
    e = _env()
    return bool(_cloud() or e.get("LLM_API_KEY"))


def _deal_text(asset):
    lines = [f"Deal: {asset.get('name')}",
             f"Verdict: {asset['verdict']['decision']} ({'; '.join(asset['verdict']['reasons'])})"]
    for group in ("facts", "returns", "noi"):
        for f in asset.get(group, []):
            src = f.get("source", {})
            lines.append(f"{f.get('label') or f['metric']}: {f.get('value')} {f.get('unit') or ''} "
                         f"[source: {src.get('type')} {src.get('ref') or ''} {src.get('page') or ''}]".strip())
    return "\n".join(lines)


async def _remember(texts):
    import cognee
    await cognee.add(texts, dataset_name="deal_flow")
    await cognee.cognify(datasets=["deal_flow"])


async def _ask(question):
    import cognee
    res = await cognee.search(query_text=question)
    return [str(r) for r in res]


def remember_report(report):
    """Add every asset of an analysis report to the deal memory (cognee cloud if configured, else local)."""
    import requests
    texts = [_deal_text(a) for a in report.get("assets", [])]
    c = _cloud()
    if c:
        base, h = c
        for t in texts:
            requests.post(f"{base}/add", headers=h, data={"datasetName": "deal_flow", "raw_data": t}, timeout=60).raise_for_status()
        requests.post(f"{base}/cognify", headers={**h, "Content-Type": "application/json"},
                      json={"datasets": ["deal_flow"]}, timeout=300).raise_for_status()
        return True
    if not _ready():
        return False
    asyncio.run(_remember(texts))
    return True


def ask(question):
    import requests
    c = _cloud()
    if c:
        base, h = c
        r = requests.post(f"{base}/search", headers={**h, "Content-Type": "application/json"},
                          json={"query": question, "search_type": "GRAPH_COMPLETION"}, timeout=120)
        r.raise_for_status()
        res = r.json()
        return res if isinstance(res, list) else [res]
    if not _ready():
        return ["Deal memory not configured (LLM_API_KEY missing in .env)."]
    return asyncio.run(_ask(question))
