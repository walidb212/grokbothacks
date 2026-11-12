"""MCP server exposing the deal qualification tools to Claude / Grok Bot / Cursor.
Run: python -m mcp_server.server   (stdio)"""
import json, os, pathlib
from mcp.server.fastmcp import FastMCP
from engine import model

mcp = FastMCP("deal-qualification")
ROOT = pathlib.Path(__file__).resolve().parents[1]


@mcp.tool()
def analyze_deal(path: str, comment: str = "") -> dict:
    """Full deal qualification of a sale brochure PDF: V7 extraction, PBSA comps around the university,
    city demand, MSCI exit yield, NOI, returns, max site bid, sensitivity, debt (SONIA), alternative uses, verdict.
    Every figure carries its source (brochure page, web URL, MSCI series, Cortile, or 'assumption')."""
    from engine.pipeline import analyze
    return analyze([path], comment, step=lambda s: None)


@mcp.tool()
def extract_brochure(path: str) -> dict:
    """Extract asset facts from a sale brochure PDF via V7 Go (each fact linked to the brochure).
    Falls back to the mock report if V7 is not configured in .env."""
    from engine import v7
    if v7.configured():
        return v7.extract(path)
    mock = json.loads((ROOT / "mock/report.json").read_text(encoding="utf-8"))
    return {"source_file": path, "facts": mock["report"]["assets"][0]["facts"], "mock": True}


@mcp.tool()
def get_room_comps(postcode: str) -> list:
    """Comparable PBSA buildings near a postcode: room type, weekly rate, weeks, operator, url.
    Reads the scraper output (data/comps.json, gitignored)."""
    p = ROOT / "data/comps.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


@mcp.tool()
def build_noi(rooms: list, assumptions: dict | None = None) -> dict:
    """NOI build-up. rooms = [{"type","beds","weekly_rate","weeks"}]."""
    return model.build_noi(rooms, assumptions)


@mcp.tool()
def compute_returns(noi: float, exit_yield: float, total_cost: float = 0, entry_price: float = 0,
                    assumptions: dict | None = None) -> dict:
    """IRR, equity multiple, entry vs exit yield. Use exit_yield from MSCI TrendTracker / Peer Finder."""
    return model.compute_returns(noi, total_cost, exit_yield, assumptions, entry_price or None)


@mcp.tool()
def deal_verdict(irr: float, entry_yield: float, exit_yield: float, hurdle_irr: float = 0.12) -> dict:
    """GO / NO-GO with reasons."""
    return model.verdict({"irr": irr, "entry_yield": entry_yield, "exit_yield": exit_yield}, hurdle_irr)


@mcp.tool()
def ask_deal_memory(question: str) -> list:
    """Ask a question across every deal analysed so far, e.g. 'Have we seen this seller before?',
    'Which Stratford deals cleared a 12% IRR?'. Backed by a cognee knowledge graph."""
    from engine import memory
    return memory.ask(question)


if __name__ == "__main__":
    mcp.run()
