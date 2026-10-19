// Chat module: the reply inbox (queue, filters, bulk actions, send confirmation).
const state = { items: [], faqs: [], selected: new Set(), priority: new Set(), openChat: new Set() };




function togglePriority(el) {
  el.classList.toggle("on");
  const p = el.dataset.priority;
  state.priority.has(p) ? state.priority.delete(p) : state.priority.add(p);
  loadChat();
}

function filterParams() {
  const p = new URLSearchParams();
  if (state.priority.size) p.set("priority", [...state.priority].join(","));
  if ($("closing").classList.contains("on")) p.set("max_hours_left", "4");
  for (const [id, key] of [["f-category", "category"], ["f-type", "customer_type"], ["f-status", "status"], ["f-q", "q"]]) {
    if ($(id).value) p.set(key, $(id).value);
  }
  return p;
}

let loadTimer;
function debouncedLoadChat() { clearTimeout(loadTimer); loadTimer = setTimeout(loadChat, 250); }

async function loadChat() {
  try {
    [state.items, state.faqs] = await Promise.all([api("/api/queue?" + filterParams()), api("/api/faqs")]);
  } catch (e) { toast("Couldn't load the inbox: " + e.message); return; }
  const visible = new Set(state.items.map((i) => i.draft_id));
  for (const id of [...state.selected]) if (!visible.has(id)) state.selected.delete(id);
  renderFilters(); renderQueue(); loadStats();
}

async function loadStats() {
  const s = await api("/api/stats").catch(() => null);
  if (!s) return;
  const sync = s.sync.running ? ` · syncing… (${s.sync.conversations} chats)` : s.sync.last_error ? ` · last sync failed: ${esc(s.sync.last_error)}` : "";
  $("stats").innerHTML = `<b>${s.open}</b> to reply · <b>${s.high}</b> high · <b>${s.closing_soon}</b> closing soon · <b>${s.sent_24h}</b> sent today${sync}`;
  $("sync-btn").disabled = s.sync.running;
  $("chat-badge").hidden = !s.open; $("chat-badge").textContent = s.open;
  if (s.sync.running) setTimeout(() => !$("chat").hidden && loadChat(), 4000);
}

function faqName(key) {
  const f = state.faqs.find((f) => f.key === key);
  return f ? f.question : { reference_image: "Sent an image", greeting: "Just said hi", other: "Other question" }[key] || key;
}

function renderFilters() {
  const cat = $("f-category"), current = cat.value;
  const keys = [...new Set([...state.faqs.map((f) => f.key), "reference_image", "greeting", "other"])];
  cat.innerHTML = `<option value="">All topics</option>` + keys.map((k) => `<option value="${esc(k)}">${esc(faqName(k))}</option>`).join("");
  cat.value = current;
  const bulk = $("bulk-faq");
  bulk.innerHTML = `<option value="">Use FAQ answer…</option>` + state.faqs.map((f) => `<option value="${esc(f.key)}">${esc(f.question)}</option>`).join("");
}

function hoursText(h) {
  if (h <= 0) return "window closed";
  return h < 1 ? `${Math.round(h * 60)} min left` : `${h.toFixed(h < 10 ? 1 : 0)}h left`;
}

function renderQueue() {
  const ready = state.items.filter((i) => i.status === "pending" && i.faq_approved);
  $("faq-banner").hidden = !ready.length;
  $("faq-banner-text").textContent = `${ready.length} ${ready.length === 1 ? "reply uses" : "replies use"} an approved FAQ answer and ${ready.length === 1 ? "is" : "are"} ready to approve.`;
  if (!state.items.length) {
    $("queue").innerHTML = `<div class="empty">Nothing waiting. Press “Check for new messages” to look again.</div>`;
  } else {
    $("queue").innerHTML = state.items.map(card).join("");
  }
  const approved = state.items.filter((i) => i.status === "approved").length;
  $("send-btn").textContent = `Send approved (${approved})`;
  $("send-btn").disabled = !approved;
  $("sel-count").textContent = state.selected.size ? `${state.selected.size} selected` : "Select all";
  $("select-all").checked = state.items.length > 0 && state.selected.size === state.items.length;
  document.querySelectorAll("[data-needs-selection]").forEach((el) => (el.disabled = !state.selected.size));
}

function card(i) {
  const said = i.customer_messages.map(esc).join("\n") + (i.sent_attachment ? `\n<span class="att">[sent an image / attachment]</span>` : "");
  const chat = state.openChat.has(i.draft_id) ? `<div class="transcript" id="chat-${i.draft_id}">Loading…</div>` : "";
  return `<div class="card ${i.status === "approved" ? "approved" : ""}">
    <input type="checkbox" ${state.selected.has(i.draft_id) ? "checked" : ""} onchange="toggleSelect(${i.draft_id}, this.checked)">
    <div>
      <div class="row">
        <span class="who"><a href="https://ig.me/m/${esc(i.username)}" target="_blank" rel="noopener">@${esc(i.username)}</a></span>
        <span class="pill ${i.priority}">${i.priority}</span>
        <span class="pill">${esc(faqName(i.category))}</span>
        ${i.customer_type !== "unknown" ? `<span class="pill">${i.customer_type === "brand" ? "clothing brand" : "personal"}</span>` : ""}
        ${i.status === "approved" ? `<span class="pill ok">approved</span>` : ""}
        <span class="spacer"></span>
        <span class="time ${i.hours_left <= 4 ? "urgent" : ""}">${hoursText(i.hours_left)}</span>
      </div>
      <div class="said">${said || '<span class="att">(no text)</span>'}</div>
      ${i.reasons.length ? `<div class="reasons">Why: ${esc(i.reasons.join(" · "))}</div>` : ""}
      ${i.error ? `<div class="reasons" style="color:var(--high)">${esc(i.error)}</div>` : ""}
      <textarea id="body-${i.draft_id}" placeholder="Write a reply, pick an FAQ answer, or ask Claude for a draft">${esc(i.body)}</textarea>
      <div class="actions">
        ${i.status === "approved" ? `<button class="btn" onclick="saveDraft(${i.draft_id})">Save changes</button>`
          : `<button class="btn primary" onclick="approveOne(${i.draft_id})">Approve</button>`}
        <button class="btn" onclick="aiDraft(${i.draft_id}, this)">Draft with Claude</button>
        <button class="btn ghost" onclick="toggleChat(${i.draft_id}, '${esc(i.conversation_id)}')">${state.openChat.has(i.draft_id) ? "Hide chat" : "Show chat"}</button>
        <button class="btn ghost" onclick="rejectOne(${i.draft_id})">Dismiss</button>
      </div>
      ${chat}
    </div>
  </div>`;
}

function toggleSelect(id, on) { on ? state.selected.add(id) : state.selected.delete(id); renderQueue(); }
function selectAll(on) { state.selected = new Set(on ? state.items.map((i) => i.draft_id) : []); renderQueue(); }

async function toggleChat(id, conversationId) {
  if (state.openChat.has(id)) { state.openChat.delete(id); renderQueue(); return; }
  state.openChat.add(id); renderQueue();
  const msgs = await api(`/api/conversations/${encodeURIComponent(conversationId)}`).catch(() => []);
  const el = $(`chat-${id}`);
  if (el) el.innerHTML = msgs.map((m) => `<div class="msg ${m.from}">${m.text ? esc(m.text) : '<span class="att">[attachment]</span>'}</div>`).join("") || "No messages stored.";
}

async function saveIfChanged(id) {
  const item = state.items.find((i) => i.draft_id === id);
  const body = $(`body-${id}`).value;
  if (item && body.trim() !== (item.body || "").trim()) await api(`/api/drafts/${id}`, { method: "POST", body: { body } });
}

async function approveOne(id) {
  try {
    await saveIfChanged(id);
    const r = await api("/api/drafts/approve", { method: "POST", body: { ids: [id] } });
    if (r.refused[id]) toast(r.refused[id]);
  } catch (e) { toast(e.message); }
  loadChat();
}

async function saveDraft(id) {
  try { await saveIfChanged(id); toast("Saved - approve it again before sending."); } catch (e) { toast(e.message); }
  loadChat();
}

async function rejectOne(id) { await api("/api/drafts/reject", { method: "POST", body: { ids: [id] } }).catch((e) => toast(e.message)); loadChat(); }

async function aiDraft(id, btn) {
  btn.disabled = true; btn.textContent = "Drafting…";
  try { await api(`/api/drafts/${id}/ai`, { method: "POST" }); } catch (e) { toast(e.message); }
  loadChat();
}

async function bulkApprove() {
  const ids = [...state.selected];
  for (const id of ids) await saveIfChanged(id).catch(() => {});
  const r = await api("/api/drafts/approve", { method: "POST", body: { ids } }).catch((e) => toast(e.message));
  const refused = r ? Object.keys(r.refused).length : 0;
  toast(`Approved ${ids.length - refused}` + (refused ? ` · ${refused} still need a reply written` : ""));
  state.selected.clear(); loadChat();
}

async function bulkApplyFaq(sel) {
  if (!sel.value) return;
  await api("/api/drafts/apply-faq", { method: "POST", body: { ids: [...state.selected], faq_key: sel.value } }).catch((e) => toast(e.message));
  toast("FAQ answer filled in - review and approve.");
  sel.value = ""; loadChat();
}

async function bulkReject() {
  await api("/api/drafts/reject", { method: "POST", body: { ids: [...state.selected] } }).catch((e) => toast(e.message));
  state.selected.clear(); loadChat();
}

async function approveFaqReplies() {
  const p = filterParams(); p.delete("status");
  const r = await api("/api/drafts/approve-faq?" + p, { method: "POST" }).catch((e) => toast(e.message));
  if (r) toast(`Approved ${r.approved.length} FAQ replies. Check them, then press Send.`);
  loadChat();
}

function openSend() {
  const items = state.items.filter((i) => i.status === "approved" && (!state.selected.size || state.selected.has(i.draft_id))).slice(0, 25);
  if (!items.length) { toast("Nothing approved in this view."); return; }
  $("send-title").textContent = `Send ${items.length} ${items.length === 1 ? "reply" : "replies"} on Instagram?`;
  $("send-list").innerHTML = items.map((i) => `<div class="confirm-item"><b>@${esc(i.username)}</b> <span class="time">${hoursText(i.hours_left)}</span><div class="said">${esc(i.body)}</div></div>`).join("");
  $("send-confirm").dataset.ids = JSON.stringify(items.map((i) => i.draft_id));
  $("send-confirm").textContent = `Send ${items.length}`;
  $("send-dialog").showModal();
}

async function confirmSend() {
  const btn = $("send-confirm"); btn.disabled = true; btn.textContent = "Sending…";
  try {
    const r = await api("/api/drafts/send", { method: "POST", body: { ids: JSON.parse(btn.dataset.ids), confirm: true } });
    const problems = [...Object.entries(r.blocked), ...Object.entries(r.failed)];
    toast(`Sent ${r.sent.length}.` + (problems.length ? ` Not sent: ${problems.map(([, why]) => why).join("; ")}` : ""));
  } catch (e) { toast(e.message); }
  btn.disabled = false; $("send-dialog").close(); state.selected.clear(); loadChat();
}

async function sync() {
  try { await api("/api/sync", { method: "POST" }); toast("Checking Instagram for new messages…"); } catch (e) { toast(e.message); }
  setTimeout(loadStats, 500);
}

loaders.chat = loadChat;
