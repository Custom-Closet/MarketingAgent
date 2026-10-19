// Settings module: status, drafting/funnel settings and the FAQ library.
let settingsFaqs = [];

async function loadSettings() {
  try {
    const [s, faqs] = await Promise.all([api("/api/settings"), api("/api/faqs")]);
    settingsFaqs = faqs;
    $("st-window").value = s.context_window_days;
    $("st-lost").value = s.lost_after_days;
    const ok = (on, yes, no) => `<span class="pill ${on ? "ok" : "high"}">${on ? yes : no}</span>`;
    const a = s.archive, sync = s.status.sync;
    $("st-status").innerHTML = [
      `<li>${ok(s.status.instagram, "connected", "not set")} Instagram (IG_ACCESS_TOKEN in .env)</li>`,
      `<li>${ok(s.status.claude_drafts, "on", "off")} AI reply drafts (model + brand.md)</li>`,
      `<li>${ok(s.status.strategist, "on", "off")} Funnel strategist</li>`,
      `<li><span class="pill">${fmt(a.conversations)} chats · ${fmt(a.messages)} messages</span> Archive, newest message ${a.newest_message ? new Date(a.newest_message).toLocaleString("en-IN") : "–"}</li>`,
      `<li><span class="pill">${fmt(a.summaries)}</span> Chats with a compressed summary</li>`,
      `<li><span class="pill">${sync.running ? "running" : sync.last_finished ? "done" : "not yet"}</span> Last sync ${sync.last_finished ? new Date(sync.last_finished).toLocaleString("en-IN") : ""}${sync.last_error ? ` – failed: ${esc(sync.last_error)}` : ""}</li>`,
    ].join("");
    renderFaqs();
    loadModels(false);
  } catch (e) { toast("Couldn't load settings: " + e.message); }
}

// ---- model picker (Bedrock) ----

async function loadModels(refresh) {
  const m = await api("/api/models" + (refresh ? "?refresh=true" : "")).catch((e) => ({ error: e.message, models: [] }));
  $("st-model-panel").hidden = m.provider !== "bedrock";
  if (m.provider !== "bedrock") return;
  $("st-model-current").textContent = `In use: ${m.current || "none yet"} · "Low cost" marks cheaper model families; see Bedrock pricing for exact rates.`;
  const options = m.models.map((x) => `<option value="${esc(x.id)}" ${x.id === m.current ? "selected" : ""}>${x.low_cost ? "★ " : ""}${esc(x.id)}${x.low_cost ? " (low cost)" : ""}</option>`);
  if (m.current && !m.models.some((x) => x.id === m.current)) options.unshift(`<option value="${esc(m.current)}" selected>${esc(m.current)}</option>`);
  $("st-model").innerHTML = options.join("") || `<option value="">No models listed</option>`;
  $("st-model-result").textContent = m.error ? `Couldn't list models: ${m.error}` : "";
}

function showModelResult(r) {
  $("st-model-result").textContent = r.ok ? `✓ ${r.model} works (replied "${r.reply}" in ${r.seconds}s)` : `✗ ${r.model}: ${r.error}`;
}

async function testModel() {
  $("st-model-result").textContent = "Testing…";
  try { showModelResult(await api("/api/models/test", { method: "POST", body: { model_id: $("st-model").value } })); }
  catch (e) { $("st-model-result").textContent = e.message; }
}

async function useModel() {
  $("st-model-result").textContent = "Testing, then switching…";
  try {
    const r = await api("/api/models/select", { method: "POST", body: { model_id: $("st-model").value } });
    showModelResult(r);
    if (r.ok) { toast(`Now drafting with ${r.model}`); loadSettings(); }
  } catch (e) { $("st-model-result").textContent = e.message; }
}

async function saveSettings() {
  try {
    await api("/api/settings", { method: "POST", body: { context_window_days: +$("st-window").value, lost_after_days: +$("st-lost").value } });
    toast("Saved.");
  } catch (e) { toast(e.message); }
}

function renderFaqs() {
  $("faq-list").innerHTML = settingsFaqs.map((f) => `<div class="faq">
    <div class="row"><h3>${esc(f.question)}</h3><span class="spacer"></span>
      ${f.approved ? `<span class="pill ok">approved</span> <button class="btn ghost" onclick="setFaqApproved('${esc(f.key)}', false)">Unapprove</button>`
        : `<button class="btn primary" onclick="setFaqApproved('${esc(f.key)}', true)">Approve</button>`}</div>
    <div class="kw">Matches: ${esc(f.keywords.join(", "))}</div>
    <textarea id="faq-${esc(f.key)}">${esc(f.answer)}</textarea>
    <div class="actions"><button class="btn" onclick="saveFaq('${esc(f.key)}')">Save answer</button></div>
  </div>`).join("") || `<div class="empty">No FAQs yet. Add them to faqs.json and restart.</div>`;
}

async function setFaqApproved(key, approved) {
  await api(`/api/faqs/${encodeURIComponent(key)}`, { method: "POST", body: { approved } }).catch((e) => toast(e.message));
  settingsFaqs = await api("/api/faqs"); renderFaqs();
}

async function saveFaq(key) {
  await api(`/api/faqs/${encodeURIComponent(key)}`, { method: "POST", body: { answer: $(`faq-${key}`).value } }).catch((e) => toast(e.message));
  toast("Saved - approve it again to use it in bulk.");
  settingsFaqs = await api("/api/faqs"); renderFaqs();
}

loaders.settings = loadSettings;
