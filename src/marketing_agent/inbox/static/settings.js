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
      `<li>${ok(s.status.claude_drafts, "on", "off")} Draft with Claude (ANTHROPIC_API_KEY + brand.md)</li>`,
      `<li>${ok(s.status.strategist, "on", "off")} Funnel strategist</li>`,
      `<li><span class="pill">${fmt(a.conversations)} chats · ${fmt(a.messages)} messages</span> Archive, newest message ${a.newest_message ? new Date(a.newest_message).toLocaleString("en-IN") : "–"}</li>`,
      `<li><span class="pill">${fmt(a.summaries)}</span> Chats with a compressed summary</li>`,
      `<li><span class="pill">${sync.running ? "running" : sync.last_finished ? "done" : "not yet"}</span> Last sync ${sync.last_finished ? new Date(sync.last_finished).toLocaleString("en-IN") : ""}${sync.last_error ? ` – failed: ${esc(sync.last_error)}` : ""}</li>`,
    ].join("");
    renderFaqs();
  } catch (e) { toast("Couldn't load settings: " + e.message); }
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
