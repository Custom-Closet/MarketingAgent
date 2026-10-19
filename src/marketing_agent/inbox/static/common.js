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

const loaders = {};
function go(name) {
  if (!document.getElementById(name)) name = "chat";
  document.querySelectorAll("section[data-title]").forEach((s) => (s.hidden = s.id !== name));
  document.querySelectorAll(".nav").forEach((n) => n.classList.toggle("active", n.dataset.module === name));
  document.title = `${$(name).dataset.title} · Custom Closet`;
  history.replaceState(null, "", `#${name}`);
  loaders[name]?.();
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
