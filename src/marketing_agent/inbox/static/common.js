// Shared helpers: API calls, escaping, toasts, tooltips and module routing.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString("en-IN"));
const pct = (n) => (n === null || n === undefined ? "–" : `${n}%`);

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : res.statusText);
  return data;
}

function toast(text) {
  const el = document.createElement("div");
  el.className = "toast"; el.textContent = text; document.body.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

// Hover tooltips for any element with data-tip (charts and bars).
document.addEventListener("mousemove", (e) => {
  const target = e.target.closest("[data-tip]");
  const tip = $("tooltip");
  if (!target) { tip.hidden = true; return; }
  tip.textContent = target.dataset.tip; tip.hidden = false;
  const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  tip.style.left = `${x}px`; tip.style.top = `${e.clientY + 14}px`;
});

// Modules and their sub-pages: go("marketing/prospecting") opens Marketing on its Prospecting tab.
const loaders = {};
function go(target) {
  let [name, sub] = target.split("/");
  if (!document.getElementById(name)) { name = "chat"; sub = undefined; }
  document.querySelectorAll("section[data-title]").forEach((s) => (s.hidden = s.id !== name));
  document.querySelectorAll(".nav").forEach((n) => n.classList.toggle("active", n.dataset.module === name));
  document.title = `${$(name).dataset.title} · Custom Closet`;
  history.replaceState(null, "", `#${sub ? `${name}/${sub}` : name}`);
  loaders[name]?.(sub);
}

// Every model call shows its estimated cost first; onConfirm runs only after "Ask".
let costAction = null;
function askCost(text, onConfirm) {
  $("cost-text").textContent = text;
  costAction = onConfirm;
  $("cost-dialog").showModal();
}
function runCostAction() {
  $("cost-dialog").close();
  const action = costAction; costAction = null;
  action?.();
}
function costText(what, est) {
  return `This sends ${what} (~${fmt(est.input_tokens)} tokens) to the model. Estimated cost: ${est.usd === null ? "unknown (billed by AWS)" : "$" + est.usd.toFixed(3)}.`;
}

// Minimal, safe Markdown for strategist answers: headings, bullets, bold, paragraphs.
function renderMarkdown(md) {
  const lines = esc(md).split("\n");
  let html = "", inList = false;
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    if (/^\s*[-*] /.test(line)) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${line.replace(/^\s*[-*] /, "")}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (/^#{1,6} /.test(line)) html += `<h4>${line.replace(/^#{1,6} /, "")}</h4>`;
    else if (line.trim()) html += `<p>${line}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}
