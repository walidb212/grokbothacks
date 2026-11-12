"""Real deal qualification pipeline: V7 extraction -> comps -> MSCI exit yield -> NOI -> returns -> verdict -> memory."""
import json, pathlib, re, statistics
from engine import market, memory, model, v7
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


def analyze(paths, comment="", step=print):
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
            comps = market.comps(geo["lat"], geo["lng"]) if geo else []
            mr = market.rates_from_comps(comps, DEFAULT_RATES)
            rates = {k: (v[0], "web" if v[2] else None) for k, v in mr.items()}
            rooms = [{"type": k, "beds": round(beds * s), "weekly_rate": mr[k][0], "weeks": mr[k][1]} for k, s in ROOM_MIX.items()]
            step("Market yield from MSCI")
            exit_y = MSCI_EXIT_YIELD["value"]
            step("NOI build-up")
            noi = model.build_noi(rooms)
            step("Returns")
            ret = model.compute_returns(noi["noi"], beds * COST_PER_BED, exit_y)
            step("Verdict")
            ver = model.verdict(ret)
            ret["figures"][3] = MSCI_EXIT_YIELD
            name = next((f["value"] for f in facts if f["metric"] == "address"), pathlib.Path(path).stem)
            assets.append({
                "name": name, "verdict": ver, "returns": ret["figures"], "facts": facts,
                "room_types": [{"type": r["type"], "beds": r["beds"], "weeks": r["weeks"],
                                "weekly_rate": fig(f"{r['type']}_rate", f"{r['type']} rate", r["weekly_rate"], "GBP/week",
                                                   source=rates[r["type"]][1] or "assumption")} for r in rooms],
                "comps": comps, "location": location, "geo": geo, "noi": noi["figures"],
                "assumptions": [fig("cost_per_bed", "Build cost per bed", COST_PER_BED, "GBP"),
                                fig("room_mix", "Room mix", ROOM_MIX, None), fig("occupancy", "Occupancy", 0.97, "%"),
                                fig("opex_pct", "OPEX ratio", 0.35, "% of gross")],
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


