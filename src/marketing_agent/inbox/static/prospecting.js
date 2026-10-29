// Marketing › Prospecting: run brand searches, tune the search setup with the model, work the prospects.
const PROSPECT_STAGES = { found: "Found", shortlisted: "Shortlisted", contacted: "Contacted", dismissed: "Dismissed" };
const SETUP_LISTS = { keywords: "p-keywords", extra_queries: "p-extra", craft_terms: "p-terms", exclude_terms: "p-exclude" };
const SETUP_NUMBERS = { leads_per_run: "p-per-run", max_per_country: "p-per-country", queries_per_run: "p-queries",
                        lookup_limit: "p-lookups", minutes: "p-minutes", results_per_query: "p-results",
                        monthly_search_budget: "p-budget", cost_per_1000: "p-cost" };
const SETUP_FLAGS = { site_queries: "p-sites", snowball: "p-snowball" };
const CRITERIA_NUMBERS = { min_followers: "p-min-followers", max_followers: "p-max-followers", ideal_followers: "p-ideal-followers",
                           min_posts: "p-min-posts", max_posts: "p-max-posts", active_within_days: "p-active-days" };
const TUNER_PRESETS = [
  "Which queries are wasting searches, and what should replace them?",
  "Find more Indian / South Asian-owned labels in the USA.",
  "Most candidates are too big. Make the searches favour tiny new labels.",
  "Add more Indian cities beyond the metros.",
  "Which other countries have many small South Asian-owned womenswear labels? Add the best two.",
];
const prospecting = { history: [], pending: null, proposal: null, poll: null, setupLoaded: false, countries: {}, targets: [] };

// Every ISO country, fetched once: code -> {name, flag}.
async function loadCountries() {
  if (Object.keys(prospecting.countries).length) return;
  const list = await api("/api/prospecting/countries").catch(() => []);
  for (const c of list) prospecting.countries[c.code] = c;
  $("p-country-list").innerHTML = list.map((c) => `<option value="${esc(c.name)}">${c.flag} ${c.code.toUpperCase()}</option>`).join("");
}
const countryLabel = (code) => {
  const c = prospecting.countries[code];
  return c ? `${c.flag} ${esc(c.name)}` : esc((code || "?").toUpperCase());
};

async function loadProspecting() {
  await loadCountries();
  loadProspectTiles();
  loadProspects();
  loadSearches();
  loadQueryStats();
  if (!prospecting.setupLoaded) loadSetup();
  $("p-presets").innerHTML = TUNER_PRESETS.map((p, i) => `<button class="chip-toggle" onclick="$('p-message').value = TUNER_PRESETS[${i}]">${esc(p)}</button>`).join("");
  pollSearch();
}

async function loadProspectTiles() {
  const [f, u] = await Promise.all([api("/api/prospects/funnel").catch(() => null), api("/api/prospecting/usage").catch(() => null)]);
  if (!f) return;
  prospecting.usage = u;
  if (prospecting.setupLoaded) previewSize();
  const n = (k) => f.steps.find((s) => s.stage === k)?.reached ?? 0;
  $("p-tiles").innerHTML = [
    tile("Prospects found", fmt(n("found")), `${fmt(f.dismissed)} dismissed`),
    tile("Shortlisted", fmt(n("shortlisted")), "worth a DM"),
    tile("Contacted", fmt(n("contacted")), "DM sent"),
    tile("Replied", fmt(n("replied")), n("contacted") ? `${(100 * n("replied") / n("contacted")).toFixed(0)}% of contacted` : ""),
    tile("Quoted", fmt(n("quoted")), `${fmt(n("won"))} won`),
    u ? tile("Searches this month", fmt(u.month), `≈ $${u.month_cost.toFixed(2)}${u.budget ? ` · ${fmt(u.budget_left)} left` : ""}`) : "",
  ].join("");
}

// ---- prospects ----

let prospectTimer;
function debouncedProspects() { clearTimeout(prospectTimer); prospectTimer = setTimeout(loadProspects, 300); }

async function loadProspects() {
  const p = new URLSearchParams();
  for (const [key, id] of [["stage", "p-stage"], ["region", "p-region"], ["search_id", "p-run-filter"], ["q", "p-q"]])
    if ($(id).value) p.set(key, $(id).value);
  const rows = await api("/api/prospects?" + p).catch((e) => { toast(e.message); return []; });
  if (!rows.length) { $("p-prospects").innerHTML = `<div class="empty">No prospects here yet. Press <b>Run search</b>.</div>`; return; }
  $("p-prospects").innerHTML = `<table class="data"><thead><tr><th>Brand</th><th>Country</th><th class="num">Followers</th>
      <th>Founder</th><th>Website</th><th>Found</th><th>Stage</th><th>Sales funnel</th><th></th></tr></thead><tbody>
    ${rows.map(prospectRow).join("")}</tbody></table>`;
}

function prospectRow(r) {
  const options = Object.entries(PROSPECT_STAGES).map(([k, v]) => `<option value="${k}" ${k === r.stage ? "selected" : ""}>${v}</option>`).join("");
  const funnel = r.funnel ? `<span class="pill ${r.funnel.stage === "lost" ? "high" : "ok"}">${esc(STAGE_NAMES[r.funnel.stage] || r.funnel.stage)}</span>`
                          : `<span class="muted">${r.stage === "contacted" ? "no reply yet" : "–"}</span>`;
  const site = r.website ? `<a href="${esc(r.website)}" target="_blank" rel="noopener">${esc(r.website.replace(/^https?:\/\/(www\.)?/, "").slice(0, 28))}</a>` : "";
  const found = new Date(r.checked_at).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
  return `<tr>
    <td><a href="https://www.instagram.com/${esc(r.username)}/" target="_blank" rel="noopener" data-tip="${esc(r.bio || "")}"><b>${esc(r.name || r.username)}</b></a>
      <div class="muted" style="font-size:12px">@${esc(r.username)}</div></td>
    <td style="white-space:nowrap">${countryLabel(r.region)}</td><td class="num">${fmt(r.followers)}</td>
    <td>${esc(r.founder || "")}</td><td>${site}</td>
    <td class="muted" style="white-space:nowrap" data-tip="${esc(r.query || "")}">${found}</td>
    <td><select onchange="setProspectStage('${esc(r.username)}', this.value)">${options}</select></td>
    <td>${funnel}</td>
    <td style="white-space:nowrap"><a class="btn small" href="https://ig.me/m/${esc(r.username)}" target="_blank" rel="noopener">Open DM</a></td></tr>`;
}

async function setProspectStage(username, stage) {
  try {
    await api(`/api/prospects/${encodeURIComponent(username)}/stage`, { method: "POST", body: { stage } });
    toast(`@${username}: ${PROSPECT_STAGES[stage]}`);
  } catch (e) { toast(e.message); }
  loadProspectTiles();
}

// ---- search runs ----

async function startSearch() {
  try {
    await api("/api/prospecting/search", { method: "POST" });
    toast("Search started. Results appear as brands are verified.");
  } catch (e) { toast(e.message); return; }
  pollSearch();
}

async function pollSearch() {
  clearTimeout(prospecting.poll);
  const s = await api("/api/prospecting/status").catch(() => null);
  if (!s) return;
  $("p-run").disabled = s.running;
  $("p-run").textContent = s.running ? "Searching…" : "Run search";
  $("p-run-panel").hidden = !s.running && !s.log.length;
  $("p-run-state").textContent = s.running ? `Search #${s.search_id} running` : `Search #${s.search_id ?? ""} finished`;
  const log = $("p-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = s.log.join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
  if (s.running) {
    prospecting.wasRunning = true;
    prospecting.poll = setTimeout(() => { if (!$("marketing").hidden) { pollSearch(); loadProspects(); } }, 4000);
  } else if (prospecting.wasRunning) {
    prospecting.wasRunning = false;
    loadProspectTiles(); loadProspects(); loadSearches(); loadQueryStats();
  }
}

async function loadSearches() {
  const runs = await api("/api/prospecting/searches").catch(() => []);
  const filter = $("p-run-filter"), current = filter.value;
  filter.innerHTML = `<option value="">All searches</option>` + runs.map((r) =>
    `<option value="${r.id}">#${r.id} · ${new Date(r.started_at).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}</option>`).join("");
  filter.value = current;
  $("p-searches").innerHTML = runs.length ? `<table class="data"><thead><tr><th>Run</th><th>When</th><th class="num">Searches</th>
      <th class="num">Checked</th><th>Prospects</th><th>Result</th></tr></thead><tbody>
    ${runs.map((r) => `<tr><td>#${r.id}</td><td class="muted">${new Date(r.started_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}</td>
      <td class="num">${fmt(r.stats.queries)}</td><td class="num">${fmt(r.stats.lookups)}</td>
      <td>${Object.entries(r.leads).filter(([, n]) => n).map(([c, n]) => `${n} ${c.toUpperCase()}`).join(" · ") || "–"}</td>
      <td>${r.status === "running" ? `<span class="pill medium">running</span>` : r.status === "failed"
        ? `<span class="pill high" data-tip="${esc(r.error || "")}">failed</span>` : `<span class="muted">${esc(r.stopped || "done")}</span>`}</td></tr>`).join("")}
    </tbody></table>` : `<div class="muted">No searches yet.</div>`;
}

async function loadQueryStats() {
  const rows = await api("/api/prospecting/queries").catch(() => []);
  $("p-queries-table").innerHTML = rows.length ? `<table class="data"><thead><tr><th>Query</th><th class="num">Runs</th>
      <th class="num">Candidates</th><th class="num">Prospects</th><th class="num">Contacted</th></tr></thead><tbody>
    ${rows.slice(0, 60).map((q) => `<tr><td data-tip="${esc(q.query)}">${esc(q.query.replace("site:instagram.com ", "").slice(0, 60))}</td>
      <td class="num">${fmt(q.runs)}</td><td class="num">${fmt(q.candidates)}</td><td class="num">${fmt(q.leads)}</td>
      <td class="num">${fmt(q.contacted)}</td></tr>`).join("")}</tbody></table>`
    : `<div class="muted">Appears after the first search.</div>`;
}

// ---- search setup ----

async function loadSetup() {
  let s;
  try { s = await api("/api/prospecting/config"); } catch (e) { toast(e.message); return; }
  fillSetup(s.config);
  $("p-history").innerHTML = s.history.length
    ? s.history.map((h) => `<option value="${h.id}">v${h.id} · ${esc(h.created_by === "ai" ? "AI: " : "")}${esc(h.note.slice(0, 40))}</option>`).join("")
    : `<option value="">Default setup</option>`;
  prospecting.setupLoaded = true;
}

const lines = (text) => text.split("\n").map((l) => l.trim()).filter(Boolean);

function fillSetup(config) {
  for (const [key, id] of Object.entries(SETUP_LISTS)) $(id).value = config[key].join("\n");
  for (const [key, id] of Object.entries(SETUP_NUMBERS)) $(id).value = config[key];
  for (const [key, id] of Object.entries(SETUP_FLAGS)) $(id).checked = config[key];
  const c = config.criteria;
  for (const [key, id] of Object.entries(CRITERIA_NUMBERS)) $(id).value = c[key];
  document.querySelectorAll(".p-segment").forEach((box) => (box.checked = c.segments.includes(box.value)));
  $("p-ethnic").checked = c.allow_ethnic_only;
  $("p-fit-notes").value = c.fit_notes;
  prospecting.targets = config.targets.map((t) => ({ country: t.country, places: [...t.places] }));
  renderTargets();
}

// One card per country: its states / cities, one per line (empty = search the country name).
function renderTargets() {
  const region = $("p-region"), current = region.value;
  region.innerHTML = `<option value="">All countries</option>` + prospecting.targets.map((t) =>
    `<option value="${t.country}">${countryLabel(t.country)}</option>`).join("");
  region.value = current;
  $("p-targets").innerHTML = prospecting.targets.map((t, i) => `<div class="target">
      <div class="row"><b>${countryLabel(t.country)}</b><span class="muted" id="p-target-count-${i}">${t.places.length || "country name only"}${t.places.length ? " places" : ""}</span>
        <span class="spacer"></span>
        <button class="btn small" onclick="fillStates(${i})" title="Replace the list with every state / province">All states</button>
        <button class="btn small ghost" onclick="removeCountry(${i})">Remove</button></div>
      <textarea rows="3" data-target="${i}" placeholder="States or cities, one per line. Empty = search '${esc(prospecting.countries[t.country]?.name || t.country)}'"
        oninput="prospecting.targets[${i}].places = lines(this.value); previewSize()">${esc(t.places.join("\n"))}</textarea></div>`).join("")
    || `<div class="muted">No countries yet: add one above.</div>`;
  previewSize();
}

function addCountry() {
  const typed = $("p-add-country").value.trim().toLowerCase();
  const match = Object.values(prospecting.countries).find((c) => c.name.toLowerCase() === typed || c.code === typed);
  if (!match) { toast("Pick a country from the list."); return; }
  if (prospecting.targets.some((t) => t.country === match.code)) { toast(`${match.name} is already in the list.`); return; }
  prospecting.targets.push({ country: match.code, places: [] });
  $("p-add-country").value = "";
  renderTargets();
}

function removeCountry(i) { prospecting.targets.splice(i, 1); renderTargets(); }

async function fillStates(i) {
  const t = prospecting.targets[i];
  const r = await api(`/api/prospecting/countries/${t.country}/states`).catch((e) => { toast(e.message); return null; });
  if (!r) return;
  if (!r.states.length) { toast(`${r.name} has no states listed; add cities instead.`); return; }
  t.places = r.states;
  renderTargets();
  toast(`${r.states.length} states / provinces of ${r.name} added. Each is searched with every keyword.`);
}

function readSetup() {
  const config = { targets: prospecting.targets.map((t) => ({ country: t.country, places: [...t.places] })) };
  for (const [key, id] of Object.entries(SETUP_LISTS)) config[key] = lines($(id).value);
  for (const [key, id] of Object.entries(SETUP_NUMBERS)) config[key] = Number($(id).value);
  for (const [key, id] of Object.entries(SETUP_FLAGS)) config[key] = $(id).checked;
  config.criteria = { segments: [...document.querySelectorAll(".p-segment:checked")].map((b) => b.value),
                      allow_ethnic_only: $("p-ethnic").checked, fit_notes: $("p-fit-notes").value };
  for (const [key, id] of Object.entries(CRITERIA_NUMBERS)) config.criteria[key] = Number($(id).value);
  return config;
}

function previewSize() {
  const c = readSetup();
  const total = c.targets.reduce((sum, t) => sum + Math.max(1, t.places.length)
    * (c.keywords.length + (c.site_queries ? 1 : 0) + (t.country === "in" ? 0 : c.craft_terms.length)), 0);
  const perRun = Math.min(c.queries_per_run, total), rate = c.cost_per_1000 / 1000;
  const u = prospecting.usage;
  $("p-cost-line").textContent = `Each search is billed by the search API. This setup uses up to ${fmt(perRun)} searches a run, about $${(perRun * rate).toFixed(2)}`
    + (u ? `. This month so far: ${fmt(u.month)} searches, about $${(u.month * rate).toFixed(2)}${u.budget ? `, ${fmt(u.budget_left)} of ${fmt(u.budget)} left` : ""}.` : ".");
  prospecting.targets.forEach((t, i) => { const el = $(`p-target-count-${i}`); if (el) el.textContent = t.places.length ? `${t.places.length} places` : "country name only"; });
  $("p-size").textContent = `${fmt(total)} searches in rotation (${fmt(c.queries_per_run)} per run, so a full cycle takes ${fmt(Math.max(1, Math.ceil(total / Math.max(1, c.queries_per_run))))} runs).`;
}

async function saveSetup(config = readSetup(), note = "edited by hand", createdBy = "user") {
  try {
    await api("/api/prospecting/config", { method: "POST", body: { config, note, created_by: createdBy } });
    toast("Search setup saved. The next search uses it.");
    prospecting.setupLoaded = false;
    loadSetup();
  } catch (e) { toast(e.message); }
}

async function restoreSetup() {
  const id = $("p-history").value;
  if (!id) return;
  try { await api(`/api/prospecting/config/${id}/restore`, { method: "POST" }); toast(`Restored version ${id}.`); }
  catch (e) { toast(e.message); return; }
  prospecting.setupLoaded = false;
  loadSetup();
}

// ---- tune with AI ----

function renderTuner() {
  $("p-chat").innerHTML = prospecting.history.map((t) => `<div class="bubble ${t.role}">${t.role === "assistant" ? renderMarkdown(t.content) : esc(t.content)}</div>`).join("");
  const p = prospecting.proposal;
  $("p-proposal").hidden = !p;
  if (p) $("p-proposal").innerHTML = `<b>Proposed changes</b><ul>${p.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
    <div class="row"><button class="btn primary" onclick="applyProposal()">Apply to search setup</button>
      <button class="btn" onclick="previewProposal()">Show in the form</button>
      <button class="btn ghost" onclick="prospecting.proposal = null; renderTuner()">Discard</button></div>`;
}

async function askTuner() {
  const message = $("p-message").value.trim();
  if (!message) { toast("Say what you'd like to change, or pick a suggestion above."); return; }
  try {
    const est = await api("/api/prospecting/tune/estimate", { method: "POST", body: { message, history: prospecting.history } });
    prospecting.pending = message;
    askCost(costText("your search setup and how each query performed", est), confirmTuner);
  } catch (e) { toast(e.message); }
}

async function confirmTuner() {
  const message = prospecting.pending;
  const btn = $("p-ask"); btn.disabled = true; btn.textContent = "Thinking…";
  try {
    const r = await api("/api/prospecting/tune", { method: "POST", body: { message, history: prospecting.history, confirm: true } });
    prospecting.history.push({ role: "user", content: message }, { role: "assistant", content: r.reply });
    prospecting.proposal = r.config ? { config: r.config, changes: r.changes } : null;
    $("p-message").value = "";
    renderTuner();
  } catch (e) { toast(e.message); }
  btn.disabled = false; btn.textContent = "Ask";
}

function previewProposal() { fillSetup(prospecting.proposal.config); toast("Loaded into the form. Review it, then Save setup."); }

async function applyProposal() {
  const p = prospecting.proposal;
  await saveSetup(p.config, p.changes.join("; ").slice(0, 200) || "AI suggestion", "ai");
  prospecting.proposal = null;
  renderTuner();
}

function resetTuner() { prospecting.history = []; prospecting.proposal = null; renderTuner(); }
