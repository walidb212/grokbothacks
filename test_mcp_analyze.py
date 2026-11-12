"""End-to-end check through MCP, like Claude would: call analyze_deal on the Stratford brochure
and audit that every figure is traced to a source."""
import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PDF = sys.argv[1]


def figures(o):
    if isinstance(o, dict):
        if "metric" in o and "source" in o:
            yield o
        for v in o.values():
            yield from figures(v)
    elif isinstance(o, list):
        for v in o:
            yield from figures(v)


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print("tools:", tools)
            res = await s.call_tool("analyze_deal", {"path": PDF})
            rep = json.loads(res.content[0].text)
    a = rep["assets"][0]
    print("asset:", a["name"]); print("verdict:", a["verdict"])
    figs = list(figures(rep))
    by = {}
    for f in figs:
        by.setdefault(f["source"]["type"], []).append(f)
    untraced = [f["metric"] for f in figs if not f["source"].get("type")]
    print(f"\n{len(figs)} figures | by source: " + ", ".join(f"{k}={len(v)}" for k, v in by.items()))
    print("untraced:", untraced or "none")
    for t in ("brochure", "web", "msci", "cortile"):
        for f in by.get(t, [])[:4]:
            print(f"  [{t}] {f['label']}: {str(f['value'])[:70]}  <- {str(f['source'].get('ref'))[:70]}")
    print("  assumptions:", ", ".join(f["metric"] for f in by.get("assumption", [])))


asyncio.run(main())
