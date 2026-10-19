// Sales module: funnel, drop-offs, weekly volume, sources, open pipeline board, strategist.
const STAGE_NAMES = { new: "New", engaged: "Engaged", qualified: "Qualified", quoted: "Quoted", order: "Sample / Order", won: "Won", lost: "Lost" };
const OPEN_STAGES = ["new", "engaged", "qualified", "quoted", "order"];
const sales = { report: null, history: [], pending: null };

function salesParams(extra = {}) {
  const p = new URLSearchParams({ days: $("s-days").value, ...extra });
  if ($("s-segment").value) p.set("segment", $("s-segment").value);
  if ($("s-source").value) p.set("source", $("s-source").value);
  return p;
}

async function loadSales() {
  let report;
  try { report = await api("/api/funnel?" + salesParams()); } catch (e) { toast("Couldn't load the funnel: " + e.message); return; }
  sales.report = report;
  renderSourceFilter(report);
  renderTiles(report);
  renderFunnel(report);
  renderDropoffs(report);
  renderWeekly(report.weekly);
  renderSources(report.by_source);
  renderPresets();
  loadBoard();
}

function renderSourceFilter(r) {
  const sel = $("s-source");
  if (sel.options.length > 1) return;
  sel.innerHTML += r.by_source.map((s) => `<option value="${esc(s.key)}">${esc(s.label)}</option>`).join("");
}

function tile(label, value, note = "") {
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${value}</div><div class="note">${note}</div></div>`;
}

function renderTiles(r) {
  const quoted = r.steps.find((s) => s.stage === "quoted").reached;
  const order = r.steps.find((s) => s.stage === "order").reached;
  const resp = r.response;
  $("s-tiles").innerHTML = [
    tile("Leads", fmt(r.total), `${fmt(r.open)} open`),
    tile("Quoted", fmt(quoted), r.total ? `${(100 * quoted / r.total).toFixed(1)}% of leads` : ""),
    tile("Sample / order", fmt(order), `${fmt(r.won)} delivered`),
    tile("Median first reply", resp.median_first_reply_hours === null ? "–" : `${resp.median_first_reply_hours}h`, "from the customer's first message"),
    tile("Replied within 1h", pct(resp.within_1h), `${pct(resp.within_24h)} within 24h`),
    tile("Waiting for us", fmt(resp.awaiting_reply), `${fmt(resp.never_answered)} never got a personal reply`),
  ].join("");
}

function renderFunnel(r) {
  const max = Math.max(1, r.steps[0].reached);
  $("s-funnel").innerHTML = r.steps.map((s) => `
    <div class="name">${esc(s.label)}</div>
    <div class="track clickable" onclick="showLeads({stage: '${s.stage}'}, '${esc(s.label)}')"
         data-tip="${esc(s.label)}: ${fmt(s.reached)} leads${s.conversion !== null ? ` · ${s.conversion}% of the previous stage` : ""}">
      <div class="fill" style="width:${(100 * s.reached / max).toFixed(2)}%"></div></div>
    <div class="val">${fmt(s.reached)}${s.conversion !== null ? ` <span class="muted">${s.conversion}%</span>` : ""}</div>`).join("");
}

function renderDropoffs(r) {
  $("s-lost-sub").textContent = `${fmt(r.lost)} lost leads (silent ${r.lost_after_days}+ days or declined) · click a reason to see them`;
  const max = Math.max(1, ...r.dropoffs.map((d) => d.count));
  $("s-drop").innerHTML = r.dropoffs.map((d) => `
    <div class="name">${esc(d.label)}</div>
    <div class="track clickable" onclick="showLeads({stage: 'lost', reason: '${d.reason}'}, 'Lost: ${esc(d.label)}')"
         data-tip="${esc(d.label)}: ${fmt(d.count)} leads (${(100 * d.count / Math.max(1, r.lost)).toFixed(0)}% of lost)">
      <div class="fill" style="width:${(100 * d.count / max).toFixed(2)}%"></div></div>
    <div class="val">${fmt(d.count)}</div>`).join("") || `<div class="muted">No lost leads in this period.</div>`;
}

function niceMax(v) {
  const step = Math.pow(10, Math.floor(Math.log10(Math.max(v, 1))));
  return Math.ceil(v / step) * step;
}

function renderWeekly(weeks) {
  if (!weeks.length) { $("s-weekly").innerHTML = `<div class="muted">No leads yet.</div>`; return; }
  const W = 640, H = 180, padL = 34, padB = 22, padT = 8;
  const max = niceMax(Math.max(...weeks.map((w) => w.leads)));
  const band = (W - padL) / weeks.length, bw = Math.min(24, band * 0.6);
  const y = (v) => H - padB - (v / max) * (H - padB - padT);
  const ticks = [0, max / 2, max];
  const grid = ticks.map((t) => `<line class="gridline" x1="${padL}" x2="${W}" y1="${y(t)}" y2="${y(t)}"/>
    <text class="axis" x="${padL - 6}" y="${y(t) + 4}" text-anchor="end">${fmt(t)}</text>`).join("");
  const bars = weeks.map((w, i) => {
    const x = padL + i * band + (band - bw) / 2, top = y(w.leads), r = Math.min(4, (H - padB - top) / 2);
    const label = new Date(w.week).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
    const showLabel = weeks.length <= 8 || i % 2 === 0 || i === weeks.length - 1;
    return `<g data-tip="Week of ${label}: ${fmt(w.leads)} new leads">
      <rect class="hit" x="${padL + i * band}" y="${padT}" width="${band}" height="${H - padB - padT}"/>
      <path class="bar" d="M${x},${H - padB} V${top + r} q0,-${r} ${r},-${r} h${bw - 2 * r} q${r},0 ${r},${r} V${H - padB} Z"/>
      ${showLabel ? `<text class="axis" x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${label}</text>` : ""}</g>`;
  }).join("");
  $("s-weekly").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="New leads per week">${grid}${bars}</svg>`;
}

function rateCell(v) {
  return v === null ? "–" : `<span class="inline-bar" style="width:${Math.max(2, v * 0.8)}px"></span>${v}%`;
}

function renderSources(rows) {
  $("s-sources").innerHTML = `<table class="data"><thead><tr><th>Source</th><th class="num">Leads</th><th>Reach a quote</th><th>Reach an order</th></tr></thead>
    <tbody>${rows.map((s) => `<tr><td>${esc(s.label)}</td><td class="num">${fmt(s.leads)}</td><td>${rateCell(s.quote_rate)}</td><td>${rateCell(s.order_rate)}</td></tr>`).join("")}</tbody></table>`;
}

// ---- pipeline board ----

async function loadBoard() {
  const leads = await api("/api/funnel/leads?" + salesParams()).catch(() => []);
  const open = leads.filter((l) => OPEN_STAGES.includes(l.stage));
  $("s-board").innerHTML = OPEN_STAGES.map((stage) => {
    const items = open.filter((l) => l.stage === stage);
    return `<div class="lane" data-stage="${stage}" ondragover="event.preventDefault(); this.classList.add('drop')"
                 ondragleave="this.classList.remove('drop')" ondrop="dropLead(event, '${stage}')">
      <h3><span>${STAGE_NAMES[stage]}</span><span class="muted">${items.length}</span></h3>
      ${items.slice(0, 40).map(leadCard).join("")}
      ${items.length > 40 ? `<div class="muted" style="font-size:12px">+${items.length - 40} more</div>` : ""}</div>`;
  }).join("");
}

function stageSelect(lead) {
  const opts = Object.entries(STAGE_NAMES).map(([k, v]) => `<option value="${k}" ${k === lead.stage ? "selected" : ""}>${v}</option>`).join("");
  return `<select onchange="setStage('${esc(lead.conversation_id)}', this.value)" title="Change stage">${opts}
    ${lead.overridden ? `<option value="auto">Auto (${STAGE_NAMES[lead.reached]})</option>` : ""}</select>`;
}

function leadCard(l) {
  const days = Math.max(0, Math.round((Date.now() - new Date(l.last_at)) / 86400000));
  return `<div class="lead" draggable="true" ondragstart="event.dataTransfer.setData('text/plain', '${esc(l.conversation_id)}')">
    <div class="row"><span class="who">@${esc(l.username)}</span>${l.awaiting_reply ? `<span class="pill high">waiting</span>` : ""}
      ${l.segment !== "unknown" ? `<span class="pill">${l.segment}</span>` : ""}</div>
    <div class="last">${l.last_from === "them" ? "Them" : "Us"}: ${esc(l.last_text.slice(0, 90)) || "<em>attachment</em>"}</div>
    <div class="row"><span class="muted">${days === 0 ? "today" : `${days}d ago`}</span><span class="spacer"></span>${stageSelect(l)}</div></div>`;
}

async function dropLead(event, stage) {
  event.preventDefault();
  event.currentTarget.classList.remove("drop");
  await setStage(event.dataTransfer.getData("text/plain"), stage);
}

async function setStage(conversationId, stage) {
  try {
    await api(`/api/funnel/${encodeURIComponent(conversationId)}/stage`, { method: "POST", body: { stage: stage === "auto" ? null : stage } });
    toast(stage === "auto" ? "Back to the automatic stage." : `Moved to ${STAGE_NAMES[stage]}.`);
  } catch (e) { toast(e.message); }
  loadSales();
}

async function showLeads(filter, title) {
  const leads = await api("/api/funnel/leads?" + salesParams(filter)).catch((e) => { toast(e.message); return []; });
  $("s-list-panel").hidden = false;
  $("s-list-title").textContent = `${title} (${leads.length >= 200 ? "latest 200" : leads.length})`;
  $("s-list").innerHTML = `<table class="data"><thead><tr><th>Customer</th><th>Stage</th><th>Segment</th><th>Last message</th><th>Last activity</th></tr></thead><tbody>
    ${leads.map((l) => `<tr><td><a href="https://ig.me/m/${esc(l.username)}" target="_blank" rel="noopener">@${esc(l.username)}</a></td>
      <td>${stageSelect(l)}</td><td>${esc(l.segment)}</td><td>${l.last_from === "them" ? "Them" : "Us"}: ${esc(l.last_text.slice(0, 120))}</td>
      <td class="muted">${new Date(l.last_at).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}</td></tr>`).join("")}</tbody></table>`;
  $("s-list-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeLeadList() { $("s-list-panel").hidden = true; }

// ---- strategist ----

const PRESETS = [
  "What are the 3 biggest leaks in my funnel and how do I fix them?",
  "How can I recover leads lost because the brand has to provide fabric?",
  "Rewrite my reply to the 'bulk pricing' button so more people send a reference image.",
  "Which lead source should I invest more in, and how?",
];

function renderPresets() {
  $("s-presets").innerHTML = PRESETS.map((p, i) => `<button class="chip-toggle" onclick="$('s-question').value = PRESETS[${i}]">${esc(p)}</button>`).join("");
}

function renderStrategistChat() {
  $("s-chat").innerHTML = sales.history.map((t) => `<div class="bubble ${t.role}">${t.role === "assistant" ? renderMarkdown(t.content) : esc(t.content)}</div>`).join("");
}

async function askStrategist() {
  const question = $("s-question").value.trim();
  if (!question) { toast("Type a question or pick one above."); return; }
  try {
    const est = await api("/api/strategy/estimate", { method: "POST", body: { question, history: sales.history, days: +$("s-days").value } });
    sales.pending = question;
    $("cost-text").textContent = `This sends your funnel numbers and a few anonymised lost chats (~${fmt(est.input_tokens)} tokens) to Claude. Estimated cost: ${est.usd === null ? "unknown" : "$" + est.usd.toFixed(3)}.`;
    $("cost-dialog").showModal();
  } catch (e) { toast(e.message); }
}

async function confirmStrategist() {
  $("cost-dialog").close();
  const question = sales.pending;
  const btn = $("s-ask"); btn.disabled = true; btn.textContent = "Thinking…";
  try {
    const r = await api("/api/strategy", { method: "POST", body: { question, history: sales.history, days: +$("s-days").value, confirm: true } });
    sales.history.push({ role: "user", content: question }, { role: "assistant", content: r.answer });
    $("s-question").value = "";
    renderStrategistChat();
  } catch (e) { toast(e.message); }
  btn.disabled = false; btn.textContent = "Ask";
}

function resetStrategist() { sales.history = []; renderStrategistChat(); }

loaders.sales = loadSales;
