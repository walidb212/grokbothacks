"""dealt MCP server: the deal qualification tools for Claude, Grok and ChatGPT.

Remote (streamable HTTP): mounted by main.py at /mcp -> https://demo3.ai-vance.de/api/mcp
Local (stdio):            python api/mcp_app.py
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
if not (ROOT / "engine").exists():  # running from api/ in the repo
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from engine import market, model

mcp = FastMCP(
    "dealt",
    instructions="UK PBSA deal qualification. Every figure comes with its source (brochure page, operator website, MSCI, HESA). "
                 "Quote the source next to each number.",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def get_room_comps(postcode: str, k: int = 5) -> dict:
    """The k nearest purpose-built student accommodation buildings to a UK postcode, scraped from operator websites
    (Unite, iQ, Yugo, Fresh, Vita, Prestige, Student Roost, CRM): weekly rate per room type, tenancy weeks, room size,
    distance, photo and page URL. Also returns the median rates the NOI uses."""
    geo = market.geocode(postcode)
    if not geo:
        return {"error": f"Could not geocode {postcode!r}"}
    cs = market.comps(geo["lat"], geo["lng"], k=k)
    rates = market.rates_from_comps(cs, {"studio": 350, "ensuite": 280, "cluster": 230})
    return {"location": geo, "comps": cs,
            "median_rates": {t: {"weekly_rate": r[0], "weeks": r[1], "source": r[2] or "assumption"} for t, r in rates.items()}}


@mcp.tool()
def get_city_demand(city: str = "London") -> list:
    """Student demand for a UK city: HESA enrolments, Russell Group share, PBSA bed stock and students per bed, with sources."""
    return market.demand(city)


@mcp.tool()
def build_noi(rooms: list, assumptions: dict | None = None) -> dict:
    """NOI build-up: gross rent x occupancy - OPEX. rooms = [{"type": "studio", "beds": 240, "weekly_rate": 489, "weeks": 51}]."""
    return model.build_noi(rooms, assumptions)


@mcp.tool()
def compute_returns(noi: float, exit_yield: float, total_cost: float = 0, entry_price: float = 0,
                    assumptions: dict | None = None) -> dict:
    """IRR, equity multiple, entry vs exit yield. Use a market exit yield (MSCI, London PBSA ~5.25%)."""
    return model.compute_returns(noi, total_cost, exit_yield, assumptions, entry_price or None)


@mcp.tool()
def deal_verdict(irr: float, entry_yield: float, exit_yield: float, hurdle_irr: float = 0.12) -> dict:
    """GO / NO-GO with reasons, against a hurdle IRR."""
    return model.verdict({"irr": irr, "entry_yield": entry_yield, "exit_yield": exit_yield}, hurdle_irr)


@mcp.tool()
def ask_deal_memory(question: str) -> list:
    """Ask across every deal analysed so far, e.g. 'Which Stratford deals cleared a 12% IRR?' (cognee knowledge graph)."""
    from engine import memory
    return memory.ask(question)


@mcp.tool()
def get_sample_report() -> dict:
    """The full sourced report for the demo deal: 78-102 Broadway, Stratford E15 (verdict, returns, facts, comps, NOI)."""
    p = ROOT / "mock" / "report.json"
    return json.loads(p.read_text(encoding="utf-8"))["report"]


if __name__ == "__main__":
    mcp.run()
