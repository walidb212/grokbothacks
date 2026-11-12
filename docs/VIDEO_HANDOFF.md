# Video handoff: "dealt" demo video

You are producing the 2-minute demo video for team aivance's hackathon entry. Everything you need about the product, the look and the story is in this file. Where something is marked **CONFIRM**, ask the team before using it.

---

## 1. The brief

| | |
|---|---|
| Event | Grok Bot Real Estate Hackathon, London, 12 Nov 2026 (V7, 60 Margaret Street) |
| Track | Track 3 · Attribution & provenance: "Tracing every figure back to its source" |
| Deadline | Code freeze and video submission at **23:00** via the submission portal |
| Length | **2:00 maximum** |
| Format | 16:9, 1920×1080, MP4 (H.264), English voice-over, burned-in captions |
| Audience | Judges from Ares Management (real estate investors), MSCI, Cortile, Dwelly, Parallel, V7, Cognee, SpaceXSI |
| Judging criteria | Relevance to a real estate problem · Quality of execution · Effective use of Grok Bot and Cursor · Traceable data · Practical usefulness |

The video must make all five criteria visible. The strongest moment for these judges: **click any number and see where it came from**.

---

## 2. The product in one breath

**dealt** is an AI deal-qualification agent for commercial real estate investors. An analyst drops a sale brochure (or the agent pulls it from their inbox). dealt extracts every claim, checks it against market data and the web, and returns a **traced GO / NO-GO**. Every figure links back to its source: brochure page, MSCI deal, web page, satellite scene. Anything without a source stays grey and is marked as an assumption.

**Tagline:** No number without a source.

**The problem:** Acquisition teams receive hundreds of brochures. Yields, rents, tenants and "5 minutes to campus" claims are taken at face value, copied into models and memos, and nobody can show where a figure came from once it is questioned. Bad deals get through screening; good ones get rejected on numbers nobody checked.

**Focus asset class in the demo:** student housing (PBSA) and redevelopment sites in the UK.

---

## 3. Brand kit

**Name:** `dealt` (always lowercase).

**Logo (use as is):** a lowercase "d" whose round bowl holds one small amber dot: the one fact that has been verified. File: `ui/brand/dealt-icon.svg`.

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="15" fill="#08090e"/>
  <rect x="0.5" y="0.5" width="63" height="63" rx="14.5" fill="none" stroke="#ffffff" stroke-opacity=".08"/>
  <circle cx="28.5" cy="38.5" r="11" fill="none" stroke="#f0eff5" stroke-width="8.5"/>
  <rect x="39.75" y="13" width="8.5" height="38" fill="#f0eff5"/>
  <circle cx="28.5" cy="38.5" r="4.6" fill="#F0A75C"/>
</svg>
```

**Colours**

| Role | Hex |
|---|---|
| Night background | `#07080D` / `#0D0E14` |
| Text | `#F1F1F5` |
| Muted text | `#A6A7B4` |
| Evidence amber (only for verified / sourced / active) | `#F0A75C` |
| Contradicted (red) | `#FF8A80` |
| Aurora glow (background only) | blue `#3B5BFF`, violet `#C03CFF`, pink `#FF4D8D`, orange `#FF8A4C` |

**Type:** Instrument Sans (headlines semibold, tight tracking; body regular) and IBM Plex Mono for sources, labels and numbers (uppercase mono labels with wide letter spacing, e.g. `SRC 01 · BROCHURE · P. 2`).

**Rules:**
- Amber means "verified". Never use it as decoration.
- Unverified things stay grey.
- The aurora glows behind content and never sits on top of numbers.
- Calm, precise, investment-committee tone. No stock "AI sparkles", no robots, no house icons.
- Wherever map or OpenStreetMap data appears: show **"© OpenStreetMap contributors"** (licence requirement).

The look of the live app: dark night sky with a slow aurora rising from the bottom, a centred glass chat bar, white text, amber only on verified figures.

---

## 4. What the user sees (front end)

Live at **https://demo3.ai-vance.de** (hosted on aivance's own server).

1. **Landing screen.** Headline "Which deal are we qualifying today?". A centred chat bar with **+**, microphone and send buttons. Quick actions under it: *Check inbox for deals*, *Qualify a brochure*, *Open Stratford sample*.
2. **+ menu.** *Upload brochure* (PDF; several PDFs are treated as a portfolio) and *Outlook* (connect the inbox). PDFs can also be dropped straight onto the chat bar.
3. **Chat agent (Grok 4.7 via xAI).** Answers in English or German, calls tools instead of guessing:
   - `check_inbox`: lists recent emails that look like real estate deals.
   - `analyze_email`: runs the qualification pipeline on that email's PDF attachments.
   - `load_sample_report`: opens the Stratford sample.

   Each email card has a **Qualify** button.
4. **Outlook connection.** Microsoft sign-in via the company's own Azure app (`Mail.Read`, read-only). Tokens stay on the server, never in the browser.
5. **The traced report** opens as an overlay over the chat:
   - **Verdict** at the top: GO / GO WITH CONDITIONS / NO-GO with reasons.
   - **Evidence coverage bar:** e.g. "9 of 19 figures sourced · 10 assumptions".
   - Sections: Investment returns · Asset facts · Location & demographics · Room types (studio / en-suite / cluster with weekly rate and weeks) · NOI build-up · Rental comps · Sources.
   - Every figure carries a footnote number (`SRC 01`, `SRC 02` …) in amber, or a grey `A` for assumption.
   - **Click any figure** to open the **source chain**: Figure → Value → Source (document + page, MSCI deal, web URL, satellite scene) → Status (Verified / To check / Contradicted / Not checked).
   - A **Sources** list at the bottom, like footnotes in a research note.
6. **Voice analyst (ElevenLabs).** Click the mic and a glowing **voice orb** appears that moves with the live audio (listening vs speaking). The analyst answers about the deal on screen and **drives the UI** while it talks. Its tools:

   | Tool | What happens on screen |
   |---|---|
   | `get_deal_report` | reads every figure with value, source and status |
   | `highlight_figure` | scrolls to the figure and highlights it |
   | `open_source` | opens that figure's source chain |
   | `run_what_if` | recalculates gross income, NOI, yield on cost and the verdict; changes stack |
   | `reset_what_if` | back to base case |
   | `check_inbox` / `qualify_email` | voice-driven inbox triage |
   | `open_sample_report` | opens the sample |
   | `connect_outlook` | starts the Outlook connection |
   | `ask_dealt` | hands a question to the chat agent |

   **Rule it follows:** it names the source for every number it says ("brochure, page 2", "MSCI") and says plainly when a figure is only an assumption. The API key never reaches the browser; the browser gets a short-lived token from our server.
7. **What-if scenarios.** "What if occupancy drops to 90 percent?" makes the NOI, yield on cost and verdict change live. A banner shows the active scenario with "Reset to base case". The scenario rule is shown on screen: GO if yield on cost ≥ exit yield + 100 bps. IRR and equity multiple are not recomputed in scenarios; say so if shown.

---

## 5. What happens in the background

This is the pipeline behind one "Qualify" click, as described by the team's pipeline engineer:

1. **Intake.** The brochure arrives by upload, drag and drop, or from Outlook (Microsoft Graph).
2. **Extraction: V7 Go workflow.** A V7 Go workflow reads the PDF and extracts the claims (price, yield, rents, room mix, tenants, area, location facts), each with its page reference.
3. **Market data: MSCI RCA via MCP.** Real comparable sales, cap rates and deal data come from MSCI's MCP server. The pitch line: "real transactions, not brochure claims". MSCI data is licensed. Show derived figures and deal references, never raw tables.
4. **Web verification: Parallel + custom scraping database.** The team built a large custom scraping database (student-housing operator sites for live room rates, listing portals, reviews) and uses Parallel for cited web research (tenant news, planning, local market).
5. **Real benchmark numbers: partner API.** The team uses a partner's API for the "real numbers" (operating cost, occupancy and service-charge benchmarks). **CONFIRM:** most likely Cortile (the server has a Cortile key), but the engineer could not remember the name. Do not name it until confirmed.
6. **Location checks: open source.**
   - **OSMnx** computes the real walking time from the building to the university on OpenStreetMap streets. It checks the classic brochure claim "5 minutes to campus". Credit "© OpenStreetMap contributors".
   - **pystac-client + Element 84 Earth Search** pulls the latest Sentinel-2 satellite image of the site and stores its scene ID and date as the source. At 10 m resolution it confirms the site exists; it does not show building detail, so do not claim more. Both tools are on Track 3's own resource list.
7. **Memory: Cognee.** dealt remembers past deals, tenants, comps and verdicts. When the same tenant or area shows up again, known facts are reused with their original source and date, so the system gets faster and better with every deal.
8. **Decision and provenance.** Every figure is stored with its source, and the verdict is computed from the checked figures. Unsourced figures are shown as assumptions and cannot make a deal look better.
9. **Packaged as an MCP server.** The whole qualification workflow is exposed as an MCP. It runs in dealt's own UI or plugs straight into **ChatGPT, Grok Bot or Claude**, so analysts can qualify deals from the assistant they already use.
10. **Hosting and build.** Runs on aivance's own server at demo3.ai-vance.de. Built during the evening with **Cursor** and Claude Code; the chat agent runs on **Grok** (xAI). The judges score use of Grok Bot and Cursor, so show both briefly.

Not in the demo: **Dealroom** (access not available tonight). Do not mention it.

---

## 6. Script (2:00)

Voice-over in English, calm and confident. Captions burned in. Numbers below are from the Stratford **sample** report. Keep the "Sample data" label visible when showing them.

| Time | Picture | Voice-over |
|---|---|---|
| 0:00–0:10 | Black, then the dealt logo; the amber dot appears last. Text: "No number without a source." | "Every week, acquisition teams get hundreds of sale brochures. The numbers inside are the seller's pitch." |
| 0:10–0:20 | Quick montage: brochure PDF pages, yields and "5 min to university" circled. | "They get copied into models and memos, and when someone asks where a number came from, nobody can say." |
| 0:20–0:32 | dealt landing screen, aurora background. Type "Check my inbox for new real estate deals." Deal emails appear. Click **Qualify**. | "This is dealt. It reads the deal straight from your inbox, or you drop in a brochure." |
| 0:32–0:50 | Animated pipeline diagram (section 7) while the progress steps tick. | "A V7 workflow extracts every claim with its page. dealt checks them against real MSCI transactions, our own scraped market data and Parallel web research, partner benchmarks, OpenStreetMap walking times and satellite imagery. Cognee remembers every past deal." |
| 0:50–1:05 | Report overlay: verdict at top, evidence coverage bar "9 of 19 figures sourced". | "The result: a GO or NO-GO, and an honest count of how much of it is actually proven." |
| 1:05–1:20 | Click the GIA figure, source chain opens: "Brochure · p. 2". Click IRR: grey, "Assumption · no source". | "Click any number and see where it came from. If there's no source, dealt says so. It stays grey." |
| 1:20–1:40 | Click the mic, the orb glows. Ask: "What if occupancy drops to 90 percent?" NOI and verdict change live; the agent highlights the figures. | "Ask the voice analyst. It answers with sources and changes the scenario live: at 90 percent occupancy, this GO becomes a GO with conditions." |
| 1:40–1:52 | Split screen: dealt UI next to Grok Bot / ChatGPT / Claude logos connecting to the dealt MCP. | "The whole workflow is an MCP server: use it in dealt, or straight from ChatGPT, Grok Bot or Claude." |
| 1:52–2:00 | Logo, tagline, partner strip: Grok · Cursor · V7 Go · MSCI · Parallel · Cognee · ElevenLabs · OpenStreetMap · Element 84. "Built by aivance." | "dealt. No number without a source." |

If 2:00 is tight, shorten 0:10–0:20 first, never the click-to-source moment.

---

## 7. Background diagram (animated, 0:32–0:50)

Left to right, one line per stage, an amber pulse travelling along the line as each stage completes:

```
Brochure / Outlook
      ↓
V7 Go workflow ── extracts claims + page refs
      ↓
Verify ── MSCI RCA (MCP) · Parallel + custom scraping DB · partner benchmark API (CONFIRM name)
          · OSMnx walk time (© OpenStreetMap contributors) · Earth Search satellite scene
      ↓
Cognee memory ── past deals, tenants, comps
      ↓
Traced GO / NO-GO ── every figure → its source
      ↓
MCP server ── dealt UI · ChatGPT · Grok Bot · Claude
```

Style: white thin lines on the night background, IBM Plex Mono labels, amber only for the verified output at the end.

---

## 8. Footage to capture from the live app

Record at 1920×1080 in Chrome, dark mode, bookmarks bar hidden, zoom 100–110 %.

1. Landing screen idle for 3 s (aurora moving).
2. Typing "Check my inbox for new real estate deals" and the reply with deal emails. **Use a demo mailbox or blur sender names.** No private mail on screen.
3. Clicking **Qualify** (or the sample quick action), with the progress steps ticking.
4. The report overlay: slow scroll from verdict to sources.
5. Clicking `GIA` (sourced, brochure p. 2) and `IRR (unlevered)` (assumption) to show the two source chains.
6. Voice: click the mic, ask "Why is this a GO?", then "What if occupancy drops to 90 percent?". Record the system audio as well. Use headphones to avoid echo.
7. 3–5 s of Cursor with the repo open and 3–5 s of the Grok chat, for the "use of Grok Bot and Cursor" criterion.

Sample figures you will see (Stratford, 78-102 Broadway & 7 Salway Place, E15 1NG; most are placeholders marked as assumptions):
- Verdict: GO
- IRR 14.1% (assumption), yield on cost 6.68% (assumption), exit yield 5.25%
- NOI £7.49m pa
- GIA 161,826 sq ft (brochure p. 2), current income £122,500 pa (brochure p. 2)
- What-if at 90% occupancy: yield on cost 6.20%, verdict GO WITH CONDITIONS

---

## 9. Honesty rules for the edit

- Keep the "Sample data" label visible whenever sample figures are on screen. Never present them as a real underwriting.
- Do not claim Dealroom, building-level detail from satellite images, or recomputed IRR in scenarios.
- No API keys, tokens, `.env` files, email addresses of third parties or MSCI raw tables on screen.
- Name the benchmark partner only after the team confirms it (see section 5, step 5).
- If a feature did not work during recording, cut it rather than fake it.

---

## 10. Open questions for the team (ask before final export)

1. Which partner API provides the "real numbers": Cortile? (section 5, step 5)
2. Is the real pipeline live for the recording, or do we show the Stratford sample?
3. Is the MCP server reachable for a live ChatGPT / Grok Bot / Claude shot, or should 1:40–1:52 be a diagram?
4. Voice-over: AI voice (ElevenLabs, credits available) or a team member?
