"""Deal memory with cognee: every analysed deal goes into a knowledge graph
(asset, location, tenants, verdict, returns) so we can ask questions across the whole deal flow.
Needs in .env: LLM_API_KEY=... (cognee uses it for cognify + embeddings)."""
import asyncio, json, os
from engine.v7 import _env


def _ready():
    e = _env()
    return bool(e.get("LLM_API_KEY"))


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
    """Add every asset of an analysis report to the deal memory. Best effort."""
    if not _ready():
        return False
    asyncio.run(_remember([_deal_text(a) for a in report.get("assets", [])]))
    return True


def ask(question):
    if not _ready():
        return ["Deal memory not configured (LLM_API_KEY missing in .env)."]
    return asyncio.run(_ask(question))
