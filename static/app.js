"use strict";
const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const STATUS_TXT = { ok: "Todo bien", warning: "Avisos", alarm: "Alarma", idle: "Sin sol", nodata: "Sin datos" };
let state = null, selected = null;

const fmtW = (w) => w == null ? "–" : (Math.abs(w) >= 1000 ? (w / 1000).toFixed(1) + " kW" : Math.round(w) + " W");
const fmt = (v, d, u) => v == null ? "–" : v.toFixed(d) + (u ? " " + u : "");
const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

function renderHeader(s) {
  $("plantName").textContent = s.plant.name;
  $("simBadge").hidden = !s.simulated;
  const p = $("globalStatus");
  p.className = "pill " + s.status;
  p.textContent = STATUS_TXT[s.status] || s.status;
  if (s.snapshot) {
    const d = new Date(s.snapshot.ts);
    $("updated").textContent = "Lectura " + d.toLocaleString("es-ES", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  } else $("updated").textContent = "Esperando la primera lectura…";
  const dc = s.trackers.reduce((a, t) => a + (t.power_w || 0), 0);
  $("kAc").textContent = fmtW(s.snapshot && s.snapshot.ac_power_w);
  $("kDc").textContent = fmtW(dc);
  $("kImed").textContent = fmt(s.median_current_a, 2, "A");
  $("kDay").textContent = s.snapshot && s.snapshot.day_energy_kwh != null ? s.snapshot.day_energy_kwh.toFixed(0) + " kWh" : "–";
}

function renderPlan(s) {
  const svg = $("plan");
  svg.innerHTML = "";
  const CW = 130, CH = 168, PW = 104, PH = 100;
  const xs = s.trackers.map((t) => t.x), ys = s.trackers.map((t) => t.y);
  const minX = Math.min(...xs), minY = Math.min(...ys);
  const w = (Math.max(...xs) - minX + 1) * CW, h = (Math.max(...ys) - minY + 1) * CH;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  for (const t of s.trackers) {
    const gx = (t.x - minX) * CW + (CW - PW) / 2, gy = (t.y - minY) * CH + 8;
    const g = el("g", { class: "tracker" + (selected === t.id ? " sel" : ""), tabindex: 0, role: "button", "aria-label": "Seguidor " + t.id }, svg);
    el("rect", { x: gx - 4, y: gy - 4, width: PW + 8, height: PH + 8, rx: 6, class: "frame st-" + t.status }, g);
    // 30 paneles: 5 columnas x 6 filas, repartidas por igual entre los strings
    // (3 strings de 10 paneles -> 2 filas cada uno)
    const cols = 5, rows = 6, gap = 2, cw = (PW - gap * (cols - 1)) / cols, ch = (PH - gap * (rows - 1)) / rows;
    // cada fila pertenece a un string; S1 y S2 en paralelo comparten la misma entrada
    const perString = t.strings.flatMap((x) => Array(x.n_strings || 1).fill(x));
    const nStr = Math.max(1, perString.length);
    for (let r = 0; r < rows; r++) {
      const str = perString[Math.min(nStr - 1, Math.floor((r * nStr) / rows))];
      const cls = !str ? "" : str.status === "alarm" ? " dead" : str.status === "warning" ? " weak" : "";
      for (let c = 0; c < cols; c++)
        el("rect", { x: gx + c * (cw + gap), y: gy + r * (ch + gap), width: cw, height: ch, rx: 1.5, class: "cell" + cls }, g);
    }
    const label = el("text", { x: gx + PW / 2, y: gy + PH + 32, "font-size": 26 }, g);
    label.textContent = t.id;
    const kw = el("text", { x: gx + PW / 2, y: gy + PH + 56, "font-size": 20, class: "kw" }, g);
    kw.textContent = fmtW(t.power_w);
    const open = () => selectTracker(t.id);
    g.addEventListener("click", open);
    g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  }
}

function renderBars(s) {
  const box = $("bars");
  box.innerHTML = "";
  const all = s.trackers.flatMap((t) => t.strings);
  const max = Math.max(1, ...all.map((x) => x.current_per_string_a || 0)) * 1.05;
  const med = s.median_current_a;
  for (const x of all) {
    const row = document.createElement("div");
    row.className = "bar " + x.status;
    const pct = ((x.current_per_string_a || 0) / max) * 100;
    const medPct = med ? (med / max) * 100 : null;
    const dev = x.deviation_pct == null ? "" : (x.deviation_pct > 0 ? "+" : "") + x.deviation_pct.toFixed(0) + "%";
    row.innerHTML = `<span>${esc(x.tracker)} ${esc(x.label)}</span><span class="track"><span class="fill" style="width:${pct}%"></span>${medPct ? `<span class="med" style="left:${medPct}%"></span>` : ""}</span><span class="val">${fmt(x.current_per_string_a, 1, "A")} ${dev}</span>`;
    row.addEventListener("click", () => openTracker(x.tracker));
    box.appendChild(row);
  }
}

function renderAlerts(s) {
  const ul = $("alerts");
  ul.innerHTML = "";
  const items = s.trackers.filter((t) => t.messages.length).flatMap((t) => t.messages.map((m) => ({ t, m })));
  const cnt = $("alertCount");
  cnt.hidden = !items.length;
  cnt.textContent = items.length;
  cnt.className = "count" + (s.status === "warning" ? " warning" : "");
  if (!items.length) {
    ul.innerHTML = `<li>${s.active ? "Ningún aviso. Todos los strings producen parecido." : "Sin radiación suficiente para comparar."}</li>`;
    return;
  }
  for (const { t, m } of items) {
    const li = document.createElement("li");
    li.className = t.status;
    li.innerHTML = `<b>Seguidor ${esc(t.id)}</b> · ${esc(m)}`;
    li.addEventListener("click", () => openTracker(t.id));
    ul.appendChild(li);
  }
}

async function selectTracker(id, scroll = true) {
  selected = id;
  renderPlan(state);
  const t = state.trackers.find((x) => x.id === id);
  if (!t) return;
  $("detail").hidden = false;
  $("dTitle").textContent = `Seguidor ${t.id} · MPPT ${t.mppt} · ${fmtW(t.power_w)}`;
  $("dMsgs").innerHTML = t.messages.map((m) => `<div class="msg ${t.status}">${esc(m)}</div>`).join("");
  $("dStrings").innerHTML = t.strings.map((x) => `
    <div class="sbox ${x.status}"><b>${esc(x.label)}</b><small>Entrada PV${x.input ?? ""}${x.n_strings > 1 ? " · paralelo" : ""}</small>
      <div class="row"><span>I</span><span>${fmt(x.current_a, 2, "A")}</span></div>
      ${x.n_strings > 1 ? `<div class="row"><span>I/str</span><span>${fmt(x.current_per_string_a, 2, "A")}</span></div>` : ""}
      <div class="row"><span>V</span><span>${fmt(x.voltage_v, 0, "V")}</span></div>
      <div class="row"><span>P</span><span>${fmtW(x.power_w)}</span></div>
      <div class="row"><span>Δ</span><span>${x.deviation_pct == null ? "–" : x.deviation_pct.toFixed(1) + " %"}</span></div>
    </div>`).join("");
  const g = document.querySelector(`#plan .tracker[aria-label="Seguidor ${CSS.escape(id)}"]`);
  const wrap = document.querySelector(".plan-wrap");
  if (g && wrap) {
    const gr = g.getBoundingClientRect(), wr = wrap.getBoundingClientRect();
    if (gr.left < wr.left || gr.right > wr.right)
      wrap.scrollBy({ left: gr.left - wr.left - (wr.width - gr.width) / 2, behavior: scroll ? "smooth" : "auto" });
  }
  if (scroll) $("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const h = await getJSON(`/api/history?hours=18&tracker=${encodeURIComponent(id)}`);
    drawChart($("dChart"), h.series, t.strings.map((x) => x.string_id), Object.fromEntries(t.strings.map((x) => [x.string_id, x.n_strings || 1])));
  } catch (e) { console.error(e); }
}

function drawChart(svg, series, ids, nOf = {}) {
  // corriente por string, para que una entrada con 2 en paralelo sea comparable
  series = Object.fromEntries(Object.entries(series).map(([k, v]) => [k, v.map((p) => ({ ...p, i: p.i == null ? null : p.i / (nOf[k] || 1) }))]));
  svg.innerHTML = "";
  const W = 600, H = 160, L = 30, B = 18, T = 6;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const pts = ids.flatMap((id) => (series[id] || []).map((p) => ({ ...p, t: +new Date(p.ts) })));
  if (!pts.length) { const tx = el("text", { x: W / 2, y: H / 2, class: "axis", "text-anchor": "middle" }, svg); tx.textContent = "Sin histórico todavía"; return; }
  const t0 = Math.min(...pts.map((p) => p.t)), t1 = Math.max(...pts.map((p) => p.t)) || t0 + 1;
  const maxI = Math.max(1, ...pts.map((p) => p.i || 0)) * 1.1;
  const X = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - 6);
  const Y = (i) => T + (1 - i / maxI) * (H - T - B);
  for (let k = 0; k <= 4; k++) {
    const v = (maxI * k) / 4;
    el("line", { x1: L, x2: W, y1: Y(v), y2: Y(v), class: "grid" }, svg);
    const tx = el("text", { x: 2, y: Y(v) + 3, class: "axis" }, svg); tx.textContent = v.toFixed(0);
  }
  const fmtH = (t) => new Date(t).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
  [t0, (t0 + t1) / 2, t1].forEach((t, k) => { const tx = el("text", { x: X(t), y: H - 4, class: "axis", "text-anchor": ["start", "middle", "end"][k] }, svg); tx.textContent = fmtH(t); });
  const colors = ["var(--accent)", "var(--warning)", "var(--ok)"];
  ids.forEach((id, k) => {
    const s = series[id] || [];
    if (!s.length) return;
    const d = s.map((p, j) => (j ? "L" : "M") + X(+new Date(p.ts)).toFixed(1) + "," + Y(p.i || 0).toFixed(1)).join(" ");
    el("path", { d, fill: "none", stroke: colors[k % 3], "stroke-width": 2, "vector-effect": "non-scaling-stroke" }, svg);
    const tx = el("text", { x: L + 6 + k * 44, y: T + 10, class: "axis", fill: colors[k % 3] }, svg);
    tx.textContent = id.split("-").slice(1).join("-") || id; tx.style.fill = colors[k % 3];
  });
}

async function renderDaily() {
  try {
    const d = await getJSON("/api/daily?days=7");
    const tbl = $("daily");
    if (!d.days.length) { tbl.innerHTML = "<tr><td>Aún no hay días completos.</td></tr>"; return; }
    const days = d.days.slice(-7);
    let html = "<tr><th>String</th>" + days.map((x) => `<th>${x.day.slice(5)}</th>`).join("") + "</tr>";
    for (const sid of d.strings) {
      html += `<tr><td>${esc(sid)}</td>`;
      for (const day of days) {
        const vals = Object.values(day.kwh_per_kwp).sort((a, b) => a - b);
        const med = vals.length ? vals[Math.floor(vals.length / 2)] : 0;
        const v = day.kwh_per_kwp[sid];
        const ratio = med ? v / med : 1;
        const bg = v == null ? "" : ratio < 0.75 ? "var(--alarm)" : ratio < 0.9 ? "var(--warning)" : "";
        html += `<td style="${bg ? `background:color-mix(in srgb, ${bg} 30%, transparent)` : ""}">${v == null ? "–" : v.toFixed(2)}</td>`;
      }
      html += "</tr>";
    }
    tbl.innerHTML = html;
  } catch (e) { console.error(e); }
}

async function load() {
  try {
    state = await getJSON("/api/status");
    renderHeader(state); renderPlan(state); renderBars(state); renderAlerts(state);
    if (selected) selectTracker(selected, false);
    if (location.hash === "#rendimiento") renderDaily();
  } catch (e) {
    $("updated").textContent = "Error de conexión";
    console.error(e);
  }
}

const PAGES = ["plano", "strings", "avisos", "rendimiento"];
function showPage(name) {
  if (!PAGES.includes(name)) name = "plano";
  document.querySelectorAll(".page").forEach((p) => { p.hidden = p.dataset.page !== name; });
  document.querySelectorAll(".tabbar a").forEach((a) => a.classList.toggle("active", a.dataset.tab === name));
  if (name === "rendimiento") renderDaily();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", () => showPage(location.hash.slice(1)));
showPage(location.hash.slice(1));
function openTracker(id) {
  if (location.hash !== "#plano") { history.pushState(null, "", "#plano"); showPage("plano"); }
  selectTracker(id);
}

$("dClose").addEventListener("click", () => { selected = null; $("detail").hidden = true; renderPlan(state); });
$("refresh").addEventListener("click", (e) => { e.preventDefault(); load(); });
load();
setInterval(load, 60000);
