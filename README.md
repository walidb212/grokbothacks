# dealt

Drop a sale brochure, get a traced GO / NO-GO: returns, location, room-type comps, NOI build-up. Every figure links back to its source (brochure page, MSCI deal, web page).

See `ROADMAP.md` for the plan and `schema.html` for the architecture.

## API contract
- `POST /analyze` (multipart: `files[]`, `comment`) -> `{ "job_id": "..." }`
- `GET /analysis/{job_id}` -> `{ "status": "running|done|error", "steps": [...], "report": {...} }`

## MCP server (Claude, Grok, ChatGPT)
`api/mcp_app.py`, mounted by the API: `https://demo3.ai-vance.de/api/mcp` (streamable HTTP, no auth). Local stdio: `python api/mcp_app.py`.
Tools: `get_room_comps`, `get_city_demand`, `build_noi`, `compute_returns`, `deal_verdict`, `ask_deal_memory`, `get_sample_report`.
The UI's "Connect to AI" dialog shows the URL and the setup steps for each client.

## Comps and photos
`engine/market.comps()` = the 5 nearest scraped PBSA buildings (academic-year lets of 38+ weeks, one academic year per building).
Photos: `market/property_images.json` (property page URL -> image URL), built from the operators' pages; no Yugo / CRM photos yet.

## Figure format (used everywhere)
```json
{ "metric": "studio_weekly_rate", "label": "Studio rate", "value": 289, "unit": "GBP/week",
  "status": "green|amber|red|null",
  "source": { "type": "brochure|msci|web|cortile|model|assumption", "ref": "url, file or formula", "page": 3 } }
```

Sample response for UI work: `mock/report.json` (Stratford, values marked `assumption` are placeholders).

## Rules
- Never commit `.env`, keys, or MSCI / Cortile data (`data/` is gitignored).
