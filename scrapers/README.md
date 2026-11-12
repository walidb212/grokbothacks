# grokbothacks · UK PBSA competitor data

Grok Bot Real Estate Hackathon, London 12/11/2026. Competitor dataset for UK purpose-built student accommodation.

## Data (`data/`)
| File | What |
|---|---|
| `competitors.csv` | 13.8k rows, one per room type x tenancy x academic year: price/week, tenancy weeks, year, size sqm, deposit, offer, beds left, amenity flags, location. Operators: iq, unite, student_roost, yugo, prestige, fresh, vita, crm |
| `studio_benchmark.csv` | Studio price/size per city x operator |
| `studentcrowd_properties.csv` / `_providers.csv` | 3,203 UK halls (all operators + universities): ratings, review counts, hall type (private / part/fully nominated / university), price from |
| `hesa_city_demand.csv` / `hesa_provider_city.csv` | HESA 2024/25 enrolments per city, Russell Group share, provider-to-city mapping |
| `city_report.csv` / `city_operators.csv` | City view: demand vs halls, nomination share, ratings, prices; operator league per city |
| `city_beds.csv` | PBSA bed stock, pipeline and student-to-bed ratio for 20 Russell Group cities, researched with Parallel, with source + year + confidence |
| `parallel_check.csv` | Sample of scraped rows cross-checked with Parallel (verdict + citations) |

## Run
```
python -m scrapers.<operator> [--limit N]     # iq unite student_roost yugo prestige fresh vita crm studentcrowd
python -m scrapers.merge                      # -> competitors.csv, studio_benchmark.csv
python -m scrapers.hesa_demand && python -m scrapers.city_report
PARALLEL_API_KEY=... python -m scrapers.parallel_check | parallel_beds
```

## Caveats
- Homes for Students blocks automated access: covered only via StudentCrowd.
- CRM is a Yugo brand: duplicated rows dropped, Yugo rows flagged `crm_brand`.
- Our students-per-bed uses all HESA students (incl. part-time), higher than consultancy ratios (full-time). Some bed figures are old (Manchester 2018/19, Sheffield 2021/22): check `figure_year`.

Sources: operator websites, StudentCrowd, HESA (CC BY 4.0, "Source: HESA"), Parallel Web Systems research with cited sources.
