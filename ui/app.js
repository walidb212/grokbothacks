// dealt UI + ElevenLabs voice analyst.
// No build step: vanilla ES modules, ElevenLabs client vendored in ./vendor.
import { Conversation } from "./vendor/elevenlabs-client.js";

// ---------- config ----------
const params = new URLSearchParams(location.search);
// Same origin in production (Caddy routes /api/* to the API). Local dev: ?api=http://localhost:8000
const API = (params.get("api") || "/api").replace(/\/$/, "");
// Optional for local tests with a PUBLIC agent: ?agent=agent_xxx (production uses signed tokens)
const PUBLIC_AGENT_ID = params.get("agent");

// ---------- state ----------
const state = {
  report: null,       // report object from the API ({assets: [...], mock})
  baseReport: null,   // untouched copy for what-if resets
  jobId: null,
  figures: new Map(), // metric -> { fig, assetIndex, section }
  sources: [],        // per render: [{key, type, ref, page}]
  selected: null,
  scenario: null,     // {changes, result}
};
let conversation = null;
let meterRaf = null;

const $ = (id) => document.getElementById(id);

// ---------- formatting ----------
const fmtNum = (n, d = 0) => Number(n).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
function fmtMoney(v) {
  const a = Math.abs(v);
  if (a >= 1e6) return `£${fmtNum(v / 1e6, a >= 1e7 ? 1 : 2)}m`;
  if (a >= 1e3) return `£${fmtNum(v / 1e3, 0)}k`;
  return `£${fmtNum(v, 0)}`;
}
function formatValue(f) {
  const v = f.value;
  if (v === null || v === undefined || v === "") return "n/a";
  if (typeof v !== "number") return String(v);
  switch (f.unit) {
    case "%": return `${fmtNum(v * 100, 1)}%`;
    case "x": return `${fmtNum(v, 2)}x`;
    case "GBP/yr": return `${fmtMoney(v)} pa`;
    case "GBP": return fmtMoney(v);
    case "GBP/week": return `£${fmtNum(v, 0)} /wk`;
    case "sqft": return `${fmtNum(v, 0)} sq ft`;
    case "m": return `${fmtNum(v, 0)} m`;
    case "acres": return `${fmtNum(v, 2)} acres`;
    case "people": return fmtNum(v, 0);
    case "beds": return `${fmtNum(v, 0)} beds`;
    default: return f.unit ? `${fmtNum(v, 2)} ${f.unit}` : fmtNum(v, 2);
  }
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hasSource = (f) => f?.source && f.source.type && f.source.type !== "assumption";
function sourceText(src) {
  if (!src || !src.type || src.type === "assumption") return "Assumption · no source";
  const type = { brochure: "Brochure", msci: "MSCI RCA", web: "Web", cortile: "Cortile", model: "Calculated" }[src.type] || src.type;
  const ref = src.ref ? ` · ${src.ref}` : "";
  const page = src.page ? ` · p. ${src.page}` : "";
  return `${type}${ref}${page}`;
}
const statusTag = (s) => s === "green" ? `<span class="tag verified">Verified</span>`
  : s === "red" ? `<span class="tag contradicted">Contradicted</span>` : "";

// ---------- figures registry ----------
function collectFigures(asset) {
  const out = [];
  for (const sec of ["returns", "facts", "location", "noi", "rates", "comps"]) {
    for (const f of asset[sec] || []) if (f && f.metric) out.push({ fig: f, section: sec });
  }
  for (const rt of asset.room_types || []) if (rt.weekly_rate?.metric) out.push({ fig: rt.weekly_rate, section: "room_types", room: rt });
  (asset.comps || []).forEach((c, i) => { if (c && !c.metric) for (const f of compFigures(c, i)) out.push({ fig: f, section: "comps" }); });
  return out;
}

// Engine comps ({operator, property_name, rooms, url, image}) -> sourced figures, one per room category
const OPERATORS = { unite: "Unite Students", iq: "iQ Student", fresh: "Fresh", prestige: "Prestige Student Living", student_roost: "Student Roost", yugo: "Yugo", vita: "Vita Student", crm: "CRM Students" };
const ROOM_LABELS = { studio: "Studio", ensuite: "En-suite", non_ensuite: "Cluster", apartment: "Apartment", twodio: "Twodio", other: "Other" };
function compFigures(c, i) {
  return Object.entries(c.rooms || {}).filter(([cat]) => cat !== "other").map(([cat, r]) => ({
    metric: `comp${i}_${cat}`, label: `${ROOM_LABELS[cat] || cat} at ${c.property_name}`, value: r.median_pw, unit: "GBP/week", status: null,
    source: { type: "web", ref: c.url, page: null, operator: OPERATORS[c.operator] || c.operator,
              note: `${c.academic_year || ""} rate${r.weeks ? `, ${r.weeks}-week tenancy` : ""}${r.size_sqm ? `, ${r.size_sqm} m²` : ""}, scraped from the operator's website` },
    room: { cat, ...r }, comp: c,
  }));
}

// ---------- render ----------
const SECTION_TITLES = { returns: "Investment returns", facts: "Asset facts", location: "Location & demographics", noi: "NOI build-up", rates: "Rates", comps: "Rental comps" };

function sourceIndex(src) {
  if (!hasSource({ source: src })) return null;
  const key = `${src.type}|${src.ref || ""}|${src.page || ""}`;
  let i = state.sources.findIndex((s) => s.key === key);
  if (i === -1) { state.sources.push({ key, ...src }); i = state.sources.length - 1; }
  return i + 1;
}

function figButton(f, extra = "") {
  const sourced = hasSource(f);
  const n = sourceIndex(f.source);
  const ref = sourced ? `<sup class="ref">${String(n).padStart(2, "0")}</sup>` : `<sup class="ref none">A</sup>`;
  const changed = state.scenario?.result?.changed?.[f.metric];
  return `<button type="button" class="fig ${sourced ? "sourced" : ""} ${extra}" data-metric="${esc(f.metric)}" data-status="${esc(f.status || "")}">
    <span class="lbl">${esc(f.label || f.metric)}</span>
    <span class="val">${esc(formatValue(changed ? { ...f, value: changed.to } : f))}${ref}</span>
    ${changed ? `<span class="changed">Scenario · was ${esc(formatValue({ ...f, value: changed.from }))}</span>` : ""}
    <span class="src">${esc(sourceText(f.source))}</span>
  </button>`;
}

const fmtDist = (km) => (km >= 1 ? `${fmtNum(km, 1)} km` : `${fmtNum(km * 1000)} m`);

// ---------- investment memo: prose where every number links to its source ----------
function cite(metric, text) {
  const e = state.figures.get(metric);
  if (!e) return null;
  const f = e.fig;
  return `<button type="button" class="cite ${hasSource(f) ? "" : "assumed"}" data-metric="${esc(metric)}">${esc(text ?? formatValue(f))}</button>`;
}
function memoHtml(a) {
  const c = (m, t) => cite(m, t);
  const s = (...parts) => (parts.some((p) => p === null) ? "" : parts.join(""));
  const comps = (a.comps || []).filter((x) => x && !x.metric);
  const far = comps.length ? Math.max(...comps.map((x) => x.distance_km || 0)) : null;
  const near = comps[0];
  const nearFig = near && ["studio", "ensuite", "non_ensuite"].map((k) => `comp0_${k}`).find((m) => state.figures.has(m));
  const decision = a.verdict?.decision || "—";
  const spb = state.figures.get("students_per_bed")?.fig.value;
  const paras = [
    s(`We recommend <strong class="memo-d" data-d="${esc(decision)}">${esc(decision)}</strong> on ${esc(a.name || "this asset")}. `,
      `The scheme returns an unlevered IRR of `, c("irr"), ` and a `, c("equity_multiple"), ` equity multiple. `) +
      s(`Yield on cost of `, c("entry_yield") ?? c("yield_on_cost"), ` beats the market exit yield of `, c("exit_yield"), c("yield_delta") ? s(` by `, c("yield_delta")) : "", `.`),
    s(`The site is `, c("station_distance"), ` from Stratford station, with `, c("population_10min"), ` people within a 10-minute drive, growing `, c("population_growth"), `. `) +
      s(`London has `, c("students", fmtNum(state.figures.get("students")?.fig.value ?? 0)), ` students, `, c("russell_group_share"), ` of them at Russell Group universities, for only `, c("pbsa_beds"), ` in purpose-built halls: `, c("students_per_bed", `${fmtNum(spb ?? 0, 1)} students per bed`), `.`),
    comps.length ? s(`Rents come from the ${comps.length} nearest operator buildings, all within ${fmtNum(far, 1)} km. `,
      `They put studios at `, c("studio_rate"), ` and en-suites at `, c("ensuite_rate"), `. `) +
      (nearFig ? s(`The closest, ${esc(near.property_name)} (${esc(OPERATORS[near.operator] || near.operator)}, ${fmtDist(near.distance_km)} away), lets at `, c(nearFig), `.`) : "") : "",
    s(`On `, c("pbsa_capacity"), `, gross income is `, c("gross_income"), `, OPEX `, c("opex"), ` and NOI `, c("noi"), `.`),
  ].filter(Boolean);
  if (!paras.length) return "";
  return `<section class="section card memo" data-section="memo">
    <div class="section-head"><h2>Investment memo</h2><span class="mono-label">Click any number for its source</span></div>
    ${paras.map((p) => `<p>${p}</p>`).join("")}
  </section>`;
}
function compsHtml(a) {
  const comps = (a.comps || []).filter((c) => c && !c.metric);
  if (!comps.length) return `<p class="muted" style="margin:0">No comps yet. The comp scraper fills this section with sourced operator rates.</p>`;
  return `<div class="comps">${comps.map((c, i) => {
    const figs = compFigures(c, i);
    const op = OPERATORS[c.operator] || c.operator;
    const img = c.image ? encodeURI(decodeURI(c.image)) : "";
    return `<article class="comp">
      <a class="comp-img" href="${esc(c.url)}" target="_blank" rel="noopener">${img ? `<img src="${esc(img)}" alt="${esc(c.property_name)}" loading="lazy" onerror="this.remove()">` : ""}<span class="comp-dist">${fmtDist(c.distance_km)}</span></a>
      <div class="comp-body">
        <div class="comp-op">${opBadge(c.operator)}${esc(op)}</div>
        <h3>${esc(c.property_name)}</h3>
        <p class="comp-meta">${esc([c.postcode, c.academic_year].filter(Boolean).join(" · "))}</p>
        <ul class="comp-rooms">${figs.map((f) => `<li><span>${esc(ROOM_LABELS[f.room.cat] || f.room.cat)}</span>${cite(f.metric, `£${fmtNum(f.value)}/wk`)}<small>${esc([f.room.weeks && `${f.room.weeks} wks`, f.room.size_sqm && `${f.room.size_sqm} m²`].filter(Boolean).join(" · "))}</small></li>`).join("")}</ul>
      </div>
    </article>`;
  }).join("")}</div>`;
}

// ---------- source popover ----------
const hostOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return null; } };
const OP_COLORS = { unite: "#0e2a47", iq: "#00a19a", fresh: "#e5007e", prestige: "#5b2333", student_roost: "#f05a28", yugo: "#1d1d1b", vita: "#c8102e", crm: "#2b2b8f" };
const badge = (text, color = "#111216") => `<span class="sb" style="background:${color};color:#fff;border-color:${color}">${esc(text)}</span>`;
const opBadge = (op) => badge((OPERATORS[op] || op || "?").replace(/[^A-Za-z]/g, "")[0].toUpperCase(), OP_COLORS[op]);
function sourceBrand(src) {
  const t = src?.type || "assumption", ref = String(src?.ref || "");
  if (t === "brochure") return { name: "Sale brochure", logo: `<span class="sb sb-pdf">PDF</span>`, detail: `${ref}${src.page ? ` · page ${src.page}` : ""}` };
  if (t === "msci") return { name: "MSCI Real Capital Analytics", logo: `<span class="sb sb-msci">MSCI</span>`, detail: ref };
  if (t === "cortile") return { name: "Cortile", logo: `<span class="sb sb-cortile">C</span>`, detail: ref };
  if (t === "model") return { name: "Calculated by dealt", logo: `<img src="brand/dealt-icon.svg" alt="">`, detail: ref };
  if (t === "assumption") return { name: "Assumption", logo: `<span class="sb sb-a">A</span>`, detail: "No source yet. To verify before an offer." };
  if (/^HESA$/i.test(ref)) return { name: "HESA", logo: badge("HE", "#5b2c83"), detail: "Higher Education Student Statistics 2024/25", url: "https://www.hesa.ac.uk/data-and-analysis/students" };
  const host = hostOf(ref);
  const op = Object.keys(OPERATORS).find((k) => OPERATORS[k] === src.operator);
  if (host) return { name: src.operator || host, logo: op ? opBadge(op) : /\.pdf($|\?)/i.test(ref) ? `<span class="sb sb-pdf">PDF</span>` : badge(host[0].toUpperCase(), "#3a3b42"), detail: host, url: ref };
  return { name: "Nearby comps", logo: `<span class="sb sb-web">W</span>`, detail: ref };
}
function showCite(el, metric) {
  const e = state.figures.get(metric);
  if (!e) return;
  const f = e.fig, b = sourceBrand(f.source);
  const pop = $("cite-pop");
  pop.innerHTML = `
    <div class="cp-head">${b.logo}<div><strong>${esc(b.name)}</strong><small>${esc(b.detail || "")}</small></div></div>
    <div class="cp-fig"><span>${esc(f.label || f.metric)}</span><b>${esc(formatValue(f))}</b></div>
    ${f.source?.note ? `<p class="cp-note">${esc(f.source.note)}</p>` : ""}
    <div class="cp-foot">${statusTag(f.status)}${b.url ? `<a href="${esc(b.url)}" target="_blank" rel="noopener">Open source ↗</a>` : ""}</div>`;
  pop.hidden = false;
  const r = el.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
  const left = Math.max(12, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 12));
  const top = r.bottom + 8 + h > innerHeight ? r.top - h - 8 : r.bottom + 8;
  pop.style.left = `${left}px`; pop.style.top = `${top}px`;
}
function hideCite() { const p = $("cite-pop"); if (p) p.hidden = true; }
document.addEventListener("click", (e) => { if (!e.target.closest("#cite-pop, [data-metric]")) hideCite(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideCite(); });
document.addEventListener("scroll", (e) => { if (!e.target.closest?.("#cite-pop")) hideCite(); }, true);

function renderReport() {
  const host = $("report");
  state.figures.clear();
  state.sources = [];
  const assets = state.report?.assets || [];
  if (!assets.length) { host.innerHTML = `<div class="empty card"><p class="mono-label">Report</p><p>No assets in this report.</p></div>`; return; }

  host.innerHTML = assets.map((a, ai) => {
    const all = collectFigures(a);
    all.forEach(({ fig, section, room }) => state.figures.set(fig.metric, { fig, assetIndex: ai, section, room }));
    const sourcedCount = all.filter(({ fig }) => hasSource(fig)).length;
    const pct = all.length ? Math.round((sourcedCount / all.length) * 100) : 0;
    const decision = state.scenario?.result?.decision || a.verdict?.decision || "—";

    const sections = ["returns", "facts", "location"].filter((s) => (a[s] || []).length).map((s) => `
      <section class="section card" data-section="${s}">
        <div class="section-head"><h2>${SECTION_TITLES[s]}</h2><span class="mono-label">${(a[s] || []).length} figures</span></div>
        <div class="figs">${a[s].map((f) => figButton(f)).join("")}</div>
      </section>`).join("");

    const rooms = (a.room_types || []).length ? `
      <section class="section card" data-section="room_types">
        <div class="section-head"><h2>Room types</h2><span class="mono-label">PBSA mix</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Type</th><th>Beds</th><th>Weekly rate</th><th>Weeks</th><th>Source</th></tr></thead>
          <tbody>${a.room_types.map((rt) => {
            const f = rt.weekly_rate || {};
            const n = sourceIndex(f.source);
            const changed = state.scenario?.result?.changed?.[f.metric];
            const val = formatValue(changed ? { ...f, value: changed.to } : f);
            return `<tr>
              <td>${esc(rt.type)}</td><td class="num">${fmtNum(rt.beds)}</td>
              <td class="num"><button type="button" class="fig-inline" data-metric="${esc(f.metric)}">${esc(val)}<sup class="ref ${hasSource(f) ? "" : "none"}">${hasSource(f) ? String(n).padStart(2, "0") : "A"}</sup></button></td>
              <td class="num">${esc(rt.weeks ?? "—")}</td>
              <td class="muted">${esc(sourceText(f.source))}</td></tr>`;
          }).join("")}</tbody>
        </table></div>
      </section>` : "";

    const noi = (a.noi || []).length ? `
      <section class="section card" data-section="noi">
        <div class="section-head"><h2>NOI build-up</h2><span class="mono-label">Gross × occupancy − OPEX</span></div>
        <div class="figs">${a.noi.map((f) => figButton(f)).join("")}</div>
      </section>` : "";

    const comps = `
      <section class="section card" data-section="comps">
        <div class="section-head"><h2>Rental comps</h2><span class="mono-label">${(a.comps || []).length} nearest buildings · operator websites</span></div>
        ${compsHtml(a)}
      </section>`;

    const sourcesList = `
      <section class="section card">
        <div class="section-head"><h2>Sources</h2><span class="mono-label">${state.sources.length} documents</span></div>
        <ol class="sources">
          ${state.sources.map((s, i) => `<li><span class="n">SRC ${String(i + 1).padStart(2, "0")}</span><span>${esc(sourceText(s))}</span></li>`).join("")}
          <li><span class="n none">A</span><span class="muted">Assumption: no source yet, to be verified</span></li>
        </ol>
      </section>`;

    const scenario = state.scenario ? `
      <div class="scenario-note"><span>Scenario active · ${esc(describeChanges(state.scenario.changes))}</span>
      <button type="button" id="reset-scenario">Reset to base case</button></div>` : "";

    return `<article class="asset" data-asset="${ai}">
      ${state.report.mock && all.length > sourcedCount ? `<p class="mock-note">Sample data: figures marked A are placeholders until the pipeline verifies them.</p>` : ""}
      <div class="card verdict">
        <div><p class="mono-label">Verdict${state.scenario ? " · scenario" : ""}</p><div class="decision${decision.length > 6 ? " long" : ""}" data-d="${esc(decision)}">${esc(decision)}</div></div>
        <div class="verdict-side">
          <div class="asset-head"><h2>${esc(a.name || "Asset")}</h2><p class="addr">${esc(a.address || "")}</p></div>
          ${(a.verdict?.reasons || []).length ? `<ul class="reasons">${a.verdict.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
          ${state.scenario?.result?.note ? `<p class="muted" style="margin:0">${esc(state.scenario.result.note)}</p>` : ""}
          <div class="coverage">
            <div class="coverage-bar" role="img" aria-label="${pct}% of figures sourced"><span style="width:${pct}%"></span></div>
            <span class="coverage-text">Evidence coverage · ${sourcedCount} of ${all.length} figures sourced · ${all.length - sourcedCount} assumptions</span>
          </div>
        </div>
      </div>
      ${scenario}
      ${memoHtml(a)}${comps}${sections}${rooms}${noi}${sourcesList}
    </article>`;
  }).join("");

  host.querySelectorAll("[data-metric]").forEach((el) => el.addEventListener("click", (ev) => { ev.stopPropagation(); selectFigure(el.dataset.metric, { fromUser: true }); showCite(el, el.dataset.metric); }));
  $("reset-scenario")?.addEventListener("click", () => { resetWhatIf(); notifyAgent("The user reset the scenario to the base case."); });
  if (state.selected) markSelected(state.selected);
}

// ---------- source inspector ----------
function selectFigure(metric, { fromUser = false, scroll = false } = {}) {
  const entry = state.figures.get(metric);
  if (!entry) return null;
  state.selected = metric;
  markSelected(metric);
  const f = entry.fig;
  const src = f.source || {};
  const sourced = hasSource(f);
  const n = sourced ? sourceIndex(src) : null;
  const changed = state.scenario?.result?.changed?.[metric];
  $("inspector-body").className = "";
  $("inspector-body").innerHTML = `
    <ol class="chain">
      <li><span class="k">Figure</span><span class="v">${esc(f.label || f.metric)}</span></li>
      <li><span class="k">Value</span><span class="v big">${esc(formatValue(changed ? { ...f, value: changed.to } : f))}</span></li>
      ${changed ? `<li><span class="k">Base case</span><span class="v mono">${esc(formatValue({ ...f, value: changed.from }))} · changed by scenario</span></li>` : ""}
      <li><span class="k">Source</span><span class="v mono ${sourced ? "amber" : ""}">${sourced ? `SRC ${String(n).padStart(2, "0")} · ` : ""}${esc(sourceText(src))}</span></li>
      <li><span class="k">Status</span><span class="v">${statusTag(f.status)}</span></li>
      <li><span class="k">Section</span><span class="v mono">${esc(SECTION_TITLES[entry.section] || "Room types")}</span></li>
      ${sourced ? "" : `<li><span class="k">Note</span><span class="v">No source yet. This value is an assumption and must be verified before an offer.</span></li>`}
    </ol>`;
  if (scroll) flashFigure(metric);
  if (fromUser) notifyAgent(`The user is looking at the figure "${f.label}" (metric ${metric}). Consider it if they ask about "this" figure.`);
  if (fromUser && window.matchMedia("(max-width: 1040px)").matches) $("inspector").scrollIntoView({ behavior: "smooth", block: "nearest" });
  return { metric, label: f.label, value: f.value, unit: f.unit, display: formatValue(f), source: sourceText(src), source_type: src.type || "assumption", page: src.page ?? null, status: f.status ?? "not_checked" };
}
function markSelected(metric) {
  document.querySelectorAll(".fig.is-selected").forEach((e) => e.classList.remove("is-selected"));
  document.querySelectorAll(`.fig[data-metric="${CSS.escape(metric)}"]`).forEach((e) => e.classList.add("is-selected"));
}
function flashFigure(metric) {
  const els = document.querySelectorAll(`[data-metric="${CSS.escape(metric)}"]`);
  if (!els.length) return false;
  els[0].scrollIntoView({ behavior: "smooth", block: "center" });
  els.forEach((e) => { e.classList.add("is-highlighted"); setTimeout(() => e.classList.remove("is-highlighted"), 4000); });
  return true;
}

// ---------- what-if (client-side, transparent rule) ----------
// Base model derived from the report: gross = Σ beds × rate × weeks × occupancy; OPEX = ratio × gross; NOI = gross − OPEX.
// Total cost = base NOI / base yield on cost. Verdict rule (demo): GO if YoC ≥ exit yield + 100 bps; 50–100 bps = GO WITH CONDITIONS.
function baseModel(asset) {
  const get = (sec, m) => (asset[sec] || []).find((f) => f.metric === m)?.value;
  const rooms = asset.room_types || [];
  const gross = get("noi", "gross_income"), opex = get("noi", "opex"), noi = get("noi", "noi");
  const yocMetric = (asset.returns || []).some((f) => f.metric === "entry_yield") ? "entry_yield" : "yield_on_cost";  // engine vs older reports
  const yoc = get("returns", yocMetric), exit = get("returns", "exit_yield");
  if (!rooms.length || !gross || !noi || !yoc) return null;
  const potential = rooms.reduce((s, r) => s + r.beds * (r.weekly_rate?.value || 0) * (r.weeks || 51), 0);
  return { rooms, occupancy: gross / potential, opexRatio: opex / gross, totalCost: noi / yoc, exit, gross, opex, noi, yoc, yocMetric };
}
function decide(yoc, exit) {
  if (!exit) return "GO";
  const spread = yoc - exit;
  return spread >= 0.01 ? "GO" : spread >= 0.005 ? "GO WITH CONDITIONS" : "NO-GO";
}
function describeChanges(c) {
  const parts = [];
  if (c.occupancy != null) parts.push(`occupancy ${fmtNum(c.occupancy * 100, 1)}%`);
  for (const k of ["studio_rate", "ensuite_rate", "cluster_rate"]) if (c[k] != null) parts.push(`${k.replace("_rate", "")} £${fmtNum(c[k])}/wk`);
  if (c.weeks != null) parts.push(`${c.weeks}-week lets`);
  if (c.opex_ratio != null) parts.push(`OPEX ${fmtNum(c.opex_ratio * 100, 1)}% of gross`);
  if (c.exit_yield != null) parts.push(`exit yield ${fmtNum(c.exit_yield * 100, 2)}%`);
  return parts.join(" · ") || "no changes";
}
function normalisePct(v) { if (v == null || v === "") return null; const n = Number(v); if (!isFinite(n)) return null; return n > 1 ? n / 100 : n; }
function runWhatIf(input = {}) {
  let raw = input || {};
  const asset = state.baseReport?.assets?.[0];
  const m = asset && baseModel(asset);
  if (!m) return { ok: false, error: "Scenario not available: this report has no room mix and NOI build-up." };
  // Changes stack on the active scenario until reset_what_if.
  raw = { ...(state.scenario?.changes || {}), ...Object.fromEntries(Object.entries(raw).filter(([, v]) => v !== null && v !== undefined && v !== "")) };
  const c = {
    occupancy: normalisePct(raw.occupancy), opex_ratio: normalisePct(raw.opex_ratio), exit_yield: normalisePct(raw.exit_yield),
    weeks: raw.weeks != null && raw.weeks !== "" ? Number(raw.weeks) : null,
    studio_rate: raw.studio_rate != null && raw.studio_rate !== "" ? Number(raw.studio_rate) : null,
    ensuite_rate: raw.ensuite_rate != null && raw.ensuite_rate !== "" ? Number(raw.ensuite_rate) : null,
    cluster_rate: raw.cluster_rate != null && raw.cluster_rate !== "" ? Number(raw.cluster_rate) : null,
  };
  Object.keys(c).forEach((k) => c[k] == null && delete c[k]);
  const occ = c.occupancy ?? m.occupancy;
  const opexRatio = c.opex_ratio ?? m.opexRatio;
  const exit = c.exit_yield ?? m.exit;
  const changed = {};
  const potential = m.rooms.reduce((s, r) => {
    const key = `${r.type}_rate`;
    const rate = c[key] ?? r.weekly_rate?.value ?? 0;
    if (c[key] != null && r.weekly_rate?.metric) changed[r.weekly_rate.metric] = { from: r.weekly_rate.value, to: rate };
    return s + r.beds * rate * (c.weeks ?? r.weeks ?? 51);
  }, 0);
  const gross = potential * occ, opex = gross * opexRatio, noi = gross - opex, yoc = noi / m.totalCost;
  changed.gross_income = { from: m.gross, to: gross };
  changed.opex = { from: m.opex, to: opex };
  changed.noi = { from: m.noi, to: noi };
  changed[m.yocMetric] = { from: m.yoc, to: yoc };
  if (c.exit_yield != null) changed.exit_yield = { from: m.exit, to: exit };
  const decision = decide(yoc, exit);
  const result = {
    ok: true, decision, changed,
    note: `Scenario rule: GO if yield on cost ≥ exit yield + 100 bps. IRR and equity multiple are not recomputed in scenarios.`,
  };
  state.scenario = { changes: c, result };
  state.report = state.baseReport; // values are overlaid via state.scenario at render time
  renderReport();
  flashFigure("noi");
  return {
    ok: true,
    applied_changes: describeChanges(c),
    occupancy_used: +(occ * 100).toFixed(1) + "%",
    gross_income: { base: fmtMoney(m.gross), scenario: fmtMoney(gross) },
    noi: { base: fmtMoney(m.noi), scenario: fmtMoney(noi) },
    yield_on_cost: { base: (m.yoc * 100).toFixed(2) + "%", scenario: (yoc * 100).toFixed(2) + "%" },
    exit_yield: (exit * 100).toFixed(2) + "%",
    verdict: { base: asset.verdict?.decision, scenario: decision },
    method: "Gross = beds × weekly rate × weeks × occupancy; OPEX = ratio × gross; total cost derived from base NOI ÷ base yield on cost. Inputs marked as assumptions stay assumptions.",
    not_recomputed: ["irr", "equity_multiple"],
  };
}
function resetWhatIf() { state.scenario = null; renderReport(); return { ok: true, message: "Back to the base case." }; }

// ---------- data loading ----------
function renderSteps(done, all, running) {
  const el = state.stepsEl;
  if (!el) return;
  el.innerHTML = all.map((s, i) => {
    const cls = i < done.length ? "done" : (running && i === done.length ? "active" : "pending");
    return `<li class="${cls}">${esc(s)}</li>`;
  }).join("");
}
const DEFAULT_STEPS = ["Extracting brochure", "Location & demographics", "Room-rate comps", "Market yields (MSCI)", "Rates (SONIA)", "NOI build-up", "Returns", "Verdict"];

function setReport(report, jobId) {
  state.baseReport = JSON.parse(JSON.stringify(report));
  state.report = state.baseReport;
  state.scenario = null;
  state.selected = null;
  state.jobId = jobId;
  renderReport();
  $("inspector-body").className = "inspector-empty";
  $("inspector-body").innerHTML = "<p>Click any figure to see where it came from.</p>";
  notifyAgent("A new deal report is loaded. Call get_deal_report before answering questions about it.");
}

async function poll(jobId) {
  for (let i = 0; i < 600; i++) {
    const r = await fetch(`${API}/analysis/${encodeURIComponent(jobId)}`);
    if (!r.ok) throw new Error(`Analysis request failed (${r.status}).`);
    const d = await r.json();
    renderSteps(d.steps || [], DEFAULT_STEPS, d.status === "running");
    if (d.status === "done") return d;
    if (d.status === "error") throw new Error(d.report?.error || "The analysis failed.");
    await new Promise((res) => setTimeout(res, 800));
  }
  throw new Error("The analysis is taking too long. Try again.");
}

async function analyse(files) {
  const pdfs = [...files].filter((f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));
  if (!pdfs.length) return showError("upload-error", "Please drop a PDF brochure.");
  addMessage("user", `Qualify ${pdfs.map((f) => f.name).join(", ")}`);
  chatLog.push({ role: "user", content: `I uploaded ${pdfs.map((f) => f.name).join(", ")} for qualification.` });
  const fd = new FormData();
  pdfs.forEach((f) => fd.append("files[]", f, f.name));
  const label = pdfs.length > 1 ? `${pdfs.length} brochures` : pdfs[0].name;
  progressMessage(`Qualifying **${label}**`);
  try {
    const r = await fetch(`${API}/analyze`, { method: "POST", body: fd });
    if (!r.ok) throw new Error(`Upload failed (${r.status}).`);
    const { job_id } = await r.json();
    await trackJob(job_id);
  } catch (e) {
    showError("upload-error", `${e.message}`);
  }
}

async function trackJob(jobId, label) {
  if (label) progressMessage(`Qualifying **${label}**`);
  const d = await poll(jobId);
  setReport(d.report, jobId);
  reportReady();
}

async function loadSample() {
  progressMessage("Opening the **78-102 Broadway, Stratford** sample");
  try {
    const r = await fetch(`${API}/analysis/demo-stratford`);
    if (!r.ok) throw new Error(`Could not load the sample (${r.status}).`);
    const d = await r.json();
    renderSteps(d.steps || DEFAULT_STEPS, DEFAULT_STEPS, false);
    setReport({ ...d.report, mock: d.mock ?? d.report?.mock }, d.job_id || "demo-stratford");
    reportReady();
  } catch (e) {
    showError("upload-error", `${e.message} Is the API running at ${API}?`);
  }
}

function showError(id, msg) {
  if (id === "upload-error") { state.stepsEl = null; return addMessage("assistant", msg, { error: true }); }
  if (id === "voice-error" && !document.body.classList.contains("report-open")) toast(msg, true);
  const el = $(id); el.textContent = msg; el.hidden = false;
}
function hideError(id) { const el = $(id); if (el) el.hidden = true; }

// ---------- agent context ----------
function compactReport() {
  if (!state.report) return { loaded: false, message: "No deal report is loaded yet. Ask the user to drop a brochure or load the Stratford sample." };
  return {
    loaded: true,
    job_id: state.jobId,
    sample_data: !!state.report.mock,
    scenario_active: state.scenario ? describeChanges(state.scenario.changes) : null,
    assets: (state.report.assets || []).map((a) => ({
      name: a.name, address: a.address,
      verdict: state.scenario ? { base: a.verdict?.decision, scenario: state.scenario.result.decision, reasons: a.verdict?.reasons } : a.verdict,
      figures: collectFigures(a).map(({ fig, section, room }) => {
        const ch = state.scenario?.result?.changed?.[fig.metric];
        return {
          metric: fig.metric, label: fig.label, section, room_type: room?.type, beds: room?.beds, weeks: room?.weeks,
          value: ch ? ch.to : fig.value, display: formatValue(ch ? { ...fig, value: ch.to } : fig), unit: fig.unit,
          base_value: ch ? formatValue({ ...fig, value: ch.from }) : undefined,
          source: sourceText(fig.source), source_type: fig.source?.type || "assumption", page: fig.source?.page ?? null,
          status: fig.status || "not_checked",
        };
      }),
    })),
  };
}

const clientTools = {
  get_deal_report: async () => JSON.stringify(compactReport()),
  highlight_figure: async ({ metric } = {}) => {
    const info = selectFigure(metric, { scroll: true });
    if (!info) return JSON.stringify({ ok: false, error: `Unknown metric "${metric}".`, available_metrics: [...state.figures.keys()] });
    logTool(`highlight_figure · ${metric}`);
    return JSON.stringify({ ok: true, ...info });
  },
  open_source: async ({ metric } = {}) => {
    const info = selectFigure(metric, { scroll: true });
    if (!info) return JSON.stringify({ ok: false, error: `Unknown metric "${metric}".`, available_metrics: [...state.figures.keys()] });
    $("inspector").scrollIntoView({ behavior: "smooth", block: "nearest" });
    logTool(`open_source · ${metric}`);
    return JSON.stringify({ ok: true, ...info });
  },
  run_what_if: async (p = {}) => {
    const res = runWhatIf(p);
    logTool(`run_what_if · ${res.applied_changes || "no data"}`);
    return JSON.stringify(res);
  },
  reset_what_if: async () => { logTool("reset_what_if"); return JSON.stringify(resetWhatIf()); },
};

function notifyAgent(text) {
  try { conversation?.sendContextualUpdate?.(text); } catch { /* not connected */ }
}

// ---------- voice ----------
function setVoiceState(s, label) {
  $("voice-status").dataset.state = s;
  $("voice-status-text").textContent = label;
  $("meter").className = `meter ${s}`;
}
function addLine(role, text) {
  const li = document.createElement("li");
  li.className = role;
  li.innerHTML = `<span class="who">${role === "user" ? "You" : "Analyst"}</span><span class="txt">${esc(text)}</span>`;
  $("transcript").appendChild(li);
  $("transcript").scrollTop = $("transcript").scrollHeight;
  addMessage(role === "user" ? "user" : "assistant", text, { voice: true });
}
function logTool(text) {
  const li = document.createElement("li");
  li.className = "tool";
  li.textContent = text;
  $("transcript").appendChild(li);
  $("transcript").scrollTop = $("transcript").scrollHeight;
}

async function getSession() {
  // 1) WebRTC token (best echo cancellation), 2) WebSocket signed URL, 3) public agent id (local tests only)
  try {
    const r = await fetch(`${API}/voice/token`);
    if (r.ok) { const { token } = await r.json(); if (token) return { conversationToken: token, connectionType: "webrtc" }; }
  } catch { /* fall through */ }
  try {
    const r = await fetch(`${API}/voice/signed-url`);
    if (r.ok) { const { signed_url } = await r.json(); if (signed_url) return { signedUrl: signed_url, connectionType: "websocket" }; }
  } catch { /* fall through */ }
  if (PUBLIC_AGENT_ID) return { agentId: PUBLIC_AGENT_ID };
  throw new Error("Voice is not configured on the server (ELEVENLABS_API_KEY / ELEVENLABS_AGENT_ID).");
}

async function startVoice() {
  hideError("voice-error");
  const btn = $("voice-toggle");
  btn.disabled = true;
  setVoiceState("connecting", "Connecting");
  try {
    await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    btn.disabled = false;
    setVoiceState("disconnected", "Offline");
    return showError("voice-error", "Microphone access is blocked. Allow it in the browser and try again.");
  }
  try {
    const session = await getSession();
    conversation = await Conversation.startSession({
      ...session,
      clientTools,
      onConnect: () => { setVoiceState("listening", "Listening"); $("mic-btn").setAttribute("aria-pressed", "true"); btn.textContent = "End conversation"; btn.dataset.active = "true"; btn.disabled = false; startMeter(); },
      onDisconnect: () => endedUi(),
      onError: (msg) => showError("voice-error", typeof msg === "string" ? msg : "Voice connection error."),
      onModeChange: ({ mode }) => setVoiceState(mode, mode === "speaking" ? "Speaking" : "Listening"),
      onMessage: ({ message, source, role }) => {
        const who = role || (source === "user" ? "user" : "agent");
        if (message) addLine(who === "user" ? "user" : "agent", message);
      },
    });
    if (state.report) notifyAgent("A deal report is already loaded on screen. Call get_deal_report before answering.");
  } catch (e) {
    conversation = null;
    endedUi();
    showError("voice-error", e?.message || "Could not start the voice analyst.");
  }
}
function endedUi() {
  conversation = null;
  $("mic-btn").setAttribute("aria-pressed", "false");
  const btn = $("voice-toggle");
  btn.textContent = "Start voice analyst";
  btn.dataset.active = "false";
  btn.disabled = false;
  setVoiceState("disconnected", "Offline");
  stopMeter();
}
async function stopVoice() {
  try { await conversation?.endSession(); } finally { endedUi(); }
}

function startMeter() {
  const bars = [...$("meter").children];
  const tick = async () => {
    if (!conversation) return;
    let v = 0;
    try {
      v = $("voice-status").dataset.state === "speaking" ? await conversation.getOutputVolume() : await conversation.getInputVolume();
    } catch { v = 0; }
    bars.forEach((b, i) => { const h = 4 + Math.min(24, v * 60 * (0.6 + ((i * 37) % 5) / 6)); b.style.height = `${h}px`; });
    meterRaf = requestAnimationFrame(tick);
  };
  meterRaf = requestAnimationFrame(tick);
}
function stopMeter() { if (meterRaf) cancelAnimationFrame(meterRaf); meterRaf = null; [...$("meter").children].forEach((b) => (b.style.height = "4px")); }

function ask(text) {
  const q = (text || "").trim();
  if (!q) return;
  if (!conversation) return showError("voice-error", "Start the voice analyst first, then ask or type your question.");
  addLine("user", q);
  conversation.sendUserMessage(q);
}

// ---------- chat shell ----------
const chatLog = [];          // [{role, content}] sent to /api/chat
let integrations = { outlook: {}, gmail: {}, dealroom: {}, llm: {} };
let busy = false;

// Small, safe markdown: escape first, then add a few inline/block rules.
function renderMarkdown(src) {
  const inline = (t) => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  let list = null, para = [];
  const flushPara = () => { if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; };
  const flushList = () => { if (list) out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; };
  for (const line of esc(src || "").split("\n")) {
    const ul = line.match(/^\s*[-*•]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/), h = line.match(/^(#{2,4})\s+(.*)/);
    if (ul || ol) {
      flushPara();
      const tag = ul ? "ul" : "ol";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push((ul || ol)[1]);
    } else if (h) { flushPara(); flushList(); out.push(`<h${h[1].length === 2 ? 3 : 4}>${inline(h[2])}</h${h[1].length === 2 ? 3 : 4}>`); }
    else if (!line.trim()) { flushPara(); flushList(); }
    else { flushList(); para.push(line); }
  }
  flushPara(); flushList();
  return out.join("");
}

function startChatting() { document.body.classList.add("chatting"); }
function scrollThread() { requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" })); }

function addMessage(role, text, { error = false, voice = false, html = null } = {}) {
  startChatting();
  const li = document.createElement("li");
  li.className = `msg ${role}${error ? " error" : ""}${voice ? " voice-line" : ""}`;
  if (role === "user") li.textContent = text;
  else li.innerHTML = (voice ? `<span class="who">Voice analyst</span>` : "") + (html ?? `<div class="md">${renderMarkdown(text)}</div>`);
  $("thread").appendChild(li);
  scrollThread();
  return li;
}

function progressMessage(title) {
  const li = addMessage("assistant", "", { html: `<div class="md">${renderMarkdown(title)}</div><ol class="steps"></ol>` });
  state.stepsEl = li.querySelector(".steps");
  renderSteps([], DEFAULT_STEPS, true);
  return li;
}

function reportReady() {
  const assets = state.report?.assets || [];
  const a = assets[0] || {};
  const d = a.verdict?.decision || "—";
  const name = assets.length > 1 ? `${assets.length} assets` : (a.name || "Deal");
  const reasons = (a.verdict?.reasons || []).slice(0, 3).map((r) => `<li>${esc(r)}</li>`).join("");
  state.stepsEl = null;
  const val = (m) => { const f = (a.returns || []).find((x) => x.metric === m); return f ? formatValue(f) : null; };
  const kpis = [["IRR", val("irr")], ["Equity multiple", val("equity_multiple")], ["Yield on cost", val("entry_yield") ?? val("yield_on_cost")], ["Exit yield", val("exit_yield")]].filter(([, v]) => v);
  const photos = (a.comps || []).filter((c) => c?.image).slice(0, 5);
  state.reportMsg = addMessage("assistant", "", { html: `
    <div class="report-card">
      <div class="rc-head"><span class="rc-verdict" data-d="${esc(d)}">${esc(d)}</span><div><strong>${esc(name)}</strong><small>${esc(a.address || "")}</small></div></div>
      ${kpis.length ? `<div class="rc-kpis">${kpis.map(([k, v]) => `<div><small>${k}</small><b>${esc(v)}</b></div>`).join("")}</div>` : ""}
      ${reasons ? `<ul class="rc-reasons">${reasons}</ul>` : ""}
      ${photos.length ? `<div class="rc-photos">${photos.map((c) => `<img src="${esc(encodeURI(decodeURI(c.image)))}" alt="${esc(c.property_name)}" title="${esc(c.property_name)}" loading="lazy" onerror="this.remove()">`).join("")}<span>${photos.length} comps nearby</span></div>` : ""}
      <button type="button" class="btn-glow" data-open-report>View full report</button>
    </div>
    <div class="inline-report" hidden></div>` });
  chatLog.push({ role: "assistant", content: `Report ready for ${name}: ${d}. Reasons: ${(a.verdict?.reasons || []).join("; ")}` });
  $("open-report").hidden = false;
}

// The report opens inside the chat message, the conversation continues below it.
function openReport(msg = state.reportMsg) {
  if (!state.report || !msg) return;
  state.reportMsg = msg;
  document.querySelectorAll(".inline-report").forEach((b) => { if (!msg.contains(b)) { b.hidden = true; b.closest(".msg")?.querySelector("[data-open-report]")?.replaceChildren("View full report"); } });
  const box = msg.querySelector(".inline-report");
  box.appendChild($("report"));
  box.hidden = false;
  msg.querySelector("[data-open-report]").textContent = "Hide report";
  msg.scrollIntoView({ behavior: "smooth", block: "start" });
}
function closeReport(msg = state.reportMsg) {
  const box = msg?.querySelector(".inline-report");
  if (!box) return;
  box.hidden = true;
  msg.querySelector("[data-open-report]").textContent = "View full report";
  hideCite();
}

function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg; t.className = `toast${err ? " err" : ""}`; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 4200);
}

function emailsHtml(emails) {
  if (!emails?.length) return "";
  return `<ul class="emails">${emails.map((e) => `
    <li class="email">
      <span class="subj"><img class="mail-src" src="brand/${e.source === "gmail" ? "gmail" : "outlook"}.svg" alt="${e.source === "gmail" ? "Gmail" : "Outlook"}" width="14" height="14">${esc(e.subject)}</span>
      <span class="meta">${esc(e.from || "")} · ${e.received ? new Date(e.received).toLocaleDateString("en-GB", { day: "numeric", month: "short" }) : ""}${e.pdfs?.length ? ` · ${e.pdfs.length} PDF` : ""}</span>
      <span class="prev">${esc(e.preview)}</span>
      <button type="button" class="btn-glow" data-analyze="${esc(e.id)}" data-subject="${esc(e.subject)}" ${e.pdfs?.length ? "" : "disabled title=\"No PDF attached\""}>Qualify</button>
    </li>`).join("")}</ul>`;
}

async function analyzeEmail(id, subject, btn) {
  if (btn) btn.disabled = true;
  addMessage("user", `Qualify “${subject}”`);
  chatLog.push({ role: "user", content: `Qualify the email "${subject}".` });
  progressMessage(`Reading the brochure from **${subject}**`);
  try {
    const r = await fetch(`${API}/outlook/messages/${encodeURIComponent(id)}/analyze`, { method: "POST", credentials: "include" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `Request failed (${r.status})`);
    if (d.error) throw new Error(d.error);
    await trackJob(d.job_id);
  } catch (e) {
    showError("upload-error", e.message);
  } finally { if (btn) btn.disabled = false; }
}

const TOOL_LABELS = { check_inbox: "Checked Outlook", analyze_email: "Started qualification", load_sample_report: "Opened sample" };
async function sendPrompt(text) {
  const q = (text || "").trim();
  if (!q || busy) return;
  busy = true; $("composer").classList.add("is-busy"); $("send-btn").disabled = true;
  addMessage("user", q);
  chatLog.push({ role: "user", content: q });
  const pending = addMessage("assistant", "", { html: `<span class="typing" aria-label="Thinking"><i></i><i></i><i></i></span>` });
  try {
    const r = await fetch(`${API}/chat`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ messages: chatLog.slice(-20) }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `The agent is unavailable (${r.status}).`);
    const actions = d.actions || [];
    const tools = actions.filter((a) => a.type === "tool").map((a) => TOOL_LABELS[a.name] || (a.name.startsWith("dealroom__") ? `Dealroom · ${a.name.slice(10).replace(/_/g, " ")}` : a.name));
    const emails = actions.filter((a) => a.type === "emails").flatMap((a) => a.emails);
    const connects = actions.filter((a) => a.type === "connect");
    pending.innerHTML =
      (tools.length ? `<div class="tool-trail">${tools.map((t) => `<span>${esc(t)}</span>`).join("")}</div>` : "") +
      `<div class="md">${renderMarkdown(d.reply || "")}</div>` + emailsHtml(emails) +
      (connects.length ? `<div class="connect-row">${connects.map((c) => `<button type="button" class="btn-glow" data-connect="${esc(c.name)}">Connect ${{ outlook: "Outlook", gmail: "Gmail", dealroom: "Dealroom" }[c.name] || c.name}</button>`).join("")}</div>` : "");
    chatLog.push({ role: "assistant", content: d.reply || "" });
    scrollThread();
    for (const a of actions) {
      if (a.type === "sample") loadSample();
      if (a.type === "analyze" && a.job_id) trackJob(a.job_id, a.subject).catch((e) => showError("upload-error", e.message));
    }
  } catch (e) {
    pending.classList.add("error");
    pending.innerHTML = `<div class="md">${renderMarkdown(e.message)}</div>`;
    chatLog.pop();
  } finally {
    busy = false; $("composer").classList.remove("is-busy"); $("send-btn").disabled = !$("prompt").value.trim();
  }
}

// ---------- integrations ----------
async function refreshIntegrations() {
  try {
    const r = await fetch(`${API}/integrations`, { credentials: "include" });
    if (r.ok) integrations = await r.json();
  } catch { /* API down: keep defaults */ }
  const o = integrations.outlook || {}, dr = integrations.dealroom || {};
  $("outlook-badge").textContent = o.connected ? "Connected" : "Connect";
  $("outlook-badge").classList.toggle("on", !!o.connected);
  $("outlook-state").textContent = o.connected ? (o.account || "Inbox connected") : (o.configured === false ? "Needs Azure app keys on the server" : "Read new deal emails");
  const g = integrations.gmail || {};
  document.querySelector('[data-act="gmail"]').hidden = !g.configured && !g.connected;  // hidden until Google OAuth keys are on the server
  $("gmail-badge").textContent = g.connected ? "Connected" : "Connect";
  $("gmail-badge").classList.toggle("on", !!g.connected);
  $("gmail-state").textContent = g.connected ? (g.account || "Inbox connected") : "Read new deal emails";
  $("dealroom-badge").textContent = dr.connected ? "Connected" : "Connect";
  $("dealroom-badge").classList.toggle("on", !!dr.connected);
  $("dealroom-state").textContent = dr.connected ? "Dealroom MCP connected" : "Investors, companies, transactions";
  $("sources").innerHTML = [o.connected && "Outlook", g.connected && "Gmail", dr.connected && "Dealroom"].filter(Boolean).map((n) => `<span>${n}</span>`).join("");
}

async function toggleIntegration(name) {
  const info = integrations[name] || {};
  if (name === "gmail" && info.configured === false) return toast("Gmail needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET on the server first.", true);
  if (info.connected) {
    const nice = { outlook: "Outlook", gmail: "Gmail", dealroom: "Dealroom" }[name] || name;
    if (!confirm(`Disconnect ${nice}?`)) return;
    await fetch(`${API}/integrations/${name}/disconnect`, { method: "POST", credentials: "include" }).catch(() => {});
    await refreshIntegrations();
    return toast(`${nice} disconnected`);
  }
  if (name === "outlook" && info.configured === false) return toast("Outlook needs MS_CLIENT_ID / MS_CLIENT_SECRET on the server first.", true);
  location.href = `${API}/auth/${name}/start`;
}

function setMenu(open) {
  $("plus-menu").hidden = !open;
  $("plus-btn").setAttribute("aria-expanded", String(open));
  if (open) $("plus-menu").querySelector("button")?.focus();
}

function initChat() {
  const prompt = $("prompt");
  const autosize = () => { prompt.style.height = "auto"; prompt.style.height = `${Math.min(prompt.scrollHeight, 220)}px`; $("send-btn").disabled = busy || !prompt.value.trim(); };
  prompt.addEventListener("input", autosize);
  prompt.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); } });
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); const v = prompt.value; prompt.value = ""; autosize(); sendPrompt(v); });

  $("plus-btn").addEventListener("click", (e) => { e.stopPropagation(); setMenu($("plus-menu").hidden); });
  $("announce").addEventListener("click", () => openAi());
  $("open-ai").addEventListener("click", () => openAi());
  document.addEventListener("click", (e) => { if (!e.target.closest(".plus-wrap")) setMenu(false); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("plus-menu").hidden) { setMenu(false); $("plus-btn").focus(); }
  });

  const act = (name) => {
    setMenu(false);
    if (name === "upload") $("file-input").click();
    else if (name === "sample") { addMessage("user", "Open the Stratford sample"); loadSample(); }
    else if (name === "ai") openAi();
    else if (name === "outlook" || name === "gmail" || name === "dealroom") toggleIntegration(name);
  };
  document.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => act(b.dataset.act)));
  document.querySelectorAll(".quick-chip[data-prompt]").forEach((b) => b.addEventListener("click", () => sendPrompt(b.dataset.prompt)));

  $("thread").addEventListener("click", (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.analyze) analyzeEmail(t.dataset.analyze, t.dataset.subject, t);
    else if ("openReport" in t.dataset) { const m = t.closest(".msg"); m.querySelector(".inline-report").hidden ? openReport(m) : closeReport(m); }
    else if (t.dataset.connect) toggleIntegration(t.dataset.connect);
  });

  $("file-input").addEventListener("change", (e) => { if (e.target.files?.length) analyse(e.target.files); e.target.value = ""; });
  const comp = $("composer");
  ["dragenter", "dragover"].forEach((ev) => comp.addEventListener(ev, (e) => { e.preventDefault(); comp.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((ev) => comp.addEventListener(ev, (e) => { e.preventDefault(); if (ev === "drop" || !comp.contains(e.relatedTarget)) comp.classList.remove("is-over"); }));
  comp.addEventListener("drop", (e) => e.dataTransfer?.files?.length && analyse(e.dataTransfer.files));

  $("mic-btn").addEventListener("click", () => (conversation ? stopVoice() : startVoice()));
  $("open-report").addEventListener("click", () => openReport());
  $("new-chat").addEventListener("click", () => location.assign(location.pathname));
  $("home-btn").addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));

  // Returning from an OAuth sign-in
  const connected = params.get("connected"), error = params.get("error");
  if (connected || error) history.replaceState(null, "", location.pathname);
  refreshIntegrations().then(() => {
    if (connected === "outlook" || connected === "gmail") { toast(`${connected === "gmail" ? "Gmail" : "Outlook"} connected`); sendPrompt("Check my inbox for new real estate deals"); }
    else if (connected === "dealroom") toast("Dealroom connected. Ask about investors, companies or transactions.");
    else if (error) toast({ outlook_not_configured: "Outlook isn't set up on the server yet (Azure app keys missing).", outlook_auth_failed: "Outlook sign-in was cancelled or failed.", gmail_not_configured: "Gmail isn't set up on the server yet (Google OAuth client missing).", gmail_auth_failed: "Gmail sign-in was cancelled or failed.", dealroom_auth_failed: "Dealroom sign-in was cancelled or failed.", dealroom_host_not_allowed: "Dealroom hasn't approved this domain for MCP sign-in yet. We've asked them to allowlist it.", dealroom_unavailable: "Dealroom is unreachable right now. Try again in a minute." }[error] || "Sign-in failed.", true);
  });
  if (params.get("sample") === "1") loadSample();
  const shared = params.get("report");  // link handed out by the MCP server (Claude / Grok / ChatGPT)
  const focusHash = () => {  // ?report=…#irr opens the source popover of that figure
    const el = location.hash && document.querySelector(`#report [data-metric="${CSS.escape(decodeURIComponent(location.hash.slice(1)))}"]`);
    if (el) { el.scrollIntoView({ block: "center" }); setTimeout(() => el.click(), 350); }
  };
  if (shared === "demo-stratford") loadSample().then(() => openReport()).then(focusHash);
  else if (shared) trackJob(shared, "the shared deal").then(() => openReport()).then(focusHash).catch(() => toast("This report link has expired.", true));
  prompt.focus();
}

// ---------- connect dealt (MCP) to Claude / Grok / ChatGPT ----------
const MCP_URL = params.get("mcp") || new URL(`${API}/mcp`, location.href).href;
const MCP_TOOLS = [
  ["qualify_brochure", "Qualify a deal from a brochure PDF link: GO / NO-GO"],
  ["get_report", "Memo where every number links to its source + link to this report"],
  ["get_room_comps", "5 nearest PBSA buildings: rate per room type, weeks, photo, page"],
  ["get_city_demand", "HESA students, Russell Group share, beds per student"],
  ["build_noi", "NOI build-up: gross rent × occupancy − OPEX"],
  ["compute_returns", "IRR, equity multiple, entry vs exit yield"],
  ["deal_verdict", "GO / NO-GO with reasons"],
  ["ask_deal_memory", "Ask across every deal analysed so far (cognee)"],
  ["get_sample_report", "Full sourced report for the Stratford deal"],
];
const AI_GUIDES = {
  claude: {
    name: "Claude",
    steps: [
      "Open <strong>claude.ai</strong> → <strong>Settings</strong> → <strong>Connectors</strong>.",
      "Click <strong>Add custom connector</strong>, name it <code>dealt</code> and paste the server URL.",
      "In a chat, enable <strong>dealt</strong> from the tools menu, then ask: <em>“Qualify the Stratford brochure”</em>.",
    ],
    snippetLabel: "Claude Code",
    snippet: () => `claude mcp add --transport http dealt ${MCP_URL}`,
  },
  grok: {
    name: "Grok",
    steps: [
      "In <strong>Grok Bot</strong> (or its Cursor), open the MCP settings and add a remote server with the URL above.",
      "From code, pass dealt to Grok as a remote MCP tool in the xAI API (snippet below).",
      "Ask: <em>“Check my inbox for new deals and give me a GO / NO-GO”</em>.",
    ],
    snippetLabel: "xAI API",
    snippet: () => `"tools": [{ "type": "mcp", "server_label": "dealt", "server_url": "${MCP_URL}" }]`,
  },
  chatgpt: {
    name: "ChatGPT",
    steps: [
      "Open <strong>ChatGPT</strong> → <strong>Settings</strong> → <strong>Apps &amp; Connectors</strong> → <strong>Advanced</strong>, turn on <strong>Developer mode</strong>.",
      "Click <strong>Create</strong>, name it <code>dealt</code>, paste the server URL, authentication <strong>None</strong>.",
      "In a chat, pick <strong>dealt</strong> under <strong>+</strong> → <strong>More</strong>, then ask about a deal.",
    ],
    snippetLabel: "OpenAI API",
    snippet: () => `"tools": [{ "type": "mcp", "server_label": "dealt", "server_url": "${MCP_URL}", "require_approval": "never" }]`,
  },
};

function renderAi(key) {
  const g = AI_GUIDES[key];
  document.querySelectorAll(".ai-tabs [data-ai]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.ai === key)));
  $("ai-body").innerHTML = `
    <div class="ai-field">
      <p class="mono-label">Server URL</p>
      <div class="copy-row"><code>${esc(MCP_URL)}</code><button type="button" class="pill" data-copy="${esc(MCP_URL)}">Copy</button></div>
    </div>
    <div class="ai-field">
      <p class="mono-label">How to connect ${esc(g.name)}</p>
      <ol class="ai-steps">${g.steps.map((t) => `<li>${t}</li>`).join("")}</ol>
    </div>
    <div class="ai-field">
      <p class="mono-label">${esc(g.snippetLabel)}</p>
      <div class="copy-row"><code>${esc(g.snippet())}</code><button type="button" class="pill" data-copy="${esc(g.snippet())}">Copy</button></div>
    </div>
    <div class="ai-field">
      <p class="mono-label">Tools exposed</p>
      <ul class="ai-tools">${MCP_TOOLS.map(([n, d]) => `<li><code>${n}</code><span>${d}</span></li>`).join("")}</ul>
    </div>`;
}
function openAi(key = "claude") { setMenu(false); renderAi(key); $("ai-modal").showModal(); }
$("ai-close").addEventListener("click", () => $("ai-modal").close());
$("ai-modal").addEventListener("click", (e) => {
  if (e.target === $("ai-modal")) return $("ai-modal").close();
  const tab = e.target.closest("[data-ai]");
  if (tab) return renderAi(tab.dataset.ai);
  const copy = e.target.closest("[data-copy]");
  if (copy) navigator.clipboard?.writeText(copy.dataset.copy).then(() => { copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy"), 1400); });
});

// ---------- wire up ----------
$("voice-toggle").addEventListener("click", () => (conversation ? stopVoice() : startVoice()));
document.querySelectorAll(".chip[data-q]").forEach((c) => c.addEventListener("click", () => ask(c.dataset.q)));
$("ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("ask-input").value); $("ask-input").value = ""; });
$("ask-input").addEventListener("input", () => { try { conversation?.sendUserActivity?.(); } catch { /* ignore */ } });

// Expose for quick console tests: dealUI.runWhatIf({occupancy: 0.9})
window.dealUI = { state, runWhatIf, resetWhatIf, compactReport, loadSample };

initChat();

// Feature flag: Dealroom stays hidden until Dealroom allowlists demo3.ai-vance.de. Show with ?dealroom=1
if (params.get("dealroom") === "1") document.querySelectorAll('[data-feature="dealroom"]').forEach((el) => { el.hidden = false; });
