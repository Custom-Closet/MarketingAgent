// Marketing module, Overview tab: outreach template performance and lead-source quality.
async function loadMarketing() {
  let m;
  try { m = await api("/api/marketing?days=" + $("m-days").value); } catch (e) { toast("Couldn't load marketing: " + e.message); return; }
  $("m-tiles").innerHTML = [
    tile("Outreach sent", fmt(m.sent), "conversations we started"),
    tile("Real replies", fmt(m.real_replies), `${pct(m.reply_rate)} reply rate`),
    tile("Reached a quote", fmt(m.quoted), "from outreach"),
    tile("Reached an order", fmt(m.orders), "from outreach"),
  ].join("");
  $("m-templates").innerHTML = `<table class="data"><thead><tr><th>Opening message</th><th class="num">Sent</th>
      <th class="num">Real replies</th><th>Reply rate</th><th class="num">Quoted</th><th class="num">Orders</th></tr></thead><tbody>
    ${m.templates.map((t) => `<tr><td data-tip="${esc(t.template)}">${esc(t.template.slice(0, 70))}${t.template.length > 70 ? "…" : ""}</td>
      <td class="num">${fmt(t.sent)}</td><td class="num">${fmt(t.real_replies)}</td><td>${rateCell(t.reply_rate)}</td>
      <td class="num">${fmt(t.quoted)}</td><td class="num">${fmt(t.orders)}</td></tr>`).join("")}</tbody></table>`;
  $("m-sources").innerHTML = `<table class="data"><thead><tr><th>Source</th><th class="num">Leads</th><th>Reach a quote</th><th>Reach an order</th><th class="num">Delivered</th></tr></thead><tbody>
    ${m.sources.map((s) => `<tr><td>${esc(s.label)}</td><td class="num">${fmt(s.leads)}</td><td>${rateCell(s.quote_rate)}</td>
      <td>${rateCell(s.order_rate)}</td><td class="num">${fmt(s.won)}</td></tr>`).join("")}</tbody></table>`;
}

// Marketing has sub-pages (Overview, Prospecting); controls marked data-sub-only show on one of them.
function showMarketing(sub) {
  sub = sub === "prospecting" ? "prospecting" : "overview";
  document.querySelectorAll("#marketing .subpage").forEach((p) => (p.hidden = p.dataset.sub !== sub));
  document.querySelectorAll("#marketing .tab").forEach((t) => t.classList.toggle("active", t.dataset.sub === sub));
  document.querySelectorAll("#marketing [data-sub-only]").forEach((el) => (el.hidden = el.dataset.subOnly !== sub));
  if (sub === "prospecting") loadProspecting(); else loadMarketing();
}

loaders.marketing = showMarketing;
