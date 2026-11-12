"""dealt MCP server: the deal qualification tools for Claude, Grok and ChatGPT.

Remote (streamable HTTP): mounted by main.py at /mcp -> https://demo3.ai-vance.de/api/mcp
Local (stdio):            python api/mcp_app.py
"""
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
if not (ROOT / "engine").exists():  # running from api/ in the repo
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import httpx

from engine import market, model

PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "https://demo3.ai-vance.de").rstrip("/")
HOOKS: dict = {}  # set by main.py: start_job(files, comment) -> job_id, job(job_id) -> dict | None


def report_url(job_id: str) -> str:
    return f"{PUBLIC_BASE}/?report={job_id}"


def _fmt(f: dict) -> str:
    v, u = f.get("value"), f.get("unit")
    if not isinstance(v, (int, float)):
        return str(v)
    money = lambda x: f"£{x / 1e6:,.1f}m" if abs(x) >= 1e6 else f"£{x / 1e3:,.0f}k" if abs(x) >= 1e3 else f"£{x:,.0f}"
    return {"%": f"{v * 100:.1f}%", "x": f"{v:.2f}x", "GBP/yr": f"{money(v)} pa", "GBP": money(v), "GBP/week": f"£{v:,.0f}/wk",
            "sqft": f"{v:,.0f} sq ft", "m": f"{v:,.0f} m", "acres": f"{v:,.2f} acres", "beds": f"{v:,.0f} beds"}.get(u, f"{v:,.0f}" if v >= 100 else f"{v:,.2f}")


def _link(f: dict | None, url: str) -> str:
    """A number as a markdown link to its source; brochure / MSCI / model figures open the report at that figure."""
    if not f:
        return "n/a"
    src = f.get("source") or {}
    ref = str(src.get("ref") or "")
    target = ref if ref.startswith("http") else "https://www.hesa.ac.uk/data-and-analysis/students" if ref == "HESA" else f"{url}#{f.get('metric')}"
    label = {"brochure": f"Brochure p.{src.get('page')}", "msci": "MSCI RCA", "model": "Calculated", "assumption": "Assumption"}.get(src.get("type"), ref or "Web")
    return f'[{_fmt(f)}]({target} "{label}")'


def memo_markdown(report: dict, url: str) -> str:
    """Investment memo where every number links to its source. Paste it as is: the links are the evidence."""
    out = []
    for a in report.get("assets", []):
        figs = {f["metric"]: f for sec in ("returns", "facts", "location", "noi") for f in a.get(sec) or [] if isinstance(f, dict) and f.get("metric")}
        for rt in a.get("room_types") or []:
            if rt.get("weekly_rate"):
                figs[rt["weekly_rate"]["metric"]] = rt["weekly_rate"]
        g = lambda m: _link(figs.get(m), url)
        v = a.get("verdict") or {}
        lines = [f"### {v.get('decision', '?')} · {a.get('name', 'Asset')}",
                 f"IRR {g('irr')} · equity multiple {g('equity_multiple')} · yield on cost {g('entry_yield') if 'entry_yield' in figs else g('yield_on_cost')} vs exit yield {g('exit_yield')}."]
        if v.get("reasons"):
            lines.append("Why: " + "; ".join(v["reasons"]) + ".")
        if "students" in figs:
            lines.append(f"Demand: {g('students')} students in London, {g('russell_group_share')} Russell Group, {g('pbsa_beds')} purpose-built, i.e. {g('students_per_bed')} students per bed.")
        comps = [c for c in a.get("comps") or [] if isinstance(c, dict) and c.get("property_name")]
        if comps:
            lines.append(f"Rents: studios {g('studio_rate')}, en-suites {g('ensuite_rate')}, from the {len(comps)} nearest operator buildings:")
            for c in comps:
                rooms = ", ".join(f"{k.replace('_', '-')} [£{r['median_pw']:,.0f}/wk]({c['url']})" for k, r in c["rooms"].items() if k != "other")
                lines.append(f"- **{c['property_name']}** ({c['operator']}, {c['distance_km']} km): {rooms}")
        if "noi" in figs:
            lines.append(f"NOI {g('noi')} = gross {g('gross_income')} − OPEX {g('opex')}.")
        out.append("\n".join(lines))
    out.append(f"[Open the full report with photos and source popovers]({url})")
    return "\n\n".join(out)


def _deliver(report: dict, job_id: str) -> dict:
    url = report_url(job_id)
    return {"report_url": url, "memo_markdown": memo_markdown(report, url),
            "verdicts": [a.get("verdict") for a in report.get("assets", [])],
            "how_to_answer": "Reply with memo_markdown as is (every number is a link to its source), then the report_url."}

mcp = FastMCP(
    "dealt",
    instructions="UK PBSA deal qualification. Every figure comes with its source (brochure page, operator website, MSCI, HESA). "
                 "Quote the source next to each number.",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def qualify_brochure(pdf_url: str, comment: str = "", exit_yield: float = 0, exit_yield_ref: str = "") -> dict:
    """Qualify a deal from a sale brochure PDF URL (e.g. an email attachment link): V7 extraction, comps, MSCI exit yield,
    NOI, returns, GO / NO-GO. Returns a job_id; call get_report(job_id) after ~60 seconds.
    If you have the MSCI MCP connected, first get the market cap rate (get_rca_trendtracker / get_rca_peerfinder)
    and pass it as exit_yield (fraction, e.g. 0.055) with exit_yield_ref naming the MSCI series or deals."""
    if "start_job" not in HOOKS:
        return {"error": "The pipeline is only available on the hosted server."}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(pdf_url)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        return {"error": f"Could not download a PDF from {pdf_url} ({r.status_code})"}
    name = pathlib.Path(httpx.URL(pdf_url).path).name or "brochure.pdf"
    opts = {"exit_yield": exit_yield, "exit_yield_ref": exit_yield_ref or None} if exit_yield else None
    job_id = await HOOKS["start_job"]([(name if name.lower().endswith(".pdf") else name + ".pdf", r.content)], comment, opts)
    return {"job_id": job_id, "status": "running", "report_url": report_url(job_id), "next": "Call get_report(job_id) in about a minute."}


@mcp.tool()
async def get_report(job_id: str) -> dict:
    """Status and result of a qualification: verdict, the investment memo in markdown (every number links to its source)
    and a link to the full interactive report (photos of the comps, source popovers)."""
    job = HOOKS["job"](job_id) if "job" in HOOKS else None
    if not job:
        return {"error": f"Unknown job {job_id}"}
    if job.get("status") != "done":
        await asyncio.sleep(5)
        job = HOOKS["job"](job_id) or job
        if job.get("status") != "done":
            return {"status": job.get("status"), "steps_done": job.get("steps"), "next": "Call get_report again in 20 seconds."}
    return {"status": "done", **_deliver(job["report"], job_id)}


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
    """The demo deal, 78-102 Broadway, Stratford E15: verdict, investment memo in markdown (every number links to its source)
    and a link to the full interactive report."""
    p = ROOT / "mock" / "report.json"
    return _deliver(json.loads(p.read_text(encoding="utf-8"))["report"], "demo-stratford")


if __name__ == "__main__":
    mcp.run()
