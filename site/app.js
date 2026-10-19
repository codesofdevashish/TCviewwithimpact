/* Tropical cyclone dashboard. One shared time (T) drives every view:
   globe, 3D flow video, track map, timeline and the inspector. No build step. */
(() => {
  "use strict";
  const banner = document.getElementById("banner");
  const fail = (m) => { if (banner) { banner.hidden = false; banner.textContent = m; } };
  window.addEventListener("error", (e) => fail(`Something went wrong while drawing the dashboard (${e.message}). Reloading usually fixes it.`));
  if (!window.d3 || !window.topojson) { fail("A required library did not load. Please reload the page."); return; }

  // ------------------------------------------------------------------ helpers
  const CONF = window.SITE || {};
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}, kids = []) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) { if (v === undefined || v === null || v === false) continue; if (k === "text") n.textContent = v; else n.setAttribute(k, v === true ? "" : v); }
    for (const k of [].concat(kids)) if (k !== null && k !== undefined) n.append(k);
    return n;
  };
  const sv = (tag, attrs = {}) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  const set = (sel, txt) => { const n = typeof sel === "string" ? $(sel) : sel; if (n) n.textContent = txt; };
  const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const pad = (n) => String(n).padStart(2, "0");
  const toMs = (s) => { if (!s) return NaN; let t = String(s).replace(" ", "T"); if (!/Z|[+-]\d\d:?\d\d$/.test(t)) t += "Z"; return Date.parse(t); };
  const fmt = (ms, utc = true) => { const d = new Date(ms); return `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${utc ? " UTC" : ""}`; };
  const ll = (lat, lon) => { const lo = ((lon + 540) % 360) - 180; return `${Math.abs(lat).toFixed(1)}°${lat >= 0 ? "N" : "S"} ${Math.abs(lo).toFixed(1)}°${lo >= 0 ? "E" : "W"}`; };
  const CATV = (kt) => kt >= 137 ? "--c5" : kt >= 113 ? "--c4" : kt >= 96 ? "--c3" : kt >= 83 ? "--c2" : kt >= 64 ? "--c1" : kt >= 34 ? "--ts" : "--td";
  const catName = (kt) => kt >= 137 ? "Category 5" : kt >= 113 ? "Category 4" : kt >= 96 ? "Category 3" : kt >= 83 ? "Category 2" : kt >= 64 ? "Category 1" : kt >= 34 ? "Tropical storm" : "Depression";
  const col = (kt) => getComputedStyle(document.documentElement).getPropertyValue(CATV(kt)).trim() || "#888";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const H = 36e5;
  const PTS16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

  function subsolar(ms) {
    const d = new Date(ms), start = Date.UTC(d.getUTCFullYear(), 0, 1);
    const doy = Math.floor((ms - start) / 864e5) + 1, hr = d.getUTCHours() + d.getUTCMinutes() / 60;
    const g = 2 * Math.PI / 365 * (doy - 1 + (hr - 12) / 24);
    const eqt = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g) - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));
    const dec = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g) + 0.000907 * Math.sin(2 * g) - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
    return [-15 * (hr - 12 + eqt / 60), dec * 180 / Math.PI];
  }
  const nightCircle = (ms, r = 90) => { const [lo, la] = subsolar(ms); return d3.geoCircle().center([lo + 180, -la]).radius(r)(); };
  const drawNight = (g, path, ms) => g.selectAll("path").data([96, 93, 90, 84]).join("path").attr("class", "g-night").attr("d", (r) => path(nightCircle(ms, r)));
  const symbolSVG = (south, r = 11) =>
    `<g class="spin${south ? " south" : ""}"><g transform="scale(${south ? -1 : 1},1)">` +
    `<path d="M0,${-r * .42} C${r * .55},${-r * .55} ${r * .85},${-r * .2} ${r * .92},${r * .36}" fill="none" stroke="currentColor" stroke-width="${r * .28}" stroke-linecap="round"/>` +
    `<path d="M0,${r * .42} C${-r * .55},${r * .55} ${-r * .85},${r * .2} ${-r * .92},${-r * .36}" fill="none" stroke="currentColor" stroke-width="${r * .28}" stroke-linecap="round"/></g>` +
    `<circle r="${r * .42}" fill="var(--panel)" stroke="currentColor" stroke-width="${r * .18}"/></g>`;

  // ------------------------------------------------------------------ storm model
  function prep(s) {
    let prev = null;
    s._pts = (s.track || []).map(([t, lat, lon, kt]) => {
      let lo = lon; if (prev !== null) { while (lo - prev > 180) lo -= 360; while (lo - prev < -180) lo += 360; }
      prev = lo; return { t: toMs(t), lat, lon: lo, kt };
    }).filter((p) => !isNaN(p.t));
    const P = s._pts;
    s._t0 = P.length ? P[0].t : NaN; s._t1 = P.length ? P[P.length - 1].t : NaN;
    s._ace = P.filter((p) => p.kt >= 34 && new Date(p.t).getUTCHours() % 6 === 0).reduce((a, p) => a + p.kt * p.kt, 0) / 1e4;
    s._ws = toMs(s.window_start); s._we = toMs(s.window_end);
    s._fps = s.video_fps || 15.1515; s._fph = s.video_fph || 1;
    const w = [];
    for (let i = 0; i < P.length; i++) for (let j = i + 1; j < P.length; j++) {
      const dh = (P[j].t - P[i].t) / H; if (dh > 27) break;
      if (Math.abs(dh - 24) <= 3 && P[j].kt - P[i].kt >= 30) w.push([P[i].t, P[j].t]);
    }
    w.sort((a, b) => a[0] - b[0]);
    s._ri = w.reduce((acc, x) => { const l = acc[acc.length - 1]; if (l && x[0] <= l[1]) l[1] = Math.max(l[1], x[1]); else acc.push([...x]); return acc; }, []);
    s._south = P.length ? P[P.length - 1].lat < 0 : false;
    return s;
  }
  function at(s, t) {
    const P = s._pts; if (!P.length) return null;
    if (t <= P[0].t) return P[0]; if (t >= P[P.length - 1].t) return P[P.length - 1];
    for (let i = 1; i < P.length; i++) if (P[i].t >= t) {
      const a = (t - P[i - 1].t) / (P[i].t - P[i - 1].t), A = P[i - 1], B = P[i];
      return { t, lat: A.lat + a * (B.lat - A.lat), lon: A.lon + a * (B.lon - A.lon), kt: A.kt + a * (B.kt - A.kt) };
    }
    return P[P.length - 1];
  }
  const nFrames = (s) => Math.round((s._we - s._ws) / H * s._fph) + 1;
  const vTime = (s, v) => s._ws + Math.min(v.currentTime * s._fps, nFrames(s) - 1) / s._fph * H;
  const inWindow = (s, t) => !isNaN(s._ws) && t >= s._ws - 1 && t <= s._we + 1;
  function seekVideo(s, v, t) {
    if (isNaN(s._ws)) return;
    const c = Math.min(Math.max(t, s._ws), s._we);
    const target = (c - s._ws) / H * s._fph / s._fps + 1e-3;
    if (Math.abs(v.currentTime - target) > 0.02) v.currentTime = target;
  }

  // ------------------------------------------------------------------ state
  let DATA = null, ALL = [], ACTIVE = [], ARCH = [], SEL = null, T = NaN, STAGE = "globe", BASIN = "", ENV = null;
  let PINS = []; try { PINS = JSON.parse(localStorage.getItem("tc-pins") || "[]"); } catch (e) { PINS = []; }
  const savePins = () => { try { localStorage.setItem("tc-pins", JSON.stringify(PINS)); } catch (e) { /* storage unavailable */ } };
  const video = $("#v-video");

  // ------------------------------------------------------------------ profile
  set("#brand-name", CONF.name || "Tropical cyclones");
  if (CONF.name) $("#brand-name").after(el("span", { class: "brand-sub", text: "Tropical cyclones" }));
  set("#about-name", CONF.name || ""); set("#about-role", [CONF.role, CONF.affiliation].filter(Boolean).join(", ")); set("#about-summary", CONF.summary || "");
  for (const i of CONF.interests || []) $("#about-interests").append(el("li", { text: i }));
  for (const m of CONF.memberships || []) $("#about-members").append(el("li", { text: m }));
  for (const k of CONF.skills || ["Automatic global storm detection from operational TCVitals, every day",
    "Processing of NCEP GFS 0.25° analyses in every basin, including dateline and Southern Hemisphere storms",
    "3D visualisation of tropical cyclone flow, vortex core and convection",
    "Hourly structure and environment diagnostics: tilt, shear, humidity, SST, RMW",
    "Rapid intensification detection from official intensities", "A fully automated pipeline on GitHub Actions and GitHub Pages"])
    $("#about-skills").append(el("li", { text: k }));
  const Lk = CONF.links || {};
  for (const [k, label, f] of [["email", "Email", (v) => `mailto:${v}`], ["scholar", "Google Scholar"], ["github", "GitHub"], ["linkedin", "LinkedIn"], ["cv", "CV (PDF)"]])
    if (Lk[k]) $("#about-links").append(el("a", { href: f ? f(Lk[k]) : Lk[k], text: label, rel: "noopener" }));
  const clock = () => { const d = new Date(); set("#clock", `Live ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`); };
  clock(); setInterval(clock, 1000);

  // ================================================================== GLOBE (stage 1)
  const EARTH = window.Earth ? window.Earth.create($("#earth-canvas"), {
    labels: $("#earth-labels"), onSelect: (k) => { const s = ALL.find((x) => x.key === k); if (s) select(s, { keepTime: true }); },
    onSpin: (on) => { const b = $("#e-spin"); b.setAttribute("aria-pressed", String(on)); b.textContent = on ? "Stop spinning" : "Spin"; } }) : null;
  window.__earth = EARTH;
  if (EARTH) {
    $("#e-in").addEventListener("click", () => EARTH.zoomIn()); $("#e-out").addEventListener("click", () => EARTH.zoomOut());
    $("#e-reset").addEventListener("click", () => { if (SEL) { const p = at(SEL, T); if (p) EARTH.focus(p.lat, p.lon, 900); } });
    $("#e-spin").addEventListener("click", () => { const on = $("#e-spin").getAttribute("aria-pressed") !== "true"; EARTH.setSpin(on);
      $("#e-spin").setAttribute("aria-pressed", String(on)); $("#e-spin").textContent = on ? "Stop spinning" : "Spin"; });
  } else { $('[data-stage="globe"]').hidden = true; STAGE = "map"; }
  function globeStorms() {
    if (!EARTH) return;
    const list = ACTIVE.slice(); if (SEL && !list.includes(SEL)) list.push(SEL);
    EARTH.setStorms(list.map((s) => ({ key: s.key, name: s.name, kt: s.vmax_kt, south: s._south, color: col(s.vmax_kt),
      pts: s._pts.map((p) => ({ lat: p.lat, lon: p.lon, t: p.t, kt: p.kt, color: col(p.kt) })) })), SEL && SEL.key);
  }

  // ================================================================== TRACK MAP (stage 3)
  let LAND110 = null, LAND50 = null;
  const M = { svg: d3.select("#map"), k: 1, s: null };
  M.proj = d3.geoOrthographic().clipAngle(90).precision(0.3); M.path = d3.geoPath(M.proj);
  for (const c of ["ocean", "grat", "land", "night", "others", "track", "dots", "ghost", "mark"]) M[c] = M.svg.append("g");
  function mapSize() { const r = $("#map").getBoundingClientRect(); return [Math.max(300, r.width || 600), Math.max(240, r.height || 400)]; }
  function drawMap() {
    const s = SEL; if (!s || !s._pts.length || STAGE !== "map") return;
    const [w, h] = mapSize(); M.svg.attr("viewBox", `0 0 ${w} ${h}`);
    const P = s._pts;
    if (M.s !== s) {
      M.rot = [-d3.mean(P, (p) => p.lon), -d3.mean(P, (p) => p.lat)]; M.k = 1; M.s = s;
      M.proj.rotate(M.rot).fitExtent([[40, 40], [w - 40, h - 40]], { type: "MultiPoint", coordinates: P.map((p) => [p.lon, p.lat]) });
      M.base = Math.min(M.proj.scale(), 5200);
    }
    M.proj.rotate(M.rot).scale(M.base * M.k).translate([w / 2, h / 2]);
    const pa = M.path;
    M.ocean.selectAll("*").remove(); M.ocean.append("path").attr("class", "g-ocean").attr("d", pa({ type: "Sphere" }));
    M.grat.selectAll("*").remove(); M.grat.append("path").attr("class", "g-grat").attr("d", pa(d3.geoGraticule().step([5, 5])()));
    M.land.selectAll("*").remove(); if (LAND50 || LAND110) M.land.append("path").attr("class", "g-land").attr("d", pa(LAND50 || LAND110));
    M.others.selectAll("*").remove();
    for (const o of ACTIVE) if (o !== s) M.others.append("path").attr("class", "m-other").attr("stroke", col(o.vmax_kt)).attr("d", pa({ type: "LineString", coordinates: o._pts.map((p) => [p.lon, p.lat]) }));
    M.track.selectAll("*").remove();
    for (let i = 1; i < P.length; i++) M.track.append("path").attr("class", "m-track").attr("stroke", col(P[i].kt)).attr("d", pa({ type: "LineString", coordinates: [[P[i - 1].lon, P[i - 1].lat], [P[i].lon, P[i].lat]] }));
    M.dots.selectAll("*").remove();
    for (const p of P) if (new Date(p.t).getUTCHours() % 12 === 0) { const xy = M.proj([p.lon, p.lat]); if (xy) M.dots.append("circle").attr("class", "m-dot").attr("r", 3).attr("cx", xy[0]).attr("cy", xy[1]); }
    M.mark.selectAll("*").remove(); M.markG = M.mark.append("g").style("color", "#fff"); M.markG.html(symbolSVG(s._south, 13));
    M.night.selectAll("*").remove();
    mapTime();
  }
  function mapTime() {
    if (STAGE !== "map" || !M.s || !M.markG) return;
    const p = at(M.s, T); if (!p) return;
    const xy = M.proj([p.lon, p.lat]); if (xy) M.markG.attr("transform", `translate(${xy[0]},${xy[1]})`);
    drawNight(M.night, M.path, T);
  }
  function mapGhost(p) {
    M.ghost.selectAll("*").remove(); if (!p || STAGE !== "map") return;
    const xy = M.proj([p.lon, p.lat]); if (xy) M.ghost.append("circle").attr("r", 8).attr("cx", xy[0]).attr("cy", xy[1]).attr("fill", "none").attr("stroke", "#fff").attr("stroke-width", 2).attr("stroke-dasharray", "3 2");
  }
  M.svg.call(d3.drag().on("drag", (e) => {
    if (!SEL) return; const [w] = mapSize(), r = $("#map").getBoundingClientRect(), px = w / Math.max(1, r.width), deg = 180 / (Math.PI * M.proj.scale());
    M.rot = [M.rot[0] + e.dx * px * deg, Math.max(-85, Math.min(85, M.rot[1] - e.dy * px * deg))]; drawMap();
  }));
  $("#m-in").addEventListener("click", () => { M.k = Math.min(6, M.k * 1.4); drawMap(); });
  $("#m-out").addEventListener("click", () => { M.k = Math.max(0.25, M.k / 1.4); drawMap(); });

  // ================================================================== SHARED TIMELINE
  const TL = {};
  function drawTimeline() {
    const box = $("#timeline"); box.textContent = ""; TL.ok = false;
    const s = SEL; if (!s || s._pts.length < 2) { box.append(el("p", { class: "small muted", text: "Not enough advisories yet to draw a timeline." })); return; }
    const W = Math.max(360, box.clientWidth || 800), Hh = 150, m = { l: 34, r: 10, b: 18 };
    const t0 = s._t0 - 3 * H, t1 = s._t1 + 3 * H;
    const X = (t) => m.l + (W - m.l - m.r) * (t - t0) / (t1 - t0);
    const lanes = [s, ...ACTIVE.filter((o) => o !== s && o._t1 >= t0 && o._t0 <= t1)].slice(0, 4);
    const laneH = 13, top = 4 + lanes.length * (laneH + 3) + 4, bot = Hh - m.b;
    const ymax = Math.max(70, d3.max(s._pts, (p) => p.kt) * 1.12);
    const Y = (k) => bot - (bot - top) * k / ymax;
    const svg = sv("svg", { viewBox: `0 0 ${W} ${Hh}`, role: "img", "aria-label": `Timeline of ${s.name}: official intensity and storm lifespans` });
    for (const [a, b, k] of [[0, 34, 20], [34, 64, 40], [64, 83, 70], [83, 96, 90], [96, 113, 100], [113, 137, 120], [137, 999, 140]])
      if (a < ymax) svg.append(sv("rect", { class: "band", x: m.l, width: W - m.l - m.r, y: Y(Math.min(b, ymax)), height: Y(a) - Y(Math.min(b, ymax)), fill: col(k) }));
    for (const g of [34, 64, 96, 137]) if (g < ymax * .95) {
      svg.append(sv("line", { class: "grid", x1: m.l, x2: W - m.r, y1: Y(g), y2: Y(g) }));
      const tx = sv("text", { x: m.l - 4, y: Y(g) + 4, "text-anchor": "end" }); tx.textContent = g; svg.append(tx);
    }
    const d0 = new Date(t0); d0.setUTCHours(0, 0, 0, 0);
    const days = (t1 - t0) / 864e5, every = days > 14 ? 3 : days > 7 ? 2 : 1; let di = 0;
    for (let d = d0.getTime() + 864e5; d < t1; d += 864e5, di++) {
      svg.append(sv("line", { class: "grid", x1: X(d), x2: X(d), y1: top, y2: bot }));
      if (di % every === 0) { const tx = sv("text", { x: X(d) + 3, y: Hh - 4 }); const dd = new Date(d); tx.textContent = `${dd.getUTCDate()} ${MON[dd.getUTCMonth()]}`; svg.append(tx); }
    }
    lanes.forEach((o, i) => {                                   // lifespans: click a bar to switch storm at the same moment
      const y = 4 + i * (laneH + 3), g = sv("g", { class: "life", role: "button", tabindex: 0, "aria-label": `Open ${o.name}` });
      const x0 = Math.max(m.l, X(o._t0)), x1 = Math.min(W - m.r, X(o._t1));
      g.append(sv("rect", { x: x0, y, width: Math.max(3, x1 - x0), height: laneH, rx: 3, fill: col(o.vmax_kt), opacity: o === s ? 0.9 : 0.4 }));
      const tx = sv("text", { x: x0 + 5, y: y + laneH - 3 }); tx.textContent = `${o.name}${o === s ? "" : "  (click to switch)"}`; g.append(tx);
      g.addEventListener("click", (e) => { e.stopPropagation(); if (o !== s) select(o, { keepTime: true }); });
      g.addEventListener("keydown", (e) => { if (e.key === "Enter") select(o, { keepTime: true }); });
      svg.append(g);
    });
    if (!isNaN(s._ws)) svg.append(sv("rect", { class: "win", x: X(s._ws), width: Math.max(2, X(s._we) - X(s._ws)), y: top, height: bot - top }));
    if (!isNaN(s._ws)) { const tx = sv("text", { x: X(s._ws) + 4, y: top + 11 }); tx.textContent = "3D video"; svg.append(tx); }
    for (const [a, b] of s._ri) svg.append(sv("rect", { class: "riw", x: X(a), width: X(b) - X(a), y: top, height: bot - top }));
    const P = s._pts;
    for (let i = 1; i < P.length; i++)
      svg.append(sv("line", { x1: X(P[i - 1].t), y1: Y(P[i - 1].kt), x2: X(P[i].t), y2: Y(P[i].kt), stroke: col(Math.max(P[i - 1].kt, P[i].kt)), "stroke-width": 3.5, "stroke-linecap": "round" }));
    const head = sv("line", { class: "head", x1: -9, x2: -9, y1: 2, y2: bot }), knob = sv("circle", { class: "knob", r: 6, cx: -9, cy: -9 });
    const hit = sv("rect", { x: m.l, y: top, width: W - m.l - m.r, height: Hh - top, fill: "transparent", tabindex: 0, "aria-label": "Timeline. Left and right arrows move one hour." });
    svg.append(head, knob, hit); box.append(svg);
    Object.assign(TL, { ok: true, X, Y, t0, t1, head, knob, s });
    const tAt = (ev) => { const r = svg.getBoundingClientRect(); const x = (ev.clientX - r.left) * W / r.width; return t0 + (t1 - t0) * Math.min(1, Math.max(0, (x - m.l) / (W - m.l - m.r))); };
    let drag = false;
    hit.addEventListener("pointerdown", (e) => { drag = true; hit.setPointerCapture(e.pointerId); pause(); setTime(tAt(e)); });
    hit.addEventListener("pointermove", (e) => { if (drag) setTime(tAt(e)); else { const p = at(s, tAt(e)); mapGhost(p); } });
    hit.addEventListener("pointerup", () => { drag = false; });
    hit.addEventListener("pointerleave", () => { if (!drag) mapGhost(null); });
    hit.addEventListener("keydown", (e) => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); e.stopPropagation(); step(e.key === "ArrowRight" ? 1 : -1); } });
    timelineTime();
  }
  function timelineTime() {
    if (!TL.ok || TL.s !== SEL || isNaN(T)) return;
    const x = TL.X(Math.min(Math.max(T, TL.t0), TL.t1)), p = at(SEL, T);
    TL.head.setAttribute("x1", x); TL.head.setAttribute("x2", x); TL.knob.setAttribute("cx", x); if (p) TL.knob.setAttribute("cy", TL.Y(p.kt));
  }
  let roT = 0; new ResizeObserver(() => { clearTimeout(roT); roT = setTimeout(() => { drawTimeline(); drawInspectorCharts(); if (STAGE === "map") drawMap(); IMP.geo = null; drawImpact(); }, 120); }).observe($("#timeline"));

  // ================================================================== INSPECTOR
  const METRICS = [
    { id: "wind", name: "Official wind", unit: "kt", val: (t) => { const p = at(SEL, t); return p ? p.kt : null; }, track: true },
    { id: "dv", name: "Change in 24 h", unit: "kt", val: (t) => { if (!SEL || t - 24 * H < SEL._t0) return null; return at(SEL, t).kt - at(SEL, t - 24 * H).kt; }, sign: true, good: (v) => (v >= 30 ? "good" : "") },
    { id: "motion", name: "Motion", text: (t) => { if (!SEL || t - 6 * H < SEL._t0) return "n/a"; const a = at(SEL, t - 6 * H), b = at(SEL, t);
        const R = Math.PI / 180, dl = (b.lon - a.lon) * R, y = Math.sin(dl) * Math.cos(b.lat * R), x = Math.cos(a.lat * R) * Math.sin(b.lat * R) - Math.sin(a.lat * R) * Math.cos(b.lat * R) * Math.cos(dl);
        const brg = (Math.atan2(y, x) / R + 360) % 360; const km = d3.geoDistance([a.lon, a.lat], [b.lon, b.lat]) * 6371 / 6;
        return km < 3 ? "Stationary" : `${PTS16[Math.round(brg / 22.5) % 16]} ${Math.round(km)} km/h`; } },
    { id: "pmin", name: "Pressure (latest)", text: () => (SEL && SEL.pmin ? `${Math.round(SEL.pmin)} hPa` : "n/a") },
    { id: "shear_deep", name: "Shear 850–200", unit: "m/s", env: true, good: (v) => (v < 10 ? "good" : "bad") },
    { id: "shear_mid", name: "Shear 850–500", unit: "m/s", env: true },
    { id: "rh_mid", name: "Humidity 500–700", unit: "%", env: true, good: (v) => (v >= 70 ? "good" : "bad") },
    { id: "sst", name: "Sea surface", unit: "°C", env: true, dp: 1, good: (v) => (v >= 28 ? "good" : "bad") },
    { id: "tilt", name: "Vortex tilt", unit: "km", env: true, good: (v) => (v < 50 ? "good" : "bad") },
    { id: "rmw", name: "Radius of max wind", unit: "km", env: true },
  ];
  const CHECKS = [["shear_deep", "Deep shear", (v) => v < 10, "< 10 m/s", "m/s", 0], ["rh_mid", "Humidity", (v) => v >= 70, "≥ 70%", "%", 0],
                  ["sst", "SST", (v) => v >= 28, "≥ 28 °C", "°C", 1], ["tilt", "Tilt", (v) => v < 50, "< 50 km", "km", 0]];
  const envIdx = (t) => { if (!ENV || ENV.s !== SEL) return -1; const i = d3.bisectCenter(ENV.t, t); return Math.abs(ENV.t[i] - t) <= 1.5 * H ? i : -1; };
  const envVal = (k, t) => { const i = envIdx(t); if (i < 0) return null; const v = (ENV.d[k] || [])[i]; return v === undefined ? null : v; };
  function fillInspector() {
    const s = SEL; if (!s) return;
    set("#i-kicker", `${s.label} ${s.sid} · ${s.basin_name}${s.status !== "active" ? ` · ${s.year}, ended` : ""}`);
    set("#i-name", s.name);
    const tags = $("#i-tags"); tags.textContent = ""; tags.style.setProperty("--cat", `var(${CATV(s.vmax_kt)})`);
    tags.append(el("span", { class: "tag cat", text: `${catName(s.vmax_kt)}, ${s.vmax_kt} kt` }));
    if (s.status !== "active") tags.append(el("span", { class: "tag", text: `Peak ${s.peak_kt} kt` }));
    if (s.ri) tags.append(el("span", { class: "tag ri", text: "Rapid intensification" }));
    const pinned = PINS.includes(s.key); $("#pin").setAttribute("aria-pressed", String(pinned)); $("#pin").textContent = pinned ? "★ Pinned" : "☆ Pin";
    const mt = $("#metrics"); mt.textContent = "";
    for (const M_ of METRICS) mt.append(el("div", { class: "metric", "data-m": M_.id }, [el("span", { text: M_.name }), el("b", { text: "—" })]));
    const story = s.story || [], sum = $("#itab-summary"); sum.textContent = "";
    if (story.length) story.forEach((p) => sum.append(el("p", { text: p }))); else sum.append(el("p", { class: "muted", text: "A written summary appears after the next daily update." }));
    const files = $("#itab-files"); files.textContent = "";
    const share = el("button", { type: "button", class: "tb wide", text: "Copy link to this storm" });
    share.addEventListener("click", () => { const url = `${location.origin}${location.pathname}#storm=${s.key}`;
      (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject()).then(() => { share.textContent = "Link copied"; setTimeout(() => (share.textContent = "Copy link to this storm"), 2000); }, () => prompt("Copy this link:", url)); });
    files.append(el("div", { class: "actions" }, [
      el("a", { class: "tb wide", href: s.video, download: `${s.key}.mp4`, text: "Download the 3D video (MP4)" }),
      el("a", { class: "tb wide", href: s.poster, target: "_blank", rel: "noopener", text: "Open the last frame as an image" }),
      el("a", { class: "tb wide", id: "wv-link", href: "#", target: "_blank", rel: "noopener", text: "Satellite view at this hour (NASA Worldview)" }), share,
      el("p", { class: "small muted", text: `Tracked since ${fmt(s._t0)}. Accumulated cyclone energy ${s._ace.toFixed(1)} × 10⁴ kt². ${isNaN(s._ws) ? "" : `3D video covers ${fmt(s._ws, false)} to ${fmt(s._we)}.`}` }),
    ]));
    drawInspectorCharts();
  }
  function drawInspectorCharts() {                // sparklines under metrics that have a series
    for (const card of $$("#metrics .metric")) {
      const M_ = METRICS.find((x) => x.id === card.dataset.m); const old = card.querySelector("svg"); if (old) old.remove(); card._cur = null;
      if (!M_ || !SEL) continue;
      let xs = [], ys = [];
      if (M_.track) { xs = SEL._pts.map((p) => p.t); ys = SEL._pts.map((p) => p.kt); }
      else if (M_.env && ENV && ENV.s === SEL && ENV.d[M_.id]) { xs = ENV.t; ys = ENV.d[M_.id]; }
      const v = ys.filter((y) => y !== null && y !== undefined); if (v.length < 2) continue;
      const W = Math.max(100, card.clientWidth - 18), Hs = 22, lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
      const X = (t) => 1 + (W - 2) * (t - xs[0]) / Math.max(1, xs[xs.length - 1] - xs[0]), Y = (y) => Hs - 2 - (Hs - 4) * (y - lo) / span;
      const d = d3.line().defined((y) => y !== null && y !== undefined).x((_, i) => X(xs[i])).y((y) => Y(y))(ys);
      const svg = sv("svg", { viewBox: `0 0 ${W} ${Hs}`, "aria-hidden": "true" });
      svg.append(sv("path", { d: d || "", fill: "none", stroke: "#8fdcff", "stroke-width": 1.5 }));
      const cur = sv("line", { x1: -5, x2: -5, y1: 0, y2: Hs, stroke: "#fff", "stroke-width": 1 }); svg.append(cur);
      card._cur = { cur, X, t0: xs[0], t1: xs[xs.length - 1] }; card.append(svg);
    }
    inspectorTime();
  }
  function inspectorTime() {
    if (!SEL || isNaN(T)) return;
    for (const card of $$("#metrics .metric")) {
      const M_ = METRICS.find((x) => x.id === card.dataset.m); if (!M_) continue;
      const b = card.querySelector("b"); b.className = "";
      if (card._cur) { const x = card._cur.X(Math.min(Math.max(T, card._cur.t0), card._cur.t1)); card._cur.cur.setAttribute("x1", x); card._cur.cur.setAttribute("x2", x); }
      if (M_.text) { b.textContent = M_.text(T); continue; }
      const v = M_.env ? envVal(M_.id, T) : M_.val(T);
      if (v === null || v === undefined || !isFinite(v)) { b.textContent = M_.env ? (ENV && ENV.s === SEL ? "outside video" : "n/a") : "n/a"; continue; }
      b.textContent = `${M_.sign && v > 0 ? "+" : ""}${M_.dp ? v.toFixed(M_.dp) : Math.round(v)} ${M_.unit}`;
      if (M_.good) { const g = M_.good(v); if (g) b.classList.add(g); }
    }
    const ul = $("#checks"); ul.textContent = ""; let ok = 0, n = 0;
    for (const [k, name, test, thr, unit, dp] of CHECKS) {
      const v = envVal(k, T);
      if (v === null || v === undefined) { ul.append(el("li", { class: "na", text: `${name}: n/a` })); continue; }
      const g = test(v); n++; if (g) ok++;
      ul.append(el("li", { class: g ? "ok" : "no", text: `${g ? "✓" : "✗"} ${name} ${dp ? v.toFixed(dp) : Math.round(v)} ${unit} (${thr})` }));
    }
    set("#checks-title", n ? `Conditions at this hour: ${ok} of ${n} favourable` : (SEL.diag ? "Conditions: move the timeline into the 3D video period" : "Conditions: hourly data not available for this storm"));
    const wv = $("#wv-link"); if (wv) { const p = at(SEL, T) || {}; const lo = (((p.lon ?? 0) + 540) % 360) - 180, la = p.lat ?? 0, d = new Date(T);
      wv.href = `https://worldview.earthdata.nasa.gov/?v=${Math.max(-180, lo - 12).toFixed(1)},${Math.max(-85, la - 9).toFixed(1)},${Math.min(180, lo + 12).toFixed(1)},${Math.min(85, la + 9).toFixed(1)}&t=${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}-T${pad(d.getUTCHours())}:00:00Z`; }
  }
  function loadEnv(s) {
    ENV = null; if (!s.diag) return;
    fetch(`${s.diag}?v=${encodeURIComponent((DATA && DATA.generated) || "")}`).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then((d) => { if (SEL !== s) return; ENV = { s, t: d.time.map(toMs), d }; drawInspectorCharts(); })
      .catch(() => {});
  }
  $$(".i-tabs button").forEach((b) => b.addEventListener("click", () => {
    for (const x of $$(".i-tabs button")) x.setAttribute("aria-selected", String(x === b));
    $("#itab-summary").hidden = b.dataset.itab !== "summary"; $("#itab-files").hidden = b.dataset.itab !== "files";
  }));
  $("#pin").addEventListener("click", () => {
    if (!SEL) return; const i = PINS.indexOf(SEL.key); if (i >= 0) PINS.splice(i, 1); else PINS.unshift(SEL.key);
    PINS = PINS.slice(0, 8); savePins(); fillInspector(); drawSide(); setupCompare();
  });


  // ================================================================== IMPACT (stage 5): hazard x exposure x vulnerability
  const IMP = { data: null, s: null, layer: "wind", geo: null };
  const impCanvas = $("#imp-canvas"), ictx = impCanvas.getContext("2d");
  const b64u8 = (b) => { const t = atob(b), a = new Uint8Array(t.length); for (let i = 0; i < t.length; i++) a[i] = t.charCodeAt(i); return a; };
  const fmtN = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)} million` : n >= 1e3 ? `${Math.round(n / 1e3)} thousand` : `${Math.round(n)}`);
  const RISK = [[1, "very low", "#fde68a"], [3, "low", "#fdba74"], [8, "moderate", "#fb923c"], [20, "high", "#ef4444"], [1e9, "very high", "#991b1b"]];
  const riskClass = (r) => RISK.find(([hi]) => r < hi);
  const WINDC = [[64, "34–63 kt", "--ts"], [83, "64–82 kt (Cat 1)", "--c1"], [96, "83–95 kt (Cat 2)", "--c2"], [113, "96–112 kt (Cat 3)", "--c3"], [137, "113–136 kt (Cat 4)", "--c4"], [999, "137 kt or more (Cat 5)", "--c5"]];
  const LUT = {};                                           // 256 colours per layer
  function luts() {
    const rgba = (c, a) => { const x = d3.color(c); x.opacity = a; return x.toString(); };
    LUT.wind = Array.from({ length: 256 }, (_, v) => v < 34 || v === 255 ? null : rgba(col(v), 0.62));
    LUT.risk = Array.from({ length: 256 }, (_, v) => { const r = v / 4; return r < 0.05 || v === 255 ? null : rgba(riskClass(r)[2], 0.8); });
    LUT.pop = Array.from({ length: 256 }, (_, v) => { const p = Math.pow(10, v / 40) - 1; return p < 5 ? null : rgba(d3.interpolateMagma(Math.min(1, 0.15 + Math.log10(p) / 5.6)), 0.85); });
    LUT.vuln = Array.from({ length: 256 }, (_, v) => v > 100 ? null : rgba(d3.interpolateRdYlGn(1 - v / 100), 0.55));
  }
  luts();
  function loadImpact(s) {
    IMP.data = null; IMP.s = s; IMP.geo = null; impactPanel();
    if (!s.impact) { drawImpact(); return; }
    fetch(`${s.impact}?v=${encodeURIComponent((DATA && DATA.generated) || "")}`).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then((d) => {
        if (SEL !== s) return;
        d.L = {}; for (const [k, v] of Object.entries(d.layers)) d.L[k] = b64u8(v);
        d._t0 = toMs(d.t0); d.series._t = (d.series.t || []).map(toMs);
        IMP.data = d; IMP.geo = null; impactPanel(); drawImpact();
      }).catch(() => { impactPanel(); drawImpact(); });
  }
  function impactGeometry() {
    const d = IMP.data, r = impCanvas.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(300, r.width), h = Math.max(240, r.height);
    impCanvas.width = Math.round(w * dpr); impCanvas.height = Math.round(h * dpr);
    const g = d.grid, la0 = g.lat_top - g.ny * g.d, lo0 = g.lon_left, lo1 = g.lon_left + g.nx * g.d;
    const proj = d3.geoOrthographic().clipAngle(90).rotate([-(lo0 + lo1) / 2, -(g.lat_top + la0) / 2])
      .fitExtent([[20, 60], [w - 20, h - 70]], { type: "MultiPoint", coordinates: [[lo0, la0], [lo1, la0], [lo0, g.lat_top], [lo1, g.lat_top], [(lo0 + lo1) / 2, la0], [(lo0 + lo1) / 2, g.lat_top]] });
    const n = g.ny * g.nx, X = new Float32Array(n), Y = new Float32Array(n), W_ = new Float32Array(n), H_ = new Float32Array(n);
    for (let i = 0; i < g.ny; i++) for (let j = 0; j < g.nx; j++) {
      const lat = g.lat_top - (i + 0.5) * g.d, lon = g.lon_left + (j + 0.5) * g.d, k = i * g.nx + j;
      const a = proj([lon - g.d / 2, lat + g.d / 2]), b = proj([lon + g.d / 2, lat - g.d / 2]);
      if (!a || !b) { X[k] = NaN; continue; }
      X[k] = Math.min(a[0], b[0]); Y[k] = Math.min(a[1], b[1]); W_[k] = Math.abs(b[0] - a[0]) + 0.6; H_[k] = Math.abs(b[1] - a[1]) + 0.6;
    }
    IMP.geo = { proj, w, h, dpr, X, Y, W: W_, H: H_ };
  }
  function drawImpact() {
    if (STAGE !== "impact") return;
    const c = ictx, r = impCanvas.getBoundingClientRect();
    if (!IMP.data || IMP.s !== SEL) {
      impCanvas.width = r.width; impCanvas.height = r.height; c.clearRect(0, 0, r.width, r.height);
      c.fillStyle = "#93a9b9"; c.font = "15px 'Source Sans 3', sans-serif"; c.textAlign = "center";
      c.fillText(SEL && SEL.impact ? "Loading the impact layer…" : "No impact layer for this storm yet. It is added by the daily update.", r.width / 2, r.height / 2);
      legend(); return;
    }
    if (!IMP.geo) impactGeometry();
    const G_ = IMP.geo, d = IMP.data, g = d.grid, path = d3.geoPath(G_.proj, c);
    c.setTransform(G_.dpr, 0, 0, G_.dpr, 0, 0); c.clearRect(0, 0, G_.w, G_.h);
    c.beginPath(); path({ type: "Sphere" }); c.fillStyle = "#0b2232"; c.fill();
    if (LAND50 || LAND110) { c.beginPath(); path(LAND50 || LAND110); c.fillStyle = "#24394a"; c.fill(); }
    const L = IMP.layer, vals = d.L[L === "wind" ? "wind_kt" : L === "pop" ? "pop_log" : L], lut = LUT[L], arr = d.L.arrival_h;
    const hourNow = Math.floor((T - d._t0) / H), grow = L === "wind" || L === "risk";
    for (let k = 0; k < vals.length; k++) {
      if (isNaN(G_.X[k])) continue; const col_ = lut[vals[k]]; if (!col_) continue;
      if (grow && (arr[k] === 255 || arr[k] > hourNow)) continue;
      c.fillStyle = col_; c.fillRect(G_.X[k], G_.Y[k], G_.W[k], G_.H[k]);
    }
    if (LAND50 || LAND110) { c.beginPath(); path(LAND50 || LAND110); c.strokeStyle = "rgba(200,220,235,.55)"; c.lineWidth = 0.8; c.stroke(); }
    c.beginPath(); path(d3.geoGraticule().step([5, 5])()); c.strokeStyle = "rgba(120,150,170,.18)"; c.lineWidth = 0.5; c.stroke();
    const P = SEL._pts;
    for (let i = 1; i < P.length; i++) { c.beginPath(); path({ type: "LineString", coordinates: [[P[i - 1].lon, P[i - 1].lat], [P[i].lon, P[i].lat]] }); c.strokeStyle = col(P[i].kt); c.lineWidth = 3; c.stroke(); }
    const p = at(SEL, T), xy = p && G_.proj([p.lon, p.lat]);
    if (xy) { c.beginPath(); c.arc(xy[0], xy[1], 6, 0, 2 * Math.PI); c.fillStyle = "#fff"; c.fill(); c.lineWidth = 2; c.strokeStyle = "#071019"; c.stroke(); }
    legend();
  }
  function legend() {
    const box = $("#imp-legend"); box.textContent = ""; const L = IMP.layer;
    const row = (color, text) => el("div", { class: "row" }, [el("i", { style: `background:${color}` }), text]);
    if (L === "wind") { box.append(el("b", { text: "Highest modelled wind so far" })); for (const [, t, v] of WINDC) box.append(row(`var(${v})`, t)); }
    if (L === "risk") { box.append(el("b", { text: "Relative risk index" })); for (const [, t, c] of RISK) box.append(row(c, t)); }
    if (L === "pop") { box.append(el("b", { text: "People per 0.1° cell" }), el("div", { class: "bar", style: `background:linear-gradient(90deg,${[0.15, .4, .65, .9].map((x) => d3.interpolateMagma(x)).join(",")})` }), el("div", { class: "ends" }, [el("span", { text: "10" }), el("span", { text: "1,000" }), el("span", { text: "100,000+" })])); }
    if (L === "vuln") { box.append(el("b", { text: "Vulnerability (country level)" }), el("div", { class: "bar", style: `background:linear-gradient(90deg,${[0, .5, 1].map((x) => d3.interpolateRdYlGn(1 - x)).join(",")})` }), el("div", { class: "ends" }, [el("span", { text: "lower" }), el("span", { text: "higher" })])); }
  }
  $$(".imp-layers button").forEach((b) => b.addEventListener("click", () => {
    IMP.layer = b.dataset.layer; for (const x of $$(".imp-layers button")) x.setAttribute("aria-pressed", String(x === b)); drawImpact();
  }));
  impCanvas.addEventListener("mousemove", (e) => {
    const d = IMP.data; if (!d || !IMP.geo) return;
    const r = impCanvas.getBoundingClientRect(), ll_ = IMP.geo.proj.invert([e.clientX - r.left, e.clientY - r.top]); if (!ll_) return;
    let [lon, lat] = ll_; const g = d.grid; while (lon < g.lon_left) lon += 360; while (lon >= g.lon_left + 360) lon -= 360;
    const i = Math.floor((g.lat_top - lat) / g.d), j = Math.floor((lon - g.lon_left) / g.d);
    if (i < 0 || j < 0 || i >= g.ny || j >= g.nx) { set("#imp-tip", ""); return; }
    const k = i * g.nx + j, people = Math.pow(10, d.L.pop_log[k] / 40) - 1, wkt = d.L.wind_kt[k], vu = d.L.vuln[k], ri = d.L.risk[k] / 4;
    set("#imp-tip", `${ll(lat, lon)} · wind ${wkt >= 34 ? `${wkt} kt` : "below 34 kt"} · about ${fmtN(people)} people` +
      (vu <= 100 ? ` · vulnerability ${(vu / 100).toFixed(2)}` : "") + (ri >= 0.05 ? ` · risk ${ri.toFixed(1)} (${riskClass(ri)[1]})` : ""));
  });
  impCanvas.addEventListener("mouseleave", () => set("#imp-tip", ""));
  function impactPanel() {
    const box = $("#imp-box"), d = IMP.data;
    if (!SEL || !SEL.impact) { box.hidden = true; return; }
    box.hidden = false;
    if (!d || IMP.s !== SEL) { set("#imp-34", "…"); set("#imp-64", "…"); $("#imp-countries").textContent = ""; return; }
    const ul = $("#imp-countries"); ul.textContent = "";
    for (const c of (d.countries || []).slice(0, 4)) ul.append(el("li", {}, [c.name, el("span", { text: `${fmtN(c.pop34)} in ≥ 34 kt${c.pop64 >= 1000 ? `, ${fmtN(c.pop64)} in ≥ 64 kt` : ""}` })]));
    if (!(d.countries || []).length) ul.append(el("li", { text: "No populated land in gale-force winds so far." }));
    impactTime();
  }
  function impactTime() {
    const d = IMP.data; if (!d || IMP.s !== SEL || isNaN(T)) return;
    const ts = d.series._t; let i = -1; for (let k = 0; k < ts.length; k++) if (ts[k] <= T + 1) i = k;
    set("#imp-34", i < 0 ? "0" : fmtN(d.series.pop34[i])); set("#imp-64", i < 0 ? "0" : fmtN(d.series.pop64[i]));
    if (STAGE === "impact") drawImpact();
  }

  // ================================================================== CENTRAL TIME
  function setTime(t, src) {
    if (!SEL || isNaN(t)) return;
    T = Math.min(Math.max(t, SEL._t0), SEL._t1);
    set("#t-time", fmt(T));
    const latest = Math.abs(T - SEL._t1) < H / 2;
    set("#t-note", latest ? "latest advisory" : `${Math.round((SEL._t1 - T) / H)} h before the latest advisory`);
    if (EARTH) EARTH.setTime(latest && SEL.status === "active" ? null : T);
    timelineTime(); inspectorTime(); mapTime(); impactTime();
    if (STAGE === "flow") {
      const inside = inWindow(SEL, T);
      set("#flow-note", inside ? "" : `The 3D video covers ${fmt(SEL._ws, false)} to ${fmt(SEL._we)}. Move the timeline into that period.`);
      if (src !== "video" && !isNaN(SEL._ws) && video.readyState >= 1) seekVideo(SEL, video, T);
    }
    updateHash();
  }
  const updateHash = () => { if (!SEL) return; try { history.replaceState(null, "", `#storm=${SEL.key}&view=${STAGE}`); } catch (e) { /* ignore */ } };

  // ---- playback: the video drives time inside its window, a clock drives it elsewhere
  const PL = { on: false, last: 0 };
  const rateHps = () => (SEL ? SEL._fps / SEL._fph : 15) * +$("#t-speed").value;     // hours of storm time per second
  function play() {
    if (!SEL) return; if (T >= SEL._t1 - H / 4) setTime(STAGE === "flow" && !isNaN(SEL._ws) ? SEL._ws : SEL._t0);
    PL.on = true; PL.last = 0; $("#t-play").textContent = "❚❚"; $("#t-play").setAttribute("aria-label", "Pause"); requestAnimationFrame(loop);
  }
  function pause() { PL.on = false; video.pause(); $("#t-play").textContent = "▶"; $("#t-play").setAttribute("aria-label", "Play"); }
  function loop(now) {
    if (!PL.on || !SEL) return;
    const dt = Math.min(0.25, (now - (PL.last || now)) / 1000); PL.last = now;
    if (STAGE === "flow" && inWindow(SEL, T) && T < SEL._we - H / 4) {
      if (video.paused) { seekVideo(SEL, video, T); video.playbackRate = +$("#t-speed").value; video.play().catch(() => {}); }
      if (video.readyState >= 2 && !video.seeking && !video.paused) setTime(vTime(SEL, video), "video");   // otherwise wait for buffering
    } else {
      if (!video.paused) video.pause();
      setTime(T + rateHps() * dt * H);
    }
    if (T >= SEL._t1 - 1) { pause(); return; }
    requestAnimationFrame(loop);
  }
  const step = (h) => { pause(); setTime(T + h * H); };
  $("#t-play").addEventListener("click", () => (PL.on ? pause() : play()));
  $("#t-back").addEventListener("click", () => step(-1)); $("#t-fwd").addEventListener("click", () => step(1));
  $("#t-latest").addEventListener("click", () => { pause(); if (SEL) setTime(SEL._t1); });
  $("#t-speed").addEventListener("change", () => { video.playbackRate = +$("#t-speed").value; });
  video.addEventListener("loadedmetadata", () => { if (STAGE === "flow" && SEL) seekVideo(SEL, video, T); });

  // ================================================================== STAGE TABS
  function setStage(name) {
    if (name === "globe" && !EARTH) name = "map";
    STAGE = name;
    for (const b of $$(".stage-tabs button")) b.setAttribute("aria-selected", String(b.dataset.stage === name));
    for (const p of ["globe", "flow", "map", "compare", "impact"]) $(`#pane-${p}`).hidden = p !== name;
    if (name !== "flow") video.pause();
    if (name !== "compare") { $("#cmp-va").pause(); $("#cmp-vb").pause(); }
    if (name === "map") { M.s = null; drawMap(); }
    if (name === "compare") setupCompare(true);
    if (name === "impact") { IMP.geo = null; drawImpact(); }
    if (SEL) setTime(T);
  }
  $$(".stage-tabs button").forEach((b) => b.addEventListener("click", () => setStage(b.dataset.stage)));

  // ================================================================== SELECTION
  function select(s, opts = {}) {
    if (!s) return;
    const keep = opts.keepTime && !isNaN(T) && T >= s._t0 && T <= s._t1;
    pause(); SEL = s;
    video.poster = s.poster; video.src = s.video; video.load(); video.playbackRate = +$("#t-speed").value;
    globeStorms(); fillInspector(); loadEnv(s); loadImpact(s); drawTimeline(); drawSide();
    M.s = null; if (STAGE === "map") drawMap();
    const t = keep ? T : s._t1; setTime(t);
    if (EARTH) { const p = at(s, t); if (p) EARTH.focus(p.lat, p.lon, opts.instant ? 1 : 1300); }
    const gi = $("#guide-img"); if (gi && !gi.getAttribute("src")) gi.src = s.poster;
    if (STAGE === "compare") setupCompare(true);
  }
  const navList = () => (ACTIVE.length ? ACTIVE : ALL);
  const move = (d) => { const nl = navList(); if (nl.length < 2 || !SEL) return; const i = Math.max(0, nl.indexOf(SEL)); select(nl[(i + d + nl.length) % nl.length]); };

  // ================================================================== LEFT: STORM LISTS
  function sparkSVG(s) {
    const P = s._pts; if (P.length < 2) return null; const W = 200, Hs = 22, mx = Math.max(64, d3.max(P, (p) => p.kt));
    const X = (t) => 1 + (W - 2) * (t - s._t0) / Math.max(1, s._t1 - s._t0), Y = (k) => Hs - 2 - (Hs - 4) * k / mx;
    const svg = sv("svg", { viewBox: `0 0 ${W} ${Hs}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    for (let i = 1; i < P.length; i++) svg.append(sv("line", { x1: X(P[i - 1].t), y1: Y(P[i - 1].kt), x2: X(P[i].t), y2: Y(P[i].kt), stroke: col(P[i].kt), "stroke-width": 2 }));
    return svg;
  }
  function stormCard(s, mini) {
    const trend = s.status !== "active" ? `peak ${s.peak_kt} kt` : s.dv24 >= 10 ? `▲ ${s.dv24} kt / 24 h` : s.dv24 <= -10 ? `▼ ${-s.dv24} kt / 24 h` : "steady";
    const b = el("button", { type: "button", class: "scard", style: `--c:var(${CATV(s.vmax_kt)})`, "aria-current": String(s === SEL) }, [
      el("strong", { text: s.name }), el("span", { class: "kt" }, [`${s.status === "active" ? s.vmax_kt : s.peak_kt}`, el("small", { text: " kt" })]),
      el("span", { class: "meta", text: `${s.basin_name} · ${trend}${s.ri ? " · RI" : ""}${s.status !== "active" ? ` · ${s.year}` : ""}` }),
      mini ? null : sparkSVG(s)]);
    b.addEventListener("click", () => select(s, { keepTime: true }));
    return el("li", {}, [b]);
  }
  function drawSide() {
    const n = ACTIVE.length, W_ = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"];
    set("#headline", n ? `${W_[n] || n} active storm${n > 1 ? "s" : ""}` : "No active storms");
    const ends = ACTIVE.map((s) => s._we).filter((x) => !isNaN(x));
    set("#status", ends.length ? `3D videos to ${fmt(Math.max(...ends))}` : "Showing the most recent storms. Updated every morning.");
    const fb = $("#basins"); fb.textContent = "";
    const basins = [...new Set(ACTIVE.map((s) => s.basin_name))];
    if (basins.length > 1) for (const b of ["", ...basins]) {
      const btn = el("button", { type: "button", "aria-pressed": String(BASIN === b), text: b || "All" }); btn.addEventListener("click", () => { BASIN = b; drawSide(); }); fb.append(btn);
    }
    const al = $("#active-list"); al.textContent = "";
    const list = ACTIVE.filter((s) => !BASIN || s.basin_name === BASIN);
    if (!list.length) al.append(el("li", { class: "empty", text: "No storms are active right now. Recent storms are below and in the archive." }));
    for (const s of list) al.append(stormCard(s));
    const pl = $("#pinned-list"); pl.textContent = "";
    const pins = PINS.map((k) => ALL.find((s) => s.key === k)).filter(Boolean);
    if (!pins.length) pl.append(el("li", { class: "empty", text: "Pin storms to keep them here and compare them." }));
    for (const s of pins) pl.append(stormCard(s, true));
    const rl = $("#recent-list"); rl.textContent = "";
    for (const s of ARCH.slice().sort((a, b) => toMs(b.last_seen) - toMs(a.last_seen)).slice(0, 5)) rl.append(stormCard(s, true));
    if (!ARCH.length) rl.append(el("li", { class: "empty", text: "Storms appear here after they end." }));
  }

  // ================================================================== SEARCH (Ctrl K)
  const q = $("#q"), qr = $("#q-results"); let qSel = 0, qList = [];
  function runSearch() {
    const v = q.value.trim().toLowerCase(); qr.textContent = ""; qSel = 0;
    if (!v) { qr.hidden = true; return; }
    qList = ALL.filter((s) => [s.name, s.sid, s.basin_name, String(s.year), s.label, catName(s.peak_kt)].some((x) => String(x).toLowerCase().includes(v))).slice(0, 8);
    if (!qList.length) { qr.append(el("li", { text: "No storms match" })); qr.hidden = false; return; }
    qList.forEach((s, i) => { const li = el("li", { role: "option", "aria-selected": String(i === 0) }, [el("b", { text: `${s.name} (${s.year})` }), el("span", { text: `${s.basin_name}, peak ${s.peak_kt} kt${s.status === "active" ? ", active" : ""}` })]);
      li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(i); }); qr.append(li); });
    qr.hidden = false;
  }
  const pick = (i) => { const s = qList[i]; if (s) { select(s); q.value = ""; qr.hidden = true; q.blur(); } };
  q.addEventListener("input", runSearch);
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); qSel = (qSel + (e.key === "ArrowDown" ? 1 : qList.length - 1)) % Math.max(1, qList.length);
      $$("li", qr).forEach((li, i) => li.setAttribute("aria-selected", String(i === qSel))); }
    if (e.key === "Enter") pick(qSel);
    if (e.key === "Escape") { q.value = ""; qr.hidden = true; q.blur(); }
  });
  q.addEventListener("blur", () => setTimeout(() => (qr.hidden = true), 150));

  // ================================================================== COMPARE (stage 4)
  const CM = { va: $("#cmp-va"), vb: $("#cmp-vb"), a: null, b: null, raf: 0 };
  function miniChart(boxSel, s) {
    const box = $(boxSel); box.textContent = ""; if (!s || s._pts.length < 2 || isNaN(s._ws)) return null;
    const W = Math.max(240, box.clientWidth || 360), Hh = 70, m = { l: 26, r: 6, t: 6, b: 14 };
    const P = s._pts.filter((p) => p.t >= s._ws - 6 * H && p.t <= s._we + 6 * H); if (P.length < 2) return null;
    const t0 = s._ws, t1 = s._we, ymax = Math.max(70, d3.max(P, (p) => p.kt) * 1.15);
    const X = (t) => m.l + (W - m.l - m.r) * (t - t0) / Math.max(1, t1 - t0), Y = (k) => Hh - m.b - (Hh - m.t - m.b) * k / ymax;
    const svg = sv("svg", { viewBox: `0 0 ${W} ${Hh}`, role: "img", "aria-label": `Official intensity of ${s.name} during its video` });
    const cid = `clip-${boxSel.slice(1)}`, defs = sv("defs"), cp = sv("clipPath", { id: cid }); cp.append(sv("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: Hh })); defs.append(cp); svg.append(defs);
    const g = sv("g", { "clip-path": `url(#${cid})` });
    for (let i = 1; i < P.length; i++) g.append(sv("line", { x1: X(P[i - 1].t), y1: Y(P[i - 1].kt), x2: X(P[i].t), y2: Y(P[i].kt), stroke: col(Math.max(P[i - 1].kt, P[i].kt)), "stroke-width": 3, "stroke-linecap": "round" }));
    svg.append(g);
    for (const k of [34, 64, 96]) if (k < ymax * .95) { const tx = sv("text", { x: m.l - 3, y: Y(k) + 3, "text-anchor": "end", fill: "var(--ink-2)", "font-size": 10 }); tx.textContent = k; svg.append(tx); }
    const head = sv("line", { x1: -9, x2: -9, y1: m.t, y2: Hh - m.b, stroke: "#fff", "stroke-width": 2 }); svg.append(head); box.append(svg);
    return { X, head, s };
  }
  function setupCompare(load) {
    const A = $("#cmp-a"), B = $("#cmp-b"), list = [...ACTIVE, ...ARCH.slice().sort((a, b) => toMs(b.last_seen) - toMs(a.last_seen))];
    const fill = (sel) => { const keep = sel.value; sel.textContent = ""; for (const s of list) sel.append(el("option", { value: s.key, text: `${s.name} (${s.year}), peak ${s.peak_kt} kt` })); if (keep) sel.value = keep; };
    fill(A); fill(B);
    if (load || !A.value) {
      A.value = SEL ? SEL.key : (list[0] || {}).key;
      const other = PINS.map((k) => list.find((s) => s.key === k)).find((s) => s && s.key !== A.value) || list.find((s) => s.key !== A.value && s.ri !== (SEL && SEL.ri)) || list.find((s) => s.key !== A.value);
      if (other) B.value = other.key;
    }
    if (STAGE === "compare") loadCompare();
  }
  function loadCompare() {
    CM.a = ALL.find((s) => s.key === $("#cmp-a").value) || null; CM.b = ALL.find((s) => s.key === $("#cmp-b").value) || null;
    for (const [v, s, cap_] of [[CM.va, CM.a, "#cmp-ia"], [CM.vb, CM.b, "#cmp-ib"]]) {
      v.pause(); if (s && !v.src.endsWith(s.video)) { v.poster = s.poster; v.src = s.video; v.load(); }
      const f = $(cap_); f.textContent = ""; if (s) f.append(el("strong", { text: `${s.name} (${s.year})` }), `${s.label}, ${s.basin_name}, peak ${s.peak_kt} kt${s.ri ? ", rapid intensification" : ""}`);
    }
    CM.ca = miniChart("#cmp-ca", CM.a); CM.cb = miniChart("#cmp-cb", CM.b); $("#cmp-play").textContent = "▶ Play both";
  }
  const cmpDur = () => Math.max(CM.va.duration || 0, CM.vb.duration || 0);
  function cmpSync() {
    for (const [c, v] of [[CM.ca, CM.va], [CM.cb, CM.vb]]) if (c) { const x = c.X(vTime(c.s, v)); c.head.setAttribute("x1", x); c.head.setAttribute("x2", x); }
    const d = cmpDur(); if (d) $("#cmp-range").value = Math.round(1000 * (CM.va.currentTime || 0) / d);
  }
  const cloop = () => { cmpSync(); CM.raf = requestAnimationFrame(cloop); };
  $("#cmp-a").addEventListener("change", loadCompare); $("#cmp-b").addEventListener("change", loadCompare);
  $("#cmp-play").addEventListener("click", () => {
    if (!CM.va.paused || !CM.vb.paused) { CM.va.pause(); CM.vb.pause(); cancelAnimationFrame(CM.raf); $("#cmp-play").textContent = "▶ Play both"; return; }
    const t = Math.min(CM.va.currentTime || 0, CM.vb.currentTime || 0); CM.va.currentTime = t; CM.vb.currentTime = t;
    Promise.all([CM.va.play(), CM.vb.play()]).catch(() => {}); $("#cmp-play").textContent = "❚❚ Pause both"; cancelAnimationFrame(CM.raf); CM.raf = requestAnimationFrame(cloop);
  });
  for (const v of [CM.va, CM.vb]) { v.addEventListener("ended", () => { $("#cmp-play").textContent = "▶ Play both"; cancelAnimationFrame(CM.raf); cmpSync(); }); v.addEventListener("seeked", cmpSync); }
  $("#cmp-range").addEventListener("input", (e) => { const t = +e.target.value / 1000 * cmpDur(); CM.va.pause(); CM.vb.pause(); $("#cmp-play").textContent = "▶ Play both";
    for (const v of [CM.va, CM.vb]) if (v.duration) v.currentTime = Math.min(t, v.duration - 0.05); });

  // ================================================================== DRAWERS
  const dlg = $("#dlg");
  const TITLES = { season: "Season at a glance", archive: "Archive", guide: "How to read a 3D video", method: "Impact method: hazard, exposure and vulnerability", about: "About" };
  function openDrawer(name) {
    for (const k of Object.keys(TITLES)) $(`#d-${k}`).hidden = k !== name;
    set("#dlg-title", name === "season" ? `${DATA && DATA.generated ? new Date(toMs(DATA.generated)).getUTCFullYear() : ""} season at a glance` : TITLES[name]);
    if (name === "season") drawSeason(); if (name === "archive") drawArchive();
    if (name === "guide" && SEL) $("#guide-img").src = SEL.poster;
    pause(); if (!dlg.open) dlg.showModal();
  }
  $$("[data-open]").forEach((b) => b.addEventListener("click", () => openDrawer(b.dataset.open)));
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  function drawSeason() {
    const year = DATA && DATA.generated ? new Date(toMs(DATA.generated)).getUTCFullYear() : new Date().getUTCFullYear();
    const S = ALL.filter((s) => s.year === year), ace = $("#s-ace"), cat = $("#s-cat"), top = $("#s-top"); ace.textContent = ""; cat.textContent = ""; top.textContent = "";
    if (!S.length) { ace.append(el("p", { class: "muted", text: "No storms rendered yet this year." })); return; }
    const byB = d3.rollups(S, (v) => d3.sum(v, (s) => s._ace), (s) => s.basin_name).sort((a, b) => b[1] - a[1]), mx = d3.max(byB, (d) => d[1]) || 1;
    for (const [b, v] of byB) ace.append(el("div", { class: "hbar" }, [el("span", { text: b }), el("div", { style: `width:${Math.max(2, v / mx * 100)}%` }), el("b", { text: v.toFixed(1) })]));
    const groups = [["TD", 0, 34, "--td"], ["TS", 34, 64, "--ts"], ["1", 64, 83, "--c1"], ["2", 83, 96, "--c2"], ["3", 96, 113, "--c3"], ["4", 113, 137, "--c4"], ["5", 137, 999, "--c5"]];
    const counts = groups.map(([n, a, b, c]) => [n, S.filter((s) => s.peak_kt >= a && s.peak_kt < b).length, c]), cm = Math.max(1, ...counts.map((c) => c[1]));
    const bars = el("div", { class: "catbars", role: "img", "aria-label": counts.map((c) => `${c[0]}: ${c[1]}`).join(", ") });
    for (const [n, k, c] of counts) bars.append(el("div", {}, [el("b", { text: k }), el("i", { style: `height:${k / cm * 100}%;--c:var(${c})` }), el("span", { text: n })]));
    cat.append(bars);
    for (const s of S.slice().sort((a, b) => b.peak_kt - a.peak_kt).slice(0, 8)) {
      const b = el("button", { type: "button", text: s.name }); b.addEventListener("click", () => { dlg.close(); select(s); });
      top.append(el("li", {}, [b, ` ${s.peak_kt} kt, ${s.basin_name}${s.ri ? ", RI" : ""}`]));
    }
  }
  function drawArchive() {
    const box = $("#archive-list"); box.textContent = "";
    const y = $("#f-year").value, b = $("#f-basin").value, sort = $("#f-sort").value;
    const list = ARCH.filter((s) => (!y || String(s.year) === y) && (!b || s.basin_name === b));
    list.sort(sort === "peak" ? (p, q_) => q_.peak_kt - p.peak_kt : sort === "ace" ? (p, q_) => q_._ace - p._ace : (p, q_) => toMs(q_.last_seen) - toMs(p.last_seen));
    if (!list.length) { box.append(el("p", { class: "muted", text: ARCH.length ? "No storms match these filters." : "Storms appear here after they end." })); return; }
    for (const s of list) {
      const wrap = el("span", { class: "imgwrap" }, [el("img", { src: s.poster, alt: "", loading: "lazy" })]);
      const c = el("button", { type: "button", class: "gcard", style: `--c:var(${CATV(s.peak_kt)})` }, [wrap,
        el("div", {}, [el("strong", { text: `${s.name} (${s.year})` }), el("span", { text: `${s.basin_name}, peak ${s.peak_kt} kt, ACE ${s._ace.toFixed(1)}${s.ri ? ", RI" : ""}` })])]);
      c.addEventListener("click", () => { dlg.close(); select(s); });
      if (!reduced && matchMedia("(hover: hover)").matches) {
        c.addEventListener("mouseenter", () => { if (wrap.querySelector("video")) return; const v = el("video", { muted: true, loop: true, playsinline: true, src: s.video }); v.muted = true; wrap.append(v); v.play().catch(() => {}); });
        c.addEventListener("mouseleave", () => { const v = wrap.querySelector("video"); if (v) { v.pause(); v.remove(); } });
      }
      box.append(c);
    }
  }
  for (const id of ["#f-year", "#f-basin", "#f-sort"]) $(id).addEventListener("change", drawArchive);
  const SPOTS = [
    [36, 3, "Title", "Storm name, agency ID and the analysis time of this frame. Frames are one hour apart."],
    [35, 42, "3D flow box", "The box follows the storm from 900 hPa near the surface up to 500 hPa. Streamlines and tracers are coloured by wind speed; the red surface is the vortex core, grey patches are strong ascent, and the white line joins the circulation centre at each level."],
    [70, 45, "Wind speed scale", "Colour scale for streamlines and tracers, in m/s."],
    [88, 13, "Diagnostics", "Official intensity, GFS maximum wind, radius of maximum wind, shear, tilt, local solar time and centre position for this frame."],
    [86, 77, "Shear and tilt compass", "The orange arrow is 850–500 hPa shear; the white arrow is where the 500 hPa centre sits relative to 850 hPa. Tilt pointing downshear, then precessing and aligning, is a classic path to rapid intensification."],
    [35, 88, "Intensity timeline", "White: official maximum wind. Blue: GFS 900 hPa maximum wind. Red shading: rapid intensification (official +30 kt in 24 h)."],
    [40, 70, "Day and night floor", "The floor is shaded by the real position of the sun, so you can follow the diurnal cycle."],
  ];
  const gf = $("#guide-frame"), gt = $("#guide-text");
  const showSpot = (i) => { for (const h of $$(".hot", gf)) h.setAttribute("aria-pressed", String(+h.dataset.i === i)); gt.textContent = ""; gt.append(el("h3", { text: `${i + 1}. ${SPOTS[i][2]}` }), el("p", { text: SPOTS[i][3] })); };
  SPOTS.forEach(([x, y, name], i) => { const h = el("button", { type: "button", class: "hot", "data-i": i, style: `left:${x}%;top:${y}%`, "aria-label": name, "aria-pressed": "false", text: i + 1 }); h.addEventListener("click", () => showSpot(i)); gf.append(h); });
  showSpot(1);

  // ================================================================== KEYBOARD
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); q.focus(); q.select(); return; }
    if (dlg.open) return;
    const t = e.target; if (t.closest && t.closest("input, select, textarea")) return;               // typing is never hijacked
    if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && t.closest && t.closest("#earth-canvas, [role=tab]")) return;   // those use arrows themselves
    if (e.key === " " && t.tagName !== "BUTTON") { e.preventDefault(); if (PL.on) pause(); else play(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); step(1); } else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
    else if (e.key === "]") move(1); else if (e.key === "[") move(-1);
    else if (["1", "2", "3", "4", "5"].includes(e.key)) setStage(["globe", "flow", "map", "compare", "impact"][+e.key - 1]);
  });
  $("#brand").addEventListener("click", (e) => { e.preventDefault(); const first = ACTIVE[0] || ALL[0]; setStage(EARTH ? "globe" : "map"); if (first) select(first); });

  // ================================================================== LOAD
  const land = (f) => fetch(`vendor/${f}`).then((r) => r.json()).then((t) => topojson.feature(t, t.objects.land)).catch(() => null);
  land("land-110m.json").then((g) => { LAND110 = g; if (STAGE === "map") drawMap(); });
  fetch(`data/storms.json?t=${Date.now()}`)
    .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then((d) => {
      DATA = d; ALL = (d.storms || []).map(prep).filter((s) => s._pts.length);
      ACTIVE = ALL.filter((s) => s.status === "active").sort((a, b) => b.vmax_kt - a.vmax_kt);
      ARCH = ALL.filter((s) => s.status !== "active");
      for (const y of [...new Set(ARCH.map((s) => s.year))].sort().reverse()) $("#f-year").append(el("option", { value: y, text: y }));
      for (const b of [...new Set(ARCH.map((s) => s.basin_name))].sort()) $("#f-basin").append(el("option", { value: b, text: b }));
      set("#generated", d.generated ? `Dashboard rebuilt ${fmt(toMs(d.generated))}.` : "");
      const basins = new Set(ALL.map((s) => s.basin_name)); if (ALL.length) set("#site-stats", `This site holds ${ALL.length} storm${ALL.length > 1 ? "s" : ""} from ${basins.size} basin${basins.size > 1 ? "s" : ""}.`);
      drawSide(); setupCompare();
      const hs = new URLSearchParams(location.hash.slice(1));
      const first = (hs.get("storm") && ALL.find((s) => s.key === hs.get("storm"))) || ACTIVE[0] || ARCH.slice().sort((a, b) => toMs(b.last_seen) - toMs(a.last_seen))[0];
      const view = hs.get("view");
      if (first) select(first, { instant: true });
      else { set("#i-name", "No storms yet"); $("#itab-summary").append(el("p", { text: "Run the daily workflow once to build the storm list." })); }
      setStage(["globe", "flow", "map", "compare", "impact"].includes(view) ? view : STAGE);
      land("land-50m.json").then((g) => { if (g) { LAND50 = g; if (STAGE === "map") drawMap(); } });
    })
    .catch((err) => { console.error(err); set("#status", "The storm list could not be loaded yet. It is created by the daily update."); });
})();
