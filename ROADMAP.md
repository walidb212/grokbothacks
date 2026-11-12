# Deal Qualification Agent: roadmap

From brochure to investment committee: drop a sale brochure, get a traced GO / NO-GO with returns, comps and the plan to run the asset.

Code freeze 23:00 · video recorded at 22:30. **MVP** = must work for the demo. **Stretch** = only if MVP is done.

## Phase 1: Input (drag and drop UI)
- **MVP**: drag and drop a brochure PDF (or several = portfolio) onto the dashboard.
- **MVP**: one card per asset once parsed.
- **Stretch**: voice comments (analyst records a note, e.g. "assume 51-week lets", transcribed and passed to the agent as an assumption).

## Phase 2: Analysis (agent)
### Common fields to extract from every brochure (each with page reference)
| Field | Example (Savills, 78-102 Broadway, Stratford) |
|---|---|
| Asset name, address, postcode | 78-102 Broadway & 7 Salway Place, E15 1NG |
| Tenure | Freehold |
| Site area / GIA / NSA | 1.56 acres · 161,826 sqft GIA · 18,699 sqft NSA |
| Current income (passing rent) | £122,500 pa (Poundstretcher) |
| ERV if fully let | £1.4m pa + £900k pa |
| Tenants, lease terms | Poundstretcher Ltd |
| Asking price / quoting yield | (if stated) |
| Use / redevelopment angle | c.800-bed PBSA or c.325-unit BTR |
| Location facts | Stratford station 400m, UEL, UCL East, Unite Students nearby |
| Demographics | 375,000 pop (10 min drive), +6.8% growth, 73.6% economically active |

### Tools
| Tool | Job |
|---|---|
| V7 Go / Grok Bot | Extract the fields above with page citations |
| MSCI RCA (MCP) | Peer Finder comps, cap rates (TrendTracker), recent deals |
| Parallel | Tenant health, local news, planning, interest rates, with sources |
| Team scraper | PBSA room-rate comps (operators' websites) |
| Cortile | OPEX benchmarks (service charges, maintenance) vs similar buildings |
| cognee | Memory graph: asset, tenants, comps, verdicts across the portfolio |
| Supabase | Every figure stored with its source (doc+page, MSCI deal, URL) |

## Phase 3: Output (investment report, per asset)
Order requested by the team's REIT FD:

1. **Investment returns + verdict** (top): GO / NO-GO, IRR, equity multiple, entry yield vs exit yield (delta).
2. **Location fundamentals + demographics**: population, growth, universities, transport; supply and demand in the local vicinity.
3. **Room-type analysis (PBSA)**: studios / en-suites / cluster rooms; target audience (e.g. international students, China and India, favour studios and can afford them).
4. **NOI build-up**
   - Rental comps: **5 comparable assets** near the anchor, per room type: weekly rate, number of weeks (e.g. 44 / 51), building name and operator.
   - All expenses (OPEX), benchmarked (Cortile).
   - NOI = gross rent × occupancy minus OPEX.
5. **Returns**: IRR, equity multiple, entry vs exit yield, using financing costs from **SONIA** (UK) / **Euribor** (EU) and swap rates.
6. **Portfolio**: if several brochures, the whole report is done **asset by asset**, then rolled up.

Every number in the report is clickable to its source.

## Demo flow (2 min video)
1. Drop the Stratford brochure.
2. Fields appear with page links.
3. Comps + rates + returns build up.
4. Verdict at the top: GO / NO-GO and why.

## Data notes
- Good demo brochure: Savills 78-102 Broadway, Stratford (PBSA / BTR redevelopment, rich location data).
- Strutt & Parker folder = mostly country houses and estates: not useful for commercial comps.
- MSCI / Cortile data and keys never go in the repo (.gitignore `data/` and `.env`).
