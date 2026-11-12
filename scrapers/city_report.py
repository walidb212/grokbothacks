"""City-level competitor view: HESA demand + StudentCrowd supply/reputation/nomination + our room-level prices.

Outputs: data/city_report.csv (one row per city) and data/city_operators.csv (city x operator league table).
"""
import csv
import json
import statistics
from collections import Counter, defaultdict

from scrapers.common import DATA_DIR


CITY_ALIASES = {"Central London": "London", "East London": "London", "North London": "London", "West London": "London",
                "South East London": "London", "South West London": "London", "Newcastle Upon Tyne": "Newcastle"}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    demand = {d["city"]: d for d in csv.DictReader((DATA_DIR / "hesa_city_demand.csv").open(encoding="utf-8-sig"))}
    sc = json.loads((DATA_DIR / "studentcrowd_properties.json").read_text(encoding="utf-8"))
    by_city = defaultdict(list)
    for p in sc:
        c = (p.get("city") or "").strip().title()
        by_city[CITY_ALIASES.get(c, c)].append(p)

    def wavg(ps):
        pairs = [(_f(p.get("overall_rating")), _f(p.get("review_count"))) for p in ps]
        pairs = [(r, n) for r, n in pairs if r and n]
        return round(sum(r * n for r, n in pairs) / sum(n for _, n in pairs), 2) if pairs else ""

    rows, league = [], []
    for city in sorted(set(demand) | set(by_city)):
        ps, d = by_city.get(city, []), demand.get(city, {})
        students = int(next((v for k, v in d.items() if k.startswith("students_")), 0) or 0)
        if not ps and not students:
            continue
        types = Counter(p.get("hall_type") or "unknown" for p in ps)
        typed = sum(v for k, v in types.items() if k != "unknown")
        nominated = types["part_nominated"] + types["fully_nominated"]
        prices = [x for x in (_f(p.get("price_from_gbp_week")) for p in ps) if x]
        ops = Counter(p.get("operator") or "" for p in ps)
        rows.append({
            "city": city, "students_hesa": students, "russell_group_share": d.get("russell_group_share", ""),
            "halls_studentcrowd": len(ps), "students_per_hall": round(students / len(ps)) if ps and students else "",
            "private": types["private"], "part_nominated": types["part_nominated"], "fully_nominated": types["fully_nominated"],
            "university_run": types["university"], "hall_type_unknown": types["unknown"],
            "nominated_share_of_typed": round(nominated / typed, 2) if typed else "",
            "avg_rating_weighted": wavg(ps), "median_price_from_pw": statistics.median(prices) if prices else "",
            "median_studio_pw_scraped": d.get("median_studio_pw_40w_plus", ""),
            "n_operators": len([o for o in ops if o]), "top_operators": ", ".join(f"{o} ({n})" for o, n in ops.most_common(5) if o),
        })
        for op, n in ops.items():
            if not op:
                continue
            ops_ps = [p for p in ps if p.get("operator") == op]
            pr = [x for x in (_f(p.get("price_from_gbp_week")) for p in ops_ps) if x]
            league.append({"city": city, "operator": op, "provider": ops_ps[0].get("provider", ""), "halls": n,
                           "reviews": int(sum(_f(p.get("review_count")) or 0 for p in ops_ps)),
                           "rating_weighted": wavg(ops_ps), "median_price_from_pw": statistics.median(pr) if pr else ""})
    rows.sort(key=lambda r: -r["students_hesa"])
    league.sort(key=lambda r: (r["city"], -r["halls"]))
    for name, data in (("city_report", rows), ("city_operators", league)):
        with (DATA_DIR / f"{name}.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0]))
            w.writeheader()
            w.writerows(data)
    print(f"{len(rows)} cities, {len(league)} city x operator rows")
    print(f"{'city':<12}{'students':>9}{'RG':>5}{'halls':>6}{'stu/hall':>9}{'nomin%':>7}{'rating':>7}{'from£':>6}{'studio£':>8}")
    for r in rows[:22]:
        print(f"{r['city']:<12}{r['students_hesa']:>9,}{str(r['russell_group_share']):>5}{r['halls_studentcrowd']:>6}"
              f"{str(r['students_per_hall']):>9}{str(r['nominated_share_of_typed']):>7}{str(r['avg_rating_weighted']):>7}"
              f"{str(r['median_price_from_pw']):>6}{str(r['median_studio_pw_scraped']):>8}")


if __name__ == "__main__":
    main()
