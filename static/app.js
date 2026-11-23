"use strict";
const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const LOC = "ca-ES";
const SEV_TXT = { warning: "Avís", alarm: "Alarma" };
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
  if (s.snapshot) {
    const d = new Date(s.snapshot.ts);
    $("updated").textContent = "Lectura " + d.toLocaleString(LOC, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  } else $("updated").textContent = "Esperant la primera lectura…";
  const e = s.energy || {};
  const kwh = (v) => v == null ? "–" : (v >= 10000 ? (v / 1000).toFixed(1) + " MWh" : Math.round(v).toLocaleString(LOC) + " kWh");
  $("kAc").textContent = fmtW(s.snapshot && s.snapshot.ac_power_w);
  $("kDay").textContent = kwh(e.today_kwh);
  $("kMonth").textContent = kwh(e.month_kwh);
  $("kYear").textContent = kwh(e.year_kwh);
}

function renderPlan(s) {
  const svg = $("plan");
  svg.innerHTML = "";
  // 5 x 2 en el móvil; en pantallas anchas, 10 x 1
  const cols = svg.clientWidth >= 760 ? 10 : 5;
  const CW = 130, CH = 168, PW = 104, PH = 100;
  s.trackers.forEach((t, k) => { t._c = k % cols; t._r = Math.floor(k / cols); });
  const w = cols * CW, h = Math.ceil(s.trackers.length / cols) * CH;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  for (const t of s.trackers) {
    const gx = t._c * CW + (CW - PW) / 2, gy = t._r * CH + 8;
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
  // Un fila por string. S1 y S2 van en paralelo en la misma entrada: el inversor
  // solo ve la suma, así que cada uno muestra la mitad (estimado).
  const box = $("bars");
  box.innerHTML = "";
  const rows = s.trackers.flatMap((t) => t.strings.flatMap((x) => {
    const names = (x.label || "").split("+");
    const n = Math.max(1, x.n_strings || names.length);
    return names.map((name) => ({
      ...x, name, shared: n > 1,
      p: x.power_w == null ? null : x.power_w / n,
      i: x.current_a == null ? null : x.current_a / n,
    }));
  }));
  const ps = rows.map((r) => r.p).filter((v) => v != null).sort((a, b) => a - b);
  const med = ps.length ? ps[Math.floor(ps.length / 2)] : null;
  const max = Math.max(1, ...ps) * 1.05;
  for (const x of rows) {
    const row = document.createElement("div");
    row.className = "bar " + x.status;
    const pct = ((x.p || 0) / max) * 100;
    const medPct = med ? (med / max) * 100 : null;
    const dev = x.deviation_pct == null ? "" : (x.deviation_pct > 0 ? "+" : "") + x.deviation_pct.toFixed(0) + "%";
    row.title = x.shared ? `${x.tracker} ${x.label}: entrada PV${x.input} compartida, meitat estimada` : `${x.tracker} ${x.label}: entrada PV${x.input}`;
    row.innerHTML = `<span>${esc(x.tracker)} ${esc(x.name)}${x.shared ? '<sup class="half">½</sup>' : ""}</span><span class="track"><span class="fill" style="width:${pct}%"></span>${medPct ? `<span class="med" style="left:${medPct}%"></span>` : ""}</span><span class="val">${fmtW(x.p)} ${dev}</span>`;
    row.addEventListener("click", () => openTracker(x.tracker));
    box.appendChild(row);
  }
}

function renderAlerts(s) {
  const ul = $("alerts");
  ul.innerHTML = "";
  const items = s.trackers.filter((t) => t.messages.length).flatMap((t) => t.messages.map((m) => ({ t, m })));
  if (!items.length) {
    ul.innerHTML = `<li>${s.active ? "Cap avís. Tots els strings produeixen semblant." : "No hi ha prou radiació per comparar."}</li>`;
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

// ---- Registre d'alarmes ----
const regState = { state: "open" };

function fmtDate(iso) {
  return new Date(iso).toLocaleString(LOC, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function updateAlertCount() {
  try {
    const d = await getJSON("/api/alerts?state=open");
    const cnt = $("alertCount");
    const n = d.alerts.length;
    cnt.hidden = !n;
    cnt.textContent = n;
    cnt.className = "count" + (d.alerts.some((a) => a.severity === "alarm") ? "" : " warning");
  } catch (e) { console.error(e); }
}

async function renderRegister() {
  document.querySelectorAll("#regSel button").forEach((b) => b.classList.toggle("on", b.dataset.s === regState.state));
  const ul = $("register");
  try {
    const d = await getJSON(`/api/alerts?state=${regState.state}`);
    ul.innerHTML = "";
    if (!d.alerts.length) {
      ul.innerHTML = `<li>${regState.state === "open" ? "No hi ha alarmes obertes." : "No hi ha alarmes tancades."}</li>`;
      return;
    }
    for (const a of d.alerts) {
      const li = document.createElement("li");
      li.className = a.severity + (a.state === "closed" ? " closed" : "");
      const where = [a.tracker ? `Seguidor ${esc(a.tracker)}` : "", a.string_id ? esc(a.string_id.split("-").slice(1).join("-")) : ""].filter(Boolean).join(" · ");
      const meta = a.source === "auto"
        ? `Automàtica · ${fmtDate(a.created)}${a.count > 1 ? ` · vista ${a.count} cops, última ${fmtDate(a.last_seen)}` : ""}`
        : `Manual · ${fmtDate(a.created)}`;
      li.innerHTML = `<div class="al-h"><span class="sev ${a.severity}">${SEV_TXT[a.severity] || a.severity}</span><b>${where || "General"}</b></div>
        <div>${esc(a.title)}</div>
        ${a.notes ? `<div class="al-notes">${esc(a.notes)}</div>` : ""}
        <div class="al-f"><span>${meta}${a.closed ? ` · tancada ${fmtDate(a.closed)}` : ""}</span>
        <button class="link">${a.state === "open" ? "Tancar" : "Reobrir"}</button></div>`;
      li.querySelector("button").addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const action = a.state === "open" ? "close" : "reopen";
        let notes = "";
        if (action === "close") notes = window.prompt("Nota de tancament (opcional):", "") ?? null;
        if (notes === null) return;
        await fetch(`/api/alerts/${a.id}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ notes }) });
        renderRegister(); updateAlertCount();
      });
      if (a.tracker) li.addEventListener("click", () => openTracker(a.tracker));
      ul.appendChild(li);
    }
  } catch (e) { console.error(e); }
}

function fillAlertForm() {
  const sel = $("fTracker");
  if (!state || sel.options.length > 1) return;
  for (const t of state.trackers) {
    const o = document.createElement("option"); o.value = t.id; o.textContent = `Seguidor ${t.id}`; sel.appendChild(o);
  }
}

function fillStringOptions() {
  const ss = $("fString");
  ss.innerHTML = '<option value="">Tot el seguidor</option>';
  const t = state && state.trackers.find((x) => x.id === $("fTracker").value);
  if (!t) return;
  for (const x of t.strings) {
    for (const name of x.label.split("+")) {
      const o = document.createElement("option"); o.value = `${t.id}-${name}`; o.textContent = name + (x.n_strings > 1 ? " (paral·lel)" : ""); ss.appendChild(o);
    }
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
    <div class="sbox ${x.status}"><b>${esc(x.label)}</b><small>Entrada PV${x.input ?? ""}${x.n_strings > 1 ? " · paral·lel" : ""}</small>
      <div class="row"><span>I</span><span>${fmt(x.current_a, 2, "A")}</span></div>
      ${x.n_strings > 1 ? `<div class="row"><span>I/str</span><span>${fmt(x.current_per_string_a, 2, "A")}</span></div>` : ""}
      <div class="row"><span>V</span><span>${fmt(x.voltage_v, 0, "V")}</span></div>
      <div class="row"><span>P</span><span>${fmtW(x.power_w)}</span></div>
      <div class="row"><span>Δ</span><span>${x.deviation_pct == null ? "–" : x.deviation_pct.toFixed(1) + " %"}</span></div>
    </div>`).join("");
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
  if (!pts.length) { const tx = el("text", { x: W / 2, y: H / 2, class: "axis", "text-anchor": "middle" }, svg); tx.textContent = "Encara no hi ha històric"; return; }
  const t0 = Math.min(...pts.map((p) => p.t)), t1 = Math.max(...pts.map((p) => p.t)) || t0 + 1;
  const maxI = Math.max(1, ...pts.map((p) => p.i || 0)) * 1.1;
  const X = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - 6);
  const Y = (i) => T + (1 - i / maxI) * (H - T - B);
  for (let k = 0; k <= 4; k++) {
    const v = (maxI * k) / 4;
    el("line", { x1: L, x2: W, y1: Y(v), y2: Y(v), class: "grid" }, svg);
    const tx = el("text", { x: 2, y: Y(v) + 3, class: "axis" }, svg); tx.textContent = v.toFixed(0);
  }
  const fmtH = (t) => new Date(t).toLocaleTimeString(LOC, { hour: "2-digit", minute: "2-digit" });
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

async function renderProduction() {
  const svg = $("prodChart");
  try {
    const d = await getJSON("/api/production?days=30");
    svg.innerHTML = "";
    const W = Math.max(320, svg.clientWidth || 600), H = 190, L = 34, B = 20, T = 14;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const days = d.days;
    if (!days.length) {
      const tx = el("text", { x: W / 2, y: H / 2, class: "axis", "text-anchor": "middle" }, svg);
      tx.textContent = "Encara no hi ha dies desats";
      return;
    }
    const max = Math.max(1, ...days.map((x) => x.kwh)) * 1.12;
    const Y = (v) => T + (1 - v / max) * (H - T - B);
    const step = Math.pow(10, Math.floor(Math.log10(max / 3)));
    const tick = [1, 2, 5, 10].map((m) => m * step).find((v) => max / v <= 5);
    for (let v = 0; v <= max; v += tick) {
      el("line", { x1: L, x2: W, y1: Y(v), y2: Y(v), class: "grid" }, svg);
      const tx = el("text", { x: L - 4, y: Y(v) + 3, class: "axis", "text-anchor": "end" }, svg);
      tx.textContent = Math.round(v);
    }
    const n = 30, slot = (W - L) / n, bw = Math.max(3, slot * 0.7);
    const today = new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Madrid" });
    const start = new Date(today); start.setDate(start.getDate() - (n - 1));
    const byDay = Object.fromEntries(days.map((x) => [x.day, x.kwh]));
    let total = 0, count = 0;
    for (let k = 0; k < n; k++) {
      const dt = new Date(start); dt.setDate(start.getDate() + k);
      const key = dt.toLocaleDateString("sv-SE");
      const v = byDay[key];
      const x = L + k * slot + (slot - bw) / 2;
      if (v != null) {
        total += v; count++;
        const r = el("rect", { x, y: Y(v), width: bw, height: Math.max(0, Y(0) - Y(v)), rx: 2, class: "bar-d" + (key === today ? " today" : "") }, svg);
        const ti = el("title", {}, r); ti.textContent = `${key}: ${v.toFixed(1)} kWh`;
        if (key === today || n <= 14) { const tv = el("text", { x: x + bw / 2, y: Y(v) - 3, class: "val" }, svg); tv.textContent = Math.round(v); }
      }
      if (dt.getDate() === 1 || k % 5 === 0 || k === n - 1) {
        const tx = el("text", { x: x + bw / 2, y: H - 5, class: "axis", "text-anchor": "middle" }, svg);
        tx.textContent = `${dt.getDate()}/${dt.getMonth() + 1}`;
      }
    }
    $("prodHint").textContent = count ? `Tots els seguidors · mitjana ${Math.round(total / count)} kWh/dia` : "Tots els seguidors · kWh";
  } catch (e) { console.error(e); }
}

const trkState = { period: "day", date: new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Madrid" }) };

function shiftDate(dir) {
  const d = new Date(trkState.date + "T12:00:00");
  if (trkState.period === "day") d.setDate(d.getDate() + dir);
  else if (trkState.period === "month") d.setMonth(d.getMonth() + dir, 1);
  else d.setFullYear(d.getFullYear() + dir, 0, 1);
  trkState.date = d.toLocaleDateString("sv-SE");
  renderTrackers();
}

function periodLabel(key, period) {
  const d = new Date(key.length === 4 ? key + "-01-01T12:00" : key.length === 7 ? key + "-01T12:00" : key + "T12:00");
  if (period === "day") return d.toLocaleDateString(LOC, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  if (period === "month") return d.toLocaleDateString(LOC, { month: "long", year: "numeric" });
  return key;
}

async function renderTrackers() {
  document.querySelectorAll("#periodSel button").forEach((b) => b.classList.toggle("on", b.dataset.p === trkState.period));
  try {
    const d = await getJSON(`/api/trackers/energy?period=${trkState.period}&date=${trkState.date}`);
    const lbl = periodLabel(d.key, d.period);
    $("periodLabel").textContent = lbl.charAt(0).toUpperCase() + lbl.slice(1);
    const total = d.trackers.reduce((a, t) => a + t.kwh, 0);
    $("trkTotal").textContent = d.has_data
      ? `Total DC seguidors: ${total.toFixed(1)} kWh` + (d.inverter_ac_kwh ? ` · inversor (AC): ${d.inverter_ac_kwh.toFixed(1)} kWh` : "")
      : "Sense dades en aquest període";
    const box = $("trkBars");
    box.innerHTML = "";
    if (!d.has_data) return;
    const vals = d.trackers.map((t) => t.kwh).sort((a, b) => a - b);
    const med = (vals[4] + vals[5]) / 2 || vals[Math.floor(vals.length / 2)];
    const max = Math.max(0.001, ...vals) * 1.05;
    for (const t of d.trackers) {
      const dev = med ? ((t.kwh - med) / med) * 100 : 0;
      const st = dev <= -15 ? "alarm" : dev <= -7 ? "warning" : "ok";
      const row = document.createElement("div");
      row.className = "trk " + st;
      const parts = t.inputs.map((i) => `${esc(i.label)} ${i.kwh.toFixed(1)}`).join(" · ");
      row.innerHTML = `<div class="trk-h"><b>${esc(t.id)}</b><span>${t.kwh.toFixed(1)} kWh <small>${dev >= 0 ? "+" : ""}${dev.toFixed(0)}%</small></span></div>
        <div class="track"><span class="fill" style="width:${(t.kwh / max) * 100}%"></span><span class="med" style="left:${(med / max) * 100}%"></span></div>
        <div class="trk-f"><span>${parts}</span><span>${t.kwh_per_kwp != null ? t.kwh_per_kwp.toFixed(2) : "–"} kWh/kWp</span></div>`;
      row.addEventListener("click", () => openTracker(t.id));
      box.appendChild(row);
    }
  } catch (e) { console.error(e); }
}

async function renderDaily() {
  try {
    const d = await getJSON("/api/daily?days=7");
    const tbl = $("daily");
    if (!d.days.length) { tbl.innerHTML = "<tr><td>Encara no hi ha dies complets.</td></tr>"; return; }
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
    renderHeader(state); renderPlan(state); renderBars(state); renderAlerts(state); renderProduction();
    updateAlertCount();
    if (selected) selectTracker(selected, false);
    if (location.hash === "#rendiment") renderDaily();
    if (location.hash === "#seguidors") renderTrackers();
    if (location.hash === "#alarmes") renderRegister();
  } catch (e) {
    $("updated").textContent = "Error de connexió";
    console.error(e);
  }
}

const PAGES = ["planol", "strings", "seguidors", "alarmes", "rendiment"];
function showPage(name) {
  if (!PAGES.includes(name)) name = "planol";
  document.querySelectorAll(".page").forEach((p) => { p.hidden = p.dataset.page !== name; });
  document.querySelectorAll(".tabbar a").forEach((a) => a.classList.toggle("active", a.dataset.tab === name));
  if (name === "rendiment") renderDaily();
  if (name === "seguidors") renderTrackers();
  if (name === "alarmes") renderRegister();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", () => showPage(location.hash.slice(1)));
showPage(location.hash.slice(1));
function openTracker(id) {
  if (location.hash !== "#planol") { history.pushState(null, "", "#planol"); showPage("planol"); }
  selectTracker(id);
}

document.querySelectorAll("#periodSel button").forEach((b) => b.addEventListener("click", () => { trkState.period = b.dataset.p; renderTrackers(); }));
$("prevP").addEventListener("click", () => shiftDate(-1));
$("nextP").addEventListener("click", () => shiftDate(1));

let resizeT;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (state) { renderPlan(state); renderProduction(); } }, 150); });

$("dClose").addEventListener("click", () => { selected = null; $("detail").hidden = true; renderPlan(state); });
$("refreshBtn").addEventListener("click", async () => {
  const b = $("refreshBtn");
  b.classList.add("spin"); b.disabled = true;
  await load();
  if (location.hash === "#alarmes") await renderRegister();
  b.classList.remove("spin"); b.disabled = false;
});
document.querySelectorAll("#regSel button").forEach((b) => b.addEventListener("click", () => { regState.state = b.dataset.s; renderRegister(); }));
$("fTracker").addEventListener("change", fillStringOptions);
$("newAlertBtn").addEventListener("click", () => { fillAlertForm(); fillStringOptions(); $("alertForm").hidden = false; $("newAlertBtn").hidden = true; $("fTitle").focus(); });
$("fCancel").addEventListener("click", () => { $("alertForm").hidden = true; $("newAlertBtn").hidden = false; });
$("alertForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { title: $("fTitle").value, tracker: $("fTracker").value, string_id: $("fString").value,
                 severity: $("fSev").value, notes: $("fNotes").value };
  const r = await fetch("/api/alerts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) { $("fErr").textContent = "No s'ha pogut desar: " + (await r.text()); return; }
  $("alertForm").reset(); $("fErr").textContent = "";
  $("alertForm").hidden = true; $("newAlertBtn").hidden = false;
  regState.state = "open"; renderRegister(); updateAlertCount();
});
load();
setInterval(load, 60000);
