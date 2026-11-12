# Deal Qualification Agent

Drop a sale brochure, get a traced GO / NO-GO: returns, location, room-type comps, NOI build-up. Every figure links back to its source (brochure page, MSCI deal, web page).

See `ROADMAP.md` for the plan and `schema.html` for the architecture.

## API contract
- `POST /analyze` (multipart: `files[]`, `comment`) -> `{ "job_id": "..." }`
- `GET /analysis/{job_id}` -> `{ "status": "running|done|error", "steps": [...], "report": {...} }`

## Figure format (used everywhere)
```json
{ "metric": "studio_weekly_rate", "label": "Studio rate", "value": 289, "unit": "GBP/week",
  "status": "green|amber|red|null",
  "source": { "type": "brochure|msci|web|cortile|assumption", "ref": "url or file", "page": 3 } }
```

Sample response for UI work: `mock/report.json` (Stratford, values marked `assumption` are placeholders).

## Rules
- Never commit `.env`, keys, or MSCI / Cortile data (`data/` is gitignored).
