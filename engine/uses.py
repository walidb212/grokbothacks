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


def compare(facts, pbsa, exit_yield, target_irr=0.12):
    """Rank strategies by the most we could pay for the site (residual land value at target IRR).
    pbsa = {"noi","cost"}. BTR and existing use clearly labelled assumptions."""
    from engine.model import residual_land_value
    get = lambda k: next((f["value"] for f in facts if f["metric"] == k), None)
    opts = [{"use": "PBSA", "noi": pbsa["noi"], "cost": pbsa["cost"], "a": None,
             "basis": "rents from nearby PBSA comps x room mix, 35% OPEX, £140k/bed build"}]
    m = re.search(r"c?\.?\s*(\d+)\s*-?\s*units?\s*BTR", str(get("beds") or ""), re.I)
    units = int(m.group(1)) if m else 325
    opts.append({"use": "BTR", "noi": units * 2300 * 12 * 0.95 * (1 - 0.25), "cost": units * 300_000, "a": None,
                 "basis": f"{units} units x £2,300/month, 95% occ, 25% OPEX, £300k/unit build (assumptions)"})
    erv = _money(get("erv"))
    gia = _money("£" + str(get("gia_sqft") or "").replace("sq ft", "")) if get("gia_sqft") else None
    if erv:
        refurb = (gia or 100_000) * 150
        opts.append({"use": "Existing (refurbish and re-let to ERV)", "noi": erv * 0.9, "cost": refurb,
                     "a": {"build_years": 1, "hold_years_stabilised": 4},
                     "basis": "brochure ERV less 10% costs, refurb £150/sq ft (assumption)"})
    for o in opts:
        o["max_land_bid"] = residual_land_value(o["noi"], o["cost"], exit_yield, target_irr, o.pop("a"))
        o["yield_on_cost"] = o["noi"] / o["cost"] if o["cost"] else None
    best = max(opts, key=lambda x: x["max_land_bid"])
    return {"options": opts, "recommended": best["use"], "metric": f"max site bid at {target_irr:.0%} IRR",
            "figures": [fig("bid_" + o["use"].split()[0].lower(), f"Max site bid: {o['use']}", round(o["max_land_bid"]), "GBP",
                            "calc", ref=o["basis"]) for o in opts]}
