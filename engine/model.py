"""Deal qualification engine: NOI build-up, returns, verdict.
Every output figure follows the shared format: metric/label/value/unit/status/source."""


def fig(metric, label, value, unit, source="calc", ref=None, page=None, status=None):
    return {"metric": metric, "label": label, "value": value, "unit": unit, "status": status,
            "source": {"type": source, "ref": ref, "page": page}}


DEFAULTS = {"weeks": 51, "occupancy": 0.97, "opex_pct": 0.35, "rent_growth": 0.03,
            "build_years": 3, "hold_years_stabilised": 2, "acq_costs_pct": 0.068,
            "leaseup": 0.90}  # first operating year at 90% of stabilised NOI


def build_noi(rooms, a=None):
    """rooms: [{"type": "studio", "beds": 240, "weekly_rate": 350, "weeks": 51}]"""
    a = {**DEFAULTS, **(a or {})}
    gross = sum(r["beds"] * r["weekly_rate"] * r.get("weeks", a["weeks"]) for r in rooms) * a["occupancy"]
    opex = gross * a["opex_pct"]
    return {"gross": gross, "opex": opex, "noi": gross - opex,
            "figures": [fig("gross_income", "Gross income", round(gross), "GBP/yr", "model",
                            f"Σ beds × weekly rate × weeks × {a['occupancy']:.0%} occupancy"),
                        fig("opex", "OPEX", round(opex), "GBP/yr", "model", f"{a['opex_pct']:.0%} of gross income"),
                        fig("noi", "NOI", round(gross - opex), "GBP/yr", "model", "Gross income − OPEX")]}


def _irr(cf):
    npv = lambda r: sum(c / (1 + r) ** (i + 1) for i, c in enumerate(cf))
    lo, hi = -0.9, 1.0
    for _ in range(200):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if npv(m) > 0 else (lo, m)
    return m


def compute_returns(noi, total_cost, exit_yield, a=None, entry_price=None):
    """Development case (total_cost spread over build) or investment case (entry_price)."""
    a = {**DEFAULTS, **(a or {})}
    g = a["rent_growth"]
    if entry_price:  # standing investment
        cost = entry_price * (1 + a["acq_costs_pct"])
        cf = [-cost + noi] + [noi * (1 + g) ** i for i in range(1, 4)]
        cf.append(noi * (1 + g) ** 4 + noi * (1 + g) ** 5 / exit_yield)
        entry_yield = noi / cost
    else:  # development
        cost = total_cost
        cf = dev_cashflows(noi, cost, exit_yield, 0.0, a)
        entry_yield = noi / cost
    irr = _irr(cf)
    em = sum(c for c in cf if c > 0) / -sum(c for c in cf if c < 0)
    return {"irr": irr, "equity_multiple": em, "entry_yield": entry_yield, "exit_yield": exit_yield,
            "figures": [fig("irr", "IRR (unlevered)", round(irr, 4), "%", "model",
                            f"Cash flows: cost, NOI growing {g:.0%} a year, exit at the market yield"),
                        fig("equity_multiple", "Equity multiple", round(em, 2), "x", "model", "Cash returned ÷ cash invested"),
                        fig("entry_yield", "Entry / yield on cost", round(entry_yield, 4), "%", "model", "NOI ÷ total cost"),
                        fig("exit_yield", "Exit yield", exit_yield, "%", source="msci"),
                        fig("yield_delta", "Entry minus exit yield", round(entry_yield - exit_yield, 4), "%", "model",
                            "Yield on cost − market exit yield")]}


def verdict(returns, hurdle_irr=0.12):
    reasons, ok = [], True
    if returns["entry_yield"] <= returns["exit_yield"]:
        ok = False; reasons.append("Entry yield at or below market exit yield")
    else:
        reasons.append("Entry yield above market exit yield")
    if returns["irr"] < hurdle_irr:
        ok = False; reasons.append(f"IRR below {hurdle_irr:.0%} hurdle")
    else:
        reasons.append(f"IRR clears {hurdle_irr:.0%} hurdle")
    return {"decision": "GO" if ok else "NO-GO", "reasons": reasons}


# ---------- development appraisal extras ----------

def dev_cashflows(noi, build_cost, exit_yield, land=0.0, a=None):
    a = {**DEFAULTS, **(a or {})}
    g, n = a["rent_growth"], a["build_years"]
    cf = [-build_cost / n] * n
    cf[0] -= land
    ops = [noi * (1 + g) ** i for i in range(a["hold_years_stabilised"])]
    ops[0] *= a["leaseup"]  # lease-up: first year below stabilised occupancy
    exit_value = noi * (1 + g) ** a["hold_years_stabilised"] / exit_yield  # sold on forward NOI
    cf += ops[:-1] + [ops[-1] + exit_value]
    return cf


def residual_land_value(noi, build_cost, exit_yield, target_irr=0.12, a=None):
    """Max price payable for the site so that the unlevered IRR hits target_irr."""
    lo, hi = 0.0, build_cost * 3
    if _irr(dev_cashflows(noi, build_cost, exit_yield, 0, a)) < target_irr:
        return 0.0
    for _ in range(100):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if _irr(dev_cashflows(noi, build_cost, exit_yield, m, a)) > target_irr else (lo, m)
    return lo


def sensitivity(noi, build_cost, exit_yield, land=0.0, a=None):
    """IRR grid: build cost x rent (-10%/base/+10%) and exit yield +/-50bp."""
    steps = (-0.10, 0.0, 0.10)
    grid = [{"build_cost": c, "rent": r,
             "irr": round(_irr(dev_cashflows(noi * (1 + r), build_cost * (1 + c), exit_yield, land, a)), 4)}
            for c in steps for r in steps]
    yields = [{"exit_yield": round(exit_yield + d, 4),
               "irr": round(_irr(dev_cashflows(noi, build_cost, exit_yield + d, land, a)), 4)}
              for d in (-0.005, 0.0, 0.005)]
    return {"cost_x_rent": grid, "exit_yield": yields}


def levered(noi, build_cost, exit_yield, land, rate, ltc=0.55, a=None, fee_pct=0.01):
    """Interest-only debt = LTC x total cost, drawn with costs, repaid at sale; arrangement fee on each draw.
    Returns levered IRR / multiple."""
    cf = dev_cashflows(noi, build_cost, exit_yield, land, a)
    debt_cf, bal = [], 0.0
    for i, c in enumerate(cf):
        draw = -c * ltc if c < 0 else 0.0
        interest = bal * rate + draw * fee_pct
        bal += draw
        repay = bal if i == len(cf) - 1 else 0.0
        debt_cf.append(c + draw - interest - repay)
    irr = _irr(debt_cf)
    em = sum(x for x in debt_cf if x > 0) / -sum(x for x in debt_cf if x < 0)
    return {"irr": irr, "equity_multiple": em, "loan": bal, "rate": rate, "ltc": ltc}
