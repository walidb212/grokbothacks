"""Real deal qualification pipeline: V7 extraction -> comps -> MSCI exit yield -> NOI -> returns -> verdict -> memory."""
import json, pathlib, re, statistics
from engine import cortile, market, memory, model, uses, v7
from engine import rates as boe_rates
from engine.model import fig

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Market exit yield: MSCI RCA TrendTracker, UK Apartment cap rate, Q3 2026 (UK Student Housing series not served)
MSCI_EXIT_YIELD = fig("exit_yield", "Exit yield (MSCI UK apartment cap rate, Q3 2026)", 0.061, "%", source="msci",
                      ref="RCA TrendTracker CA2_UKCTRY_APT 2026-09-30")
ROOM_MIX = {"studio": 0.30, "ensuite": 0.50, "cluster": 0.20}
DEFAULT_RATES = {"studio": 350, "ensuite": 280, "cluster": 230}  # GBP/week, assumptions until comps arrive
COST_PER_BED = 140_000


def _num(text):
    m = re.search(r"([\d][\d,\.]*)", str(text or ""))
    return float(m.group(1).replace(",", "")) if m else None


def _beds(facts):
    for f in facts:
        if f["metric"] == "beds":
            m = re.search(r"([\d,]+)\s*-?\s*bed", str(f["value"]), re.I)
            if m:
                return int(m.group(1).replace(",", "")), f
    return 800, None


def _rates_from_comps():
    p = ROOT / "data/comps.json"
    if not p.exists():
        return {k: (v, None) for k, v in DEFAULT_RATES.items()}, []
    comps = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for k, v in DEFAULT_RATES.items():
        vals = [c["weekly_rate"] for c in comps if k in str(c.get("type", "")).lower() and c.get("weekly_rate")]
        out[k] = (statistics.median(vals), "web") if vals else (v, None)
    return out, comps


def analyze(paths, comment="", step=print, exit_yield=None, exit_yield_ref=None):
    """Run the full deal qualification on brochure PDFs. Returns {'assets': [...]}."""
    assets = []
    try:
        for path in paths:
            step(f"Extracting {pathlib.Path(path).name} with V7 Go")
            ext = v7.extract(path) if v7.configured() else {"facts": [], "mock": True}
            facts = ext["facts"]
            step("Location & demographics")
            beds, beds_fact = _beds(facts)
            addr = next((f["value"] for f in facts if f["metric"] == "address"), "")
            geo = market.geocode(addr)
            city = "London" if not geo or geo.get("region") == "London" else (geo.get("district") or "London")
            location = market.demand(city)
            step("Room-rate comps")
            uni = market.target_university(geo["lat"], geo["lng"]) if geo else None
            # competition = PBSA around the university the asset serves (not around the building)
            anchor = (uni["lat"], uni["lng"]) if uni else ((geo["lat"], geo["lng"]) if geo else None)
            comps = market.comps(*anchor) if anchor else []
            mr = market.rates_from_comps(comps, DEFAULT_RATES)
            rates = {k: (v[0], "web" if v[2] else None) for k, v in mr.items()}
            comp_ref = " ; ".join(f"{c['operator']} {c['property_name']} {c['url']}" for c in comps)
            rooms = [{"type": k, "beds": round(beds * s), "weekly_rate": mr[k][0], "weeks": mr[k][1]} for k, s in ROOM_MIX.items()]
            step("Market yield from MSCI")
            exit_y = exit_yield or MSCI_EXIT_YIELD["value"]
            exit_fig = fig("exit_yield", "Exit yield (MSCI, live via MCP)", exit_yield, "%", source="msci", ref=exit_yield_ref) if exit_yield else MSCI_EXIT_YIELD
            step("NOI build-up")
            noi = model.build_noi(rooms)
            step("OPEX benchmark (Cortile)")
            opex_err = None
            try:
                opex_bm = cortile.compare(noi["opex"], beds, city)
            except Exception as e:
                opex_bm, opex_err = [], f"{type(e).__name__}: {e}"
            step("Returns")
            build_cost = beds * COST_PER_BED
            asking = None
            ask_txt = next((f["value"] for f in facts if f["metric"] == "asking_price"), None)
            if ask_txt and "not stated" not in str(ask_txt).lower():
                asking = uses._money(ask_txt)
            land = asking or 0.0
            ret = model.compute_returns(noi["noi"], build_cost + land, exit_y)
            ret["figures"][3] = exit_fig
            step("Max land bid at 12% IRR")
            max_bid = model.residual_land_value(noi["noi"], build_cost, exit_y, 0.12)
            step("Sensitivity")
            sens = model.sensitivity(noi["noi"], build_cost, exit_y, land)
            step("Debt (SONIA)")
            sonia = boe_rates.sonia()
            debt_rate = sonia["value"] + 0.025
            lev = model.levered(noi["noi"], build_cost, exit_y, land, debt_rate)
            step("Alternative uses")
            alt = uses.compare(facts, {"noi": noi["noi"], "cost": build_cost}, exit_y)
            step("Verdict")
            if asking:
                ver = model.verdict(ret)
            else:
                ok = max_bid > 0
                ver = {"decision": "GO" if ok else "NO-GO",
                       "reasons": [f"No asking price: we can pay up to £{max_bid/1e6:,.1f}m for the site and still hit a 12% IRR"
                                   if ok else "Even a free site does not reach a 12% IRR",
                                   f"Yield on cost {ret['entry_yield']:.1%} vs market exit yield {exit_y:.1%} (MSCI)"]}
            appraisal = {
                "max_land_bid": fig("max_land_bid", "Max land bid at 12% IRR", round(max_bid), "GBP", "calc", ref="residual land value at 12% unlevered IRR"),
                "land_used": fig("land_cost", "Site price used", round(land), "GBP", "brochure" if asking else "assumption", ref=None if asking else "no asking price: returns shown before site cost"),
                "sensitivity": sens,
                "debt": [sonia, fig("debt_rate", "Debt cost (SONIA + 2.5% margin)", round(debt_rate, 4), "%", "calc", ref="SONIA (BoE) + 2.5% margin assumption"),
                         fig("ltc", "Loan to cost", 0.55, "%", "assumption"),
                         fig("levered_irr", "Levered IRR", round(lev["irr"], 4), "%", "calc"),
                         fig("levered_em", "Levered equity multiple", round(lev["equity_multiple"], 2), "x", "calc")],
                "alternatives": alt}
            name = next((f["value"] for f in facts if f["metric"] == "address"), pathlib.Path(path).stem)
            assets.append({
                "name": name, "verdict": ver, "returns": ret["figures"], "facts": facts,
                "room_types": [{"type": r["type"], "beds": r["beds"], "weeks": r["weeks"],
                                "weekly_rate": fig(f"{r['type']}_rate", f"{r['type']} rate", r["weekly_rate"], "GBP/week",
                                                   source=rates[r["type"]][1] or "assumption",
                                                   ref=(f"median of nearby comps around {uni['name'] if uni else 'the asset'}: " + comp_ref) if rates[r["type"]][1] else None)} for r in rooms],
                "comps": comps, "target_university": uni, "location": location, "appraisal": appraisal, "geo": geo, "noi": noi["figures"], "opex_benchmark": opex_bm, "opex_benchmark_error": opex_err,
                "assumptions": [fig("cost_per_bed", "Build cost per bed", COST_PER_BED, "GBP", "assumption"),
                                fig("room_mix", "Room mix", ROOM_MIX, None, "assumption"), fig("occupancy", "Occupancy", 0.97, "%", "assumption"),
                                fig("opex_pct", "OPEX ratio", 0.35, "% of gross", "assumption")],
                "comment": comment, "v7_entity": ext.get("v7_entity")})
    except Exception:
        raise
    report = {"assets": assets}
    try:
        step("Saving to deal memory (cognee)")
        memory.remember_report(report)
    except Exception:
        pass  # memory must never break the analysis
    return report


