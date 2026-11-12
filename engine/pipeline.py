"""Deal qualification pipeline: V7 extraction -> campus + comps -> demand -> MSCI exit yield -> NOI -> returns
-> max site bid -> sensitivity -> debt -> alternative uses -> verdict -> memory.
Steps are reported as "Tool · action · detail" so the UI can show which tool is working."""
import pathlib, re
from engine import cortile, market, memory, model, parallel, uses, v7
from engine import rates as boe_rates
from engine.model import fig
from engine.v7 import _env

ROOM_MIX = {"studio": 0.30, "ensuite": 0.50, "cluster": 0.20}
DEFAULT_RATES = {"studio": 350, "ensuite": 280, "cluster": 230}  # GBP/week, only if no comps
COST_PER_BED = 140_000   # hard build cost
FEES_PCT = 0.12          # professional fees
CONTINGENCY_PCT = 0.05
DEBT_MARGIN = 0.0275
TARGET_IRR = 0.12


def _exit(use):
    """Exit yield per use from env (MSCI values are licensed: never hard-coded in the repo)."""
    e = _env()
    v, ref = e.get(f"MSCI_EXIT_YIELD_{use}"), e.get(f"MSCI_EXIT_YIELD_{use}_REF")
    if v:
        return float(v), fig("exit_yield", f"Exit yield ({use})", float(v), "%", "msci", ref=ref)
    return 0.065, fig("exit_yield", f"Exit yield ({use})", 0.065, "%", "assumption", ref="MSCI value not configured")


def _beds(facts):
    for f in facts:
        if f["metric"] == "beds":
            m = re.search(r"([\d,]+)\s*-?\s*bed", str(f["value"]), re.I)
            if m:
                return int(m.group(1).replace(",", ""))
    return 800


def analyze(paths, comment="", step=print, exit_yield=None, exit_yield_ref=None):
    """Run the full deal qualification on brochure PDFs. Returns {'assets': [...]}."""
    assets = []
    for path in paths:
        name_pdf = pathlib.Path(path).name
        step(f"V7 Go · Reading the brochure · {name_pdf}")
        ext = v7.extract(path) if v7.configured() else {"facts": [], "mock": True}
        facts = ext["facts"]
        get = lambda k: next((f["value"] for f in facts if f["metric"] == k), None)
        step(f"V7 Go · Extracted {len(facts)} facts · every value tied to its page")
        beds = _beds(facts)
        geo = market.geocode(get("address") or "")
        city = "London" if not geo or geo.get("region") == "London" else (geo.get("district") or "London")
        step(f"postcodes.io · Located the site · {geo['postcode'] if geo else 'no postcode found'}")
        uni = market.target_university(geo["lat"], geo["lng"]) if geo else None
        if uni:
            step(f"dealt · Campus served · {uni['name']} ({uni['distance_km']} km)")
        anchor = (uni["lat"], uni["lng"]) if uni else ((geo["lat"], geo["lng"]) if geo else None)
        comps = market.comps(*anchor) if anchor else []
        ops = ", ".join(sorted({c["operator"].replace("_", " ").title() for c in comps}))
        step(f"Scrapers · PBSA competition around the campus · {len(comps)} buildings ({ops})")
        mr = market.rates_from_comps(comps, DEFAULT_RATES)
        comp_ref = " ; ".join(f"{c['operator']} {c['property_name']} {c['url']}" for c in comps)
        rooms = [{"type": k, "beds": round(beds * s), "weekly_rate": mr[k][0], "weeks": mr[k][1]} for k, s in ROOM_MIX.items()]
        location = market.demand(city)
        spb = next((f["value"] for f in location if f["metric"] == "students_per_bed"), None)
        step(f"HESA · Student demand in {city} · {spb} students per PBSA bed" if spb else f"HESA · Student demand in {city}")
        web_checks = []
        if parallel.configured():
            area = (geo or {}).get("district") or city
            step(f"Parallel · Searching the web · tenant news and PBSA pipeline near {area}")
            try:
                web_checks = parallel.tenant_check(get("tenants"), area)
                step(f"Parallel · {len(web_checks)} sourced results")
            except Exception as e:
                step(f"Parallel · search failed ({type(e).__name__})")
        if exit_yield:
            exit_y = exit_yield
            exit_fig = fig("exit_yield", "Exit yield (MSCI, live via MCP)", exit_yield, "%", "msci", ref=exit_yield_ref)
        else:
            exit_y, exit_fig = _exit("PBSA")
        ysrc = "MSCI" if exit_fig["source"]["type"] == "msci" else "assumption"
        step(f"MSCI RCA · Market exit yield · {exit_y:.1%}" if ysrc == "MSCI" else f"dealt · Exit yield · {exit_y:.1%} (assumption, MSCI not configured)")
        noi = model.build_noi(rooms)
        opex_err = None
        try:
            opex_bm = cortile.compare(noi["opex"], beds, city)
            step("Cortile · OPEX benchmark · London building costs per unit")
        except Exception as e:
            opex_bm, opex_err = [], f"{type(e).__name__}: {e}"
        # development cost: hard cost + fees + contingency (site excluded)
        hard = beds * COST_PER_BED
        build_cost = hard * (1 + FEES_PCT + CONTINGENCY_PCT)
        asking = None
        ask_txt = get("asking_price")
        if ask_txt and "not stated" not in str(ask_txt).lower():
            asking = uses._money(ask_txt)
        land = asking or 0.0
        ret = model.compute_returns(noi["noi"], build_cost + land, exit_y)
        ret["figures"][3] = exit_fig
        if not asking:
            for f in ret["figures"]:
                if f["metric"] in ("irr", "equity_multiple", "entry_yield", "yield_delta"):
                    f["label"] += " (before site cost)"
                    f["source"]["ref"] = (f["source"].get("ref") or "") + " | No asking price: site cost excluded; see max site bid."
        max_bid = model.residual_land_value(noi["noi"], build_cost, exit_y, TARGET_IRR)
        step(f"dealt · NOI £{noi['noi']/1e6:,.1f}m · " + (f"max site bid £{max_bid/1e6:,.1f}m at {TARGET_IRR:.0%} IRR" if max_bid > 0
             else f"PBSA misses {TARGET_IRR:.0%} IRR even with a free site"))
        sens = model.sensitivity(noi["noi"], build_cost, exit_y, land)
        sonia = boe_rates.sonia()
        step(f"Bank of England · SONIA {sonia['value']:.2%}")
        debt_rate = sonia["value"] + DEBT_MARGIN
        lev = model.levered(noi["noi"], build_cost, exit_y, land, debt_rate)
        btr_y, _ = _exit("BTR")
        alt = uses.compare(facts, {"noi": noi["noi"], "cost": build_cost}, {"PBSA": exit_y, "BTR": btr_y, "Existing": btr_y})
        step(f"dealt · Compared PBSA, BTR and existing use · best: {alt['recommended']}")
        if asking:
            ver = model.verdict(ret)
        else:
            best = max(alt["options"], key=lambda o: o["max_land_bid"])
            ok = max_bid > 0
            ver = {"decision": "GO" if ok else ("GO (other use)" if best["max_land_bid"] > 0 else "NO-GO"),
                   "reasons": [f"No asking price: we can pay up to £{max_bid/1e6:,.1f}m for the site as PBSA and still hit a {TARGET_IRR:.0%} IRR"
                               if ok else f"PBSA does not reach a {TARGET_IRR:.0%} IRR even with a free site"
                               + (f"; {best['use']} supports a site bid up to £{best['max_land_bid']/1e6:,.1f}m" if best["max_land_bid"] > 0 else ""),
                               f"Yield on cost {ret['entry_yield']:.1%} vs market exit yield {exit_y:.1%} ({ysrc})",
                               f"Best use of the site: {alt['recommended']}"]}
        lev_label = "Levered IRR" if asking else "Levered IRR (before site cost)"
        appraisal = {
            "max_land_bid": fig("max_land_bid", f"Max site bid at {TARGET_IRR:.0%} IRR", round(max_bid), "GBP", "model",
                                ref="Residual land value: site price that leaves a 12% unlevered IRR"),
            "land_used": fig("land_cost", "Site price used", round(land), "GBP", "brochure" if asking else "assumption",
                             ref=None if asking else "No asking price: returns shown before site cost"),
            "costs": [fig("hard_cost", "Hard build cost", round(hard), "GBP", "assumption", ref=f"£{COST_PER_BED:,}/bed"),
                      fig("fees", "Professional fees", round(hard * FEES_PCT), "GBP", "assumption", ref=f"{FEES_PCT:.0%} of hard cost"),
                      fig("contingency", "Contingency", round(hard * CONTINGENCY_PCT), "GBP", "assumption",
                          ref=f"{CONTINGENCY_PCT:.0%} of hard cost"),
                      fig("total_dev_cost", "Total development cost (excl. site)", round(build_cost), "GBP", "model")],
            "sensitivity": sens,
            "debt": [sonia,
                     fig("debt_rate", "Debt cost (SONIA + margin)", round(debt_rate, 4), "%", "model",
                         ref=f"SONIA (BoE, proxy for the swap rate) + {DEBT_MARGIN:.2%} margin assumption"),
                     fig("ltc", "Loan to cost", 0.55, "%", "assumption"),
                     fig("arrangement_fee", "Arrangement fee", 0.01, "% of loan", "assumption"),
                     fig("levered_irr", lev_label, round(lev["irr"], 4), "%", "model"),
                     fig("levered_em", "Levered equity multiple", round(lev["equity_multiple"], 2), "x", "model")],
            "alternatives": alt}
        assets.append({
            "name": get("address") or pathlib.Path(path).stem, "address": get("address"), "rates": appraisal["debt"][:2], "verdict": ver, "returns": ret["figures"], "facts": facts,
            "room_types": [{"type": r["type"], "beds": r["beds"], "weeks": r["weeks"],
                            "weekly_rate": fig(f"{r['type']}_rate", f"{r['type']} rate", r["weekly_rate"], "GBP/week",
                                               source="web" if mr[r["type"]][2] else "assumption",
                                               ref=(f"{mr[r['type']][2]} around {uni['name'] if uni else 'the asset'}: {comp_ref}")
                                               if mr[r["type"]][2] else "No comps found: default rate")} for r in rooms],
            "comps": comps, "target_university": uni, "location": location, "web_checks": web_checks, "appraisal": appraisal,
            "geo": geo, "noi": noi["figures"], "opex_benchmark": opex_bm, "opex_benchmark_error": opex_err,
            "assumptions": [fig("cost_per_bed", "Hard build cost per bed", COST_PER_BED, "GBP", "assumption"),
                            fig("room_mix", "Room mix", ROOM_MIX, None, "assumption"),
                            fig("occupancy", "Stabilised occupancy", 0.97, "%", "assumption"),
                            fig("leaseup", "First operating year", 0.90, "% of stabilised NOI", "assumption"),
                            fig("opex_pct", "OPEX ratio", 0.35, "% of gross", "assumption")],
            "comment": comment, "v7_entity": ext.get("v7_entity"), "source_file": name_pdf})
    report = {"assets": assets}
    try:
        if memory.remember_report(report):
            step("cognee · Saved to deal memory · ask questions across every deal")
    except Exception:
        pass
    return report
