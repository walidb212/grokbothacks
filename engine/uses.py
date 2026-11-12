"""Compare the brochure's alternative strategies on the same site: PBSA vs BTR vs hold the existing building."""
import re
from engine.model import fig


def _money(text):
    m = re.search(r"£\s*([\d,\.]+)\s*(m|million|k)?", str(text or ""), re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    return v * 1e6 if unit in ("m", "million") else v * 1e3 if unit == "k" else v


def _all_money(text):
    """Every £ amount in a text (deduplicated), e.g. an ERV breakdown per building."""
    out = []
    for m in re.finditer(r"£\s*([\d,\.]+)\s*(m|million|k)?", str(text or ""), re.I):
        v = _money(m.group(0))
        if v and v not in out:
            out.append(v)
    return out


def compare(facts, pbsa, exit_yields, target_irr=0.12):
    """Rank strategies by the most we could pay for the site (residual land value at target IRR).
    pbsa = {"noi","cost"}; exit_yields = {"PBSA": y, "BTR": y, "Existing": y}. BTR / existing use labelled assumptions."""
    from engine.model import residual_land_value
    get = lambda k: next((f["value"] for f in facts if f["metric"] == k), None)
    opts = [{"use": "PBSA", "noi": pbsa["noi"], "cost": pbsa["cost"], "a": None, "exit": exit_yields["PBSA"],
             "basis": "rents from PBSA comps around the university x room mix, 35% OPEX, build + fees + contingency"}]
    m = re.search(r"c?\.?\s*(\d+)\s*-?\s*units?\s*BTR", str(get("beds") or ""), re.I)
    units = int(m.group(1)) if m else 325
    opts.append({"use": "BTR", "noi": units * 2300 * 12 * 0.95 * (1 - 0.25), "cost": units * 300_000 * 1.17, "a": None,
                 "exit": exit_yields["BTR"],
                 "basis": f"{units} units x £2,300/month, 95% occ, 25% OPEX, £300k/unit + 17% fees and contingency (assumptions)"})
    # every ERV in the breakdown, minus the passing rent (already inside the ERV): robust to V7 wording
    passing = _money(get("passing_rent"))
    ervs = [v for v in _all_money(get("erv_breakdown")) if not passing or abs(v - passing) > 1] or _all_money(get("erv"))
    if ervs:
        erv = sum(ervs)
        gia = _money("£" + str(get("gia_sqft") or "").replace("sq ft", "")) if get("gia_sqft") else None
        refurb = (gia or 100_000) * 100
        opts.append({"use": "Existing (refurbish and re-let)", "noi": erv * 0.9, "cost": refurb,
                     "a": {"build_years": 1, "hold_years_stabilised": 4}, "exit": exit_yields["Existing"],
                     "basis": f"brochure ERVs {', '.join(f'£{v/1e6:,.2f}m' for v in ervs)} less 10% costs, refurb £100/sq ft (assumption)"})
    for o in opts:
        o["max_land_bid"] = residual_land_value(o["noi"], o["cost"], o.pop("exit"), target_irr, o.pop("a"))
        o["yield_on_cost"] = o["noi"] / o["cost"] if o["cost"] else None
    best = max(opts, key=lambda x: x["max_land_bid"])
    return {"options": opts, "recommended": best["use"], "metric": f"max site bid at {target_irr:.0%} IRR",
            "figures": [fig("bid_" + o["use"].split()[0].lower(), f"Max site bid: {o['use']}", round(o["max_land_bid"]), "GBP",
                            "model", ref=o["basis"]) for o in opts]}
