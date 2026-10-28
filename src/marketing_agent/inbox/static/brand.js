// Settings › Brand profile: edit brand.md, keep versions, and tune it with the model.
const BRAND_PRESETS = [
  "Make the Voice section simpler and more humble, the way a solo founder writes.",
  "Check the whole profile for anything that sounds like a big company or overclaims, and tone it down.",
  "Shorten it: remove repetition so it costs fewer tokens, without losing any facts.",
  "List what's still under Needs confirmation as short questions I can answer.",
];
const brandState = { saved: "", history: [], pending: null, proposal: null };

async function loadBrand() {
  let s;
  try { s = await api("/api/brand"); } catch (e) { $("st-brand-panel").hidden = true; return; }
  $("st-brand-panel").hidden = false;
  brandState.saved = s.content;
  if (!brandDirty()) $("st-brand").value = s.content;
  $("st-brand-meta").textContent = s.exists ? `${s.path} · ~${fmt(s.est_tokens)} tokens sent with every draft` : `${s.path} doesn't exist yet`;
  $("st-brand-draft-banner").hidden = !(s.is_template && s.draft_available);
  $("st-brand-draft-btn").hidden = !s.draft_available || s.is_template;
  $("st-brand-versions").innerHTML = s.versions.length
    ? s.versions.map((v) => `<option value="${v.id}">v${v.id} · ${new Date(v.created_at).toLocaleDateString("en-IN", { day: "numeric", month: "short" })} · ${esc(v.created_by === "ai" ? "AI: " : "")}${esc((v.note || "").slice(0, 40))}</option>`).join("")
    : `<option value="">No saved versions yet</option>`;
  $("st-brand-presets").innerHTML = BRAND_PRESETS.map((p, i) => `<button class="chip-toggle" onclick="$('st-brand-message').value = BRAND_PRESETS[${i}]">${esc(p)}</button>`).join("");
  brandEdited();
}

const brandDirty = () => $("st-brand").value !== brandState.saved && $("st-brand").value.trim() !== "";

function brandEdited() {
  const dirty = $("st-brand").value !== brandState.saved;
  $("st-brand-dirty").textContent = dirty ? `Unsaved changes · ~${fmt(Math.round($("st-brand").value.length / 4))} tokens` : "Saved";
  $("st-brand-revert").hidden = !dirty;
}

function revertBrandEditor() { $("st-brand").value = brandState.saved; brandState.aiNote = null; brandEdited(); }

async function saveBrand() {
  // Text that came from an AI proposal is labelled as such in the version history.
  const note = brandState.aiNote || "edited in Settings", createdBy = brandState.aiNote ? "ai" : "user";
  try {
    const r = await api("/api/brand", { method: "POST", body: { content: $("st-brand").value, note, created_by: createdBy } });
    toast(`Saved as version ${r.version}. New drafts use it now.`);
    brandState.aiNote = null;
    brandState.saved = "";            // reload from disk, normalised
    $("st-brand").value = "";
    await loadBrand();
  } catch (e) { toast(e.message); }
}

async function loadBrandDraft() {
  if (brandDirty() && !confirm("Replace the editor's unsaved text with brand.draft.md?")) return;
  try {
    $("st-brand").value = (await api("/api/brand/draft")).content;
    brandEdited();
    toast("Draft loaded. Review it (especially Needs confirmation), then Save.");
  } catch (e) { toast(e.message); }
}

async function viewBrandVersion() {
  const id = $("st-brand-versions").value;
  if (!id) return;
  if (brandDirty() && !confirm("Replace the editor's unsaved text with this version?")) return;
  try {
    $("st-brand").value = (await api(`/api/brand/versions/${id}`)).content;
    brandEdited();
    toast(`Version ${id} is in the editor. Save to make it current.`);
  } catch (e) { toast(e.message); }
}

// ---- tune with AI ----

function renderBrandTuner() {
  $("st-brand-chat").innerHTML = brandState.history.map((t) => `<div class="bubble ${t.role}">${esc(t.content)}</div>`).join("");
  const p = brandState.proposal;
  $("st-brand-proposal").hidden = !p;
  if (p) $("st-brand-proposal").innerHTML = `<b>Proposed changes</b><ul>${p.changes.map((c) => `<li>${esc(c)}</li>`).join("") || "<li>See the revised text</li>"}</ul>
    <div class="row"><button class="btn primary" onclick="useBrandProposal()">Put it in the editor</button>
      <button class="btn ghost" onclick="brandState.proposal = null; renderBrandTuner()">Discard</button></div>`;
}

async function askBrandTuner() {
  const message = $("st-brand-message").value.trim();
  if (!message) { toast("Say what you'd like to change, or pick a suggestion."); return; }
  try {
    const est = await api("/api/brand/tune/estimate", { method: "POST", body: { content: $("st-brand").value, message, history: brandState.history } });
    brandState.pending = message;
    askCost(costText("your brand profile", est), confirmBrandTuner);
  } catch (e) { toast(e.message); }
}

async function confirmBrandTuner() {
  const message = brandState.pending;
  const btn = $("st-brand-ask"); btn.disabled = true; btn.textContent = "Thinking…";
  try {
    const r = await api("/api/brand/tune", { method: "POST", body: { content: $("st-brand").value, message, history: brandState.history, confirm: true } });
    brandState.history.push({ role: "user", content: message }, { role: "assistant", content: r.reply || "Done." });
    brandState.proposal = r.profile ? { profile: r.profile, changes: r.changes } : null;
    $("st-brand-message").value = "";
    renderBrandTuner();
  } catch (e) { toast(e.message); }
  btn.disabled = false; btn.textContent = "Ask";
}

function useBrandProposal() {
  $("st-brand").value = brandState.proposal.profile;
  brandState.aiNote = (brandState.proposal.changes.join("; ") || "AI revision").slice(0, 200);
  brandState.proposal = null;
  brandEdited();
  renderBrandTuner();
  toast("The revised profile is in the editor. Read it through, then Save.");
}

function resetBrandTuner() { brandState.history = []; brandState.proposal = null; renderBrandTuner(); }
