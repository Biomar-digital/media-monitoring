/* BioMar Media Monitor dashboard.
   Loads data/dashboard.json (or the encrypted dashboard.enc.json), aggregates in the browser,
   and renders plain-SVG charts. All data-derived text is inserted with textContent. */
(() => {
  "use strict";

  // ---------------------------------------------------------------------------------
  // Small DOM helpers
  // ---------------------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "style") el.setAttribute("style", v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null) continue;
      if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid) el.append(kid);
    return el;
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* storage unavailable */ } },
    sget(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
    sset(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  };

  // ---------------------------------------------------------------------------------
  // Formatting
  // ---------------------------------------------------------------------------------
  const nf = new Intl.NumberFormat("en-GB");
  const fmt = (n) => (Math.abs(n) >= 10000 ? new Intl.NumberFormat("en-GB", { notation: "compact", maximumFractionDigits: 1 }).format(n) : nf.format(n));
  const pct = (x) => `${Math.round(x * 100)}%`;
  const signed = (n) => (n > 0 ? `+${n}` : `${n}`);
  let regionNames;
  try { regionNames = new Intl.DisplayNames(["en"], { type: "region" }); } catch { regionNames = null; }
  const countryName = (c) => (!c ? "International / unknown" : (regionNames && regionNames.of(c)) || c);
  const dateFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
  const dateTimeFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const hourFmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
  function relTime(iso) {
    const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 60) return `${Math.max(mins, 1)} min ago`;
    if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
    return dateFmt.format(new Date(iso));
  }
  const toneOf = (score) => (score >= 0.25 ? "positive" : score <= -0.25 ? "negative" : "neutral");

  // ---------------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------------
  let DATA = null;
  let ENT = {};          // id -> entity
  const state = {
    days: Number(store.get("mm.days")) || 30,
    region: "",
    trend: null,         // Set of entity ids shown in the trend chart (null = auto)
    feed: { entity: "", tone: "", q: "", page: 0 },
  };
  const PAGE = 25;

  const colorOf = (id) => {
    const e = ENT[id];
    return e && e.color_slot ? `var(--series-${e.color_slot})` : "var(--de-emph)";
  };

  // ---------------------------------------------------------------------------------
  // Data loading (plain or encrypted)
  // ---------------------------------------------------------------------------------
  const b64 = (str) => Uint8Array.from(atob(str), (c) => c.charCodeAt(0));
  async function decrypt(env, password) {
    const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
    const key = await crypto.subtle.deriveKey(
      { name: "PBKDF2", hash: "SHA-256", salt: b64(env.salt), iterations: env.iterations },
      base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
    const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(env.iv) }, key, b64(env.ciphertext));
    return JSON.parse(new TextDecoder().decode(plain));
  }

  async function load() {
    const bust = `?t=${Date.now()}`;
    const plain = await fetch(`data/dashboard.json${bust}`).catch(() => null);
    if (plain && plain.ok) return start(await plain.json());
    const enc = await fetch(`data/dashboard.enc.json${bust}`).catch(() => null);
    if (!enc || !enc.ok) {
      $("updated").textContent = "No data yet. Run `python -m monitor run` to collect coverage.";
      return;
    }
    const env = await enc.json();
    const saved = store.sget("mm.pw") || store.get("mm.pw");
    if (saved) {
      try { return start(await decrypt(env, saved)); } catch { store.del("mm.pw"); }
    }
    $("updated").textContent = "Locked";
    $("gate").hidden = false;
    $("gatePw").focus();
    $("gateForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const pw = $("gatePw").value;
      $("gateError").textContent = "Checking…";
      try {
        const data = await decrypt(env, pw);
        store.sset("mm.pw", pw);
        if ($("gateRemember").checked) store.set("mm.pw", pw);
        $("gate").hidden = true;
        start(data);
      } catch {
        $("gateError").textContent = "That password didn't work.";
      }
    });
  }

  function start(data) {
    DATA = data;
    ENT = Object.fromEntries(data.entities.map((e) => [e.id, e]));
    for (const m of data.mentions) m._t = new Date(m.p).getTime();
    $("main").hidden = false;
    const since = data.tracking_since ? ` since ${dateFmt.format(new Date(data.tracking_since))}` : "";
    $("updated").textContent = `Updated ${dateTimeFmt.format(new Date(data.generated_at))} · ${nf.format(data.mentions.length)} articles tracked${since}`;
    setupFilters();
    renderAll();
    let t;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(renderCharts, 120); });
  }

  // ---------------------------------------------------------------------------------
  // Slicing
  // ---------------------------------------------------------------------------------
  const DAY = 86400000;
  const now = () => Date.now();
  function inRange(m, from, to) { return m._t >= from && m._t < to; }
  function slice(offsetPeriods = 0) {
    const span = state.days * DAY;
    const to = now() - offsetPeriods * span;
    const from = to - span;
    return DATA.mentions.filter((m) => inRange(m, from, to) && (!state.region || (m.c || "") === state.region));
  }
  const ofType = (t) => DATA.entities.filter((e) => e.type === t);
  const brandId = () => DATA.brand;
  const companies = () => [ENT[brandId()], ...ofType("competitor")];

  function countBy(ms, ids) {
    const c = Object.fromEntries(ids.map((i) => [i, 0]));
    for (const m of ms) for (const e of m.e) if (e in c) c[e]++;
    return c;
  }
  function toneBy(ms, id) {
    const t = { positive: 0, neutral: 0, negative: 0 };
    for (const m of ms) if (m.e.includes(id)) t[toneOf(m.se[id] ?? 0)]++;
    const n = t.positive + t.neutral + t.negative;
    return { ...t, n, net: n ? Math.round(((t.positive - t.negative) / n) * 100) : 0 };
  }

  // ---------------------------------------------------------------------------------
  // Filters
  // ---------------------------------------------------------------------------------
  function setupFilters() {
    const seg = $("rangeSeg");
    for (const b of seg.querySelectorAll("button")) {
      if (Number(b.dataset.days) > DATA.history_days) b.hidden = true;
      b.addEventListener("click", () => {
        state.days = Number(b.dataset.days);
        store.set("mm.days", String(state.days));
        state.feed.page = 0;
        renderAll();
      });
    }
    const countries = [...new Set(DATA.mentions.map((m) => m.c || ""))];
    countries.sort((a, b) => countryName(a).localeCompare(countryName(b)));
    const sel = $("regionSel");
    for (const c of countries) sel.append(h("option", { value: c, text: countryName(c) }));
    sel.addEventListener("change", () => { state.region = sel.value; state.feed.page = 0; renderAll(); });

    const fe = $("feedEntity");
    fe.append(h("option", { value: "", text: "All companies & people" }));
    for (const t of ["brand", "executive", "competitor"]) {
      const g = h("optgroup", { label: { brand: "BioMar", executive: "Executives", competitor: "Competitors" }[t] });
      for (const e of ofType(t)) g.append(h("option", { value: e.id, text: e.name }));
      fe.append(g);
    }
    fe.addEventListener("change", () => { state.feed.entity = fe.value; state.feed.page = 0; renderFeed(); });
    $("feedTone").addEventListener("change", (e) => { state.feed.tone = e.target.value; state.feed.page = 0; renderFeed(); });
    let t;
    $("feedSearch").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { state.feed.q = e.target.value.trim().toLowerCase(); state.feed.page = 0; renderFeed(); }, 150); });
    $("feedCsv").addEventListener("click", downloadCsv);
  }

  function renderAll() {
    for (const b of $("rangeSeg").querySelectorAll("button")) b.setAttribute("aria-checked", String(Number(b.dataset.days) === state.days));
    renderBriefing();
    renderTiles();
    renderCharts();
    renderExecutives();
    renderAttention();
    renderFeed();
    const methods = DATA.analysis_methods || {};
    const lex = methods.lexicon || 0;
    $("foot").textContent =
      `Sources: Google News (17 regional editions), GDELT and trade-press RSS. Includes articles where the company appears in the text but not the headline (marked “Full-text match”). ` +
      `Sentiment and topics are AI-assessed from headlines and snippets` +
      (lex ? `; ${nf.format(lex)} articles were scored by the keyword fallback because no Claude API key was configured.` : ".");
  }
  function renderCharts() {
    renderSov();
    renderSentiment();
    renderTrend();
    renderGeo();
    renderTopics();
  }

  // ---------------------------------------------------------------------------------
  // Briefing
  // ---------------------------------------------------------------------------------
  function bulletList(title, items) {
    if (!items || !items.length) return null;
    return h("div", {}, h("h3", { text: title }), h("ul", {}, items.map((x) => h("li", { text: x }))));
  }
  function renderBriefing() {
    const el = $("briefing");
    el.replaceChildren();
    const [b, ...older] = DATA.briefings || [];
    if (!b) { el.append(h("p", { class: "muted", text: "No briefing yet. One is written on each run." })); return; }
    el.append(
      h("p", { class: "eyebrow" },
        h("span", { id: "briefingTitle", text: `Daily briefing · ${dateFmt.format(new Date(b.generated_at))}` }),
        h("span", { class: "pill", text: b.method === "claude" ? "Written by Claude" : "Automatic summary" })),
      h("p", { class: "headline", text: b.headline }),
      h("p", { class: "summary", text: b.summary }),
      h("div", { class: "cols" },
        bulletList("BioMar & executives", b.brand),
        bulletList("Competitors", b.competitors),
        bulletList("Watch", b.watch && b.watch.length ? b.watch : ["Nothing flagged."])),
    );
    if (b.cite && b.cite.length) {
      const byId = Object.fromEntries(DATA.mentions.map((m) => [m.id, m]));
      const cited = b.cite.map((i) => byId[i]).filter(Boolean);
      if (cited.length) el.append(h("div", { class: "list", style: "margin-top:10px" }, h("h3", { text: "Key articles" }), cited.map(itemRow)));
    }
    if (older.length) {
      el.append(h("details", { class: "older" }, h("summary", { text: "Previous briefings" }),
        older.map((o) => h("p", {}, h("strong", { text: `${dateFmt.format(new Date(o.generated_at))}: ` }), o.headline))));
    }
  }

  // ---------------------------------------------------------------------------------
  // Stat tiles
  // ---------------------------------------------------------------------------------
  function delta(cur, prev, { unit = "", goodUp = true, label } = {}) {
    if (prev == null) return h("div", { class: "delta", text: label || "" });
    const d = cur - prev;
    const cls = d === 0 ? "" : (d > 0) === goodUp ? "up" : "down";
    const arrow = d > 0 ? "▲" : d < 0 ? "▼" : "";
    return h("div", { class: "delta" }, h("span", { class: cls, text: `${arrow} ${signed(d)}${unit}` }), ` vs previous ${periodName()}`);
  }
  const periodName = () => (state.days === 1 ? "24 hours" : `${state.days} days`);
  function renderTiles() {
    const cur = slice(0);
    // Only compare with the previous period if we were already collecting back then.
    const sinceT = DATA.tracking_since ? new Date(DATA.tracking_since).getTime() : now();
    const hasPrev = state.days * 2 <= DATA.history_days && now() - 2 * state.days * DAY >= sinceT - DAY;
    const prev = hasPrev ? slice(1) : null;
    const bid = brandId();
    const ids = companies().map((e) => e.id);
    const cc = countBy(cur, ids), pc = prev ? countBy(prev, ids) : null;
    const total = ids.reduce((a, i) => a + cc[i], 0);
    const ptotal = pc ? ids.reduce((a, i) => a + pc[i], 0) : 0;
    const sov = total ? cc[bid] / total : 0;
    const psov = pc && ptotal ? pc[bid] / ptotal : null;
    const tone = toneBy(cur, bid), ptone = prev ? toneBy(prev, bid) : null;
    const execIds = ofType("executive").map((e) => e.id);
    const ex = cur.filter((m) => m.e.some((e) => execIds.includes(e))).length;
    const pex = prev ? prev.filter((m) => m.e.some((e) => execIds.includes(e))).length : null;
    const attn = attentionItems(cur).length;

    const tiles = $("tiles");
    tiles.replaceChildren(
      h("div", { class: "tile hero" },
        h("p", { class: "label", text: `BioMar mentions · last ${periodName()}` }),
        h("div", { class: "value", text: fmt(cc[bid]) }),
        delta(cc[bid], pc ? pc[bid] : null, { label: "Comparison appears once a full previous period is tracked" }),
        sparkline(cur, bid)),
      h("div", { class: "tile" },
        h("p", { class: "label", text: "Share of voice" }),
        h("div", { class: "value", text: total ? pct(sov) : "–" }),
        delta(Math.round(sov * 100), psov == null ? null : Math.round(psov * 100), { unit: " pts" })),
      h("div", { class: "tile" },
        h("p", { class: "label", text: "Net sentiment" }),
        h("div", { class: "value", text: tone.n ? signed(tone.net) : "–" }),
        delta(tone.net, ptone && ptone.n ? ptone.net : null, { unit: " pts" })),
      h("div", { class: "tile" },
        h("p", { class: "label", text: "Executive mentions" }),
        h("div", { class: "value", text: fmt(ex) }),
        delta(ex, pex)),
      h("div", { class: "tile" },
        h("p", { class: "label", text: "Needs attention" }),
        h("div", { class: "value" }, attn ? h("span", { class: "attn-icon", "aria-hidden": "true", text: "! " }) : null, fmt(attn)),
        h("div", { class: "delta", text: attn ? "negative or high-importance items" : "nothing flagged" })),
    );
  }

  // Buckets for the current range: hourly for 24h, daily up to 45 days, weekly beyond.
  function buckets() {
    const end = now();
    const step = state.days === 1 ? 3600000 : state.days <= 45 ? DAY : 7 * DAY;
    const n = Math.ceil((state.days * DAY) / step);
    const start = end - n * step;
    return { start, step, n, label: (i) => {
      const d = new Date(start + i * step);
      return step === 3600000 ? hourFmt.format(d) : step === DAY ? dateFmt.format(d) : `Week of ${dateFmt.format(d)}`;
    } };
  }
  function series(ms, id, B) {
    const out = new Array(B.n).fill(0);
    for (const m of ms) {
      if (!m.e.includes(id)) continue;
      const i = Math.floor((m._t - B.start) / B.step);
      if (i >= 0 && i < B.n) out[i]++;
    }
    return out;
  }
  function sparkline(ms, id) {
    const B = buckets();
    const v = series(ms, id, B);
    const W = 220, H = 36, max = Math.max(1, ...v);
    const x = (i) => (B.n === 1 ? W / 2 : (i / (B.n - 1)) * (W - 8) + 4);
    const y = (val) => H - 4 - (val / max) * (H - 8);
    const d = v.map((val, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(val).toFixed(1)}`).join("");
    return s("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img", "aria-label": `Trend of BioMar mentions per ${B.step === DAY ? "day" : B.step === 3600000 ? "hour" : "week"}` },
      s("path", { d, fill: "none", stroke: "var(--de-emph)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }),
      s("circle", { cx: x(B.n - 1), cy: y(v[B.n - 1]), r: 4, fill: "var(--accent)", stroke: "var(--surface-1)", "stroke-width": 2 }));
  }

  // ---------------------------------------------------------------------------------
  // Tooltip
  // ---------------------------------------------------------------------------------
  const tip = $("tooltip");
  function showTip(evOrRect, title, rows) {
    tip.replaceChildren(h("div", { class: "tt-title", text: title }),
      ...rows.map((r) => h("div", { class: "row" },
        h("span", { class: "k" }, r.color ? h("span", { class: "ln", style: `background:${r.color}` }) : null, r.k),
        h("span", { class: "v", text: r.v }))));
    tip.hidden = false;
    let x, y;
    if (evOrRect.clientX != null) { x = evOrRect.clientX; y = evOrRect.clientY; }
    else { const r = evOrRect; x = r.left + r.width / 2; y = r.top; }
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = x + 14, top = y - th - 10;
    if (left + tw > window.innerWidth - 8) left = x - tw - 14;
    if (top < 8) top = y + 16;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${top}px`;
  }
  const hideTip = () => { tip.hidden = true; };

  // ---------------------------------------------------------------------------------
  // Chart primitives
  // ---------------------------------------------------------------------------------
  function niceMax(v) {
    if (v <= 4) return Math.max(v, 1);
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  // Counts are whole numbers, so pick a step count that divides the max evenly.
  function ticks(max) {
    const n = max <= 5 ? max : [4, 5, 3, 2].find((k) => Number.isInteger(max / k)) || 4;
    return Array.from({ length: n + 1 }, (_, i) => Math.round((max / n) * i));
  }
  // Bar path with a 4px rounded data end and a square baseline end.
  function hbarPath(x0, y, w, hgt, r = 4) {
    if (w <= 0) return "";
    r = Math.min(r, w, hgt / 2);
    const x1 = x0 + w;
    return `M${x0},${y}H${x1 - r}Q${x1},${y} ${x1},${y + r}V${y + hgt - r}Q${x1},${y + hgt} ${x1 - r},${y + hgt}H${x0}Z`;
  }
  function empty(el, msg = "No coverage in this period.") { el.replaceChildren(h("div", { class: "empty", text: msg })); }

  /** Horizontal bar chart. rows: [{label, value, color, valueText, tip:[{k,v}]}] */
  function hbars(el, rows, { labelW = 150, highlight } = {}) {
    if (!rows.length || rows.every((r) => !r.value)) return empty(el);
    const W = Math.max(280, el.clientWidth), rowH = 30, barH = 18, pad = 4;
    const lw = Math.min(labelW, W * 0.38), vw = 54;
    const H = rows.length * rowH + pad * 2;
    const max = Math.max(...rows.map((r) => r.value)) || 1;
    const x = (v) => (v / max) * (W - lw - vw - 8);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": rows.map((r) => `${r.label}: ${r.valueText}`).join("; ") });
    svg.append(s("line", { class: "baseline", x1: lw, x2: lw, y1: pad, y2: H - pad }));
    rows.forEach((r, i) => {
      const y = pad + i * rowH + (rowH - barH) / 2;
      const strong = highlight && r.id === highlight;
      svg.append(s("text", { x: lw - 8, y: y + barH / 2 + 4, "text-anchor": "end", class: strong ? "label-strong" : null, text: truncate(r.label, lw) }));
      const g = s("g", { class: "mark", tabindex: 0, "aria-label": `${r.label}: ${r.valueText}` });
      g.append(s("path", { d: hbarPath(lw + 1, y, Math.max(x(r.value), r.value ? 2 : 0), barH), fill: r.color }));
      g.append(s("rect", { class: "hit", x: 0, y: pad + i * rowH, width: W, height: rowH }));
      const enter = (ev) => { svg.classList.add("hover-dim"); g.classList.add("on"); showTip(ev.clientX != null ? ev : g.getBoundingClientRect(), r.label, r.tip || [{ k: "Mentions", v: r.valueText }]); };
      const leave = () => { svg.classList.remove("hover-dim"); g.classList.remove("on"); hideTip(); };
      g.addEventListener("pointermove", enter); g.addEventListener("pointerleave", leave);
      g.addEventListener("focus", enter); g.addEventListener("blur", leave);
      svg.append(g);
      svg.append(s("text", { x: lw + x(r.value) + 8, y: y + barH / 2 + 4, class: strong ? "label-strong" : null, style: "font-variant-numeric:tabular-nums", text: r.valueText }));
    });
    el.replaceChildren(svg);
  }
  function truncate(str, px) {
    const max = Math.floor(px / 7);
    return str.length > max ? `${str.slice(0, max - 1)}…` : str;
  }

  // ---------------------------------------------------------------------------------
  // Share of voice: emphasis on BioMar, competitors recede
  // ---------------------------------------------------------------------------------
  function renderSov() {
    const ms = slice(0);
    const ids = companies().map((e) => e.id);
    const c = countBy(ms, ids);
    const total = ids.reduce((a, i) => a + c[i], 0);
    const rows = ids.map((id) => ({
      id, label: ENT[id].name, value: c[id],
      color: id === brandId() ? "var(--accent)" : "var(--de-emph)",
      valueText: total ? pct(c[id] / total) : "0%",
      tip: [{ k: "Share of voice", v: total ? pct(c[id] / total) : "0%" }, { k: "Mentions", v: nf.format(c[id]) }],
    })).sort((a, b) => b.value - a.value);
    hbars($("sovChart"), rows, { highlight: brandId() });
  }

  // ---------------------------------------------------------------------------------
  // Sentiment: 100% stacked diverging bars (negative | neutral | positive)
  // ---------------------------------------------------------------------------------
  function renderSentiment() {
    const el = $("sentChart");
    const ms = slice(0);
    const rows = companies().map((e) => ({ e, t: toneBy(ms, e.id) })).filter((r) => r.t.n > 0).sort((a, b) => b.t.net - a.t.net);
    if (!rows.length) return empty(el);
    const W = Math.max(280, el.clientWidth), rowH = 30, barH = 18, pad = 4;
    const lw = Math.min(150, W * 0.38), vw = 48;
    const H = rows.length * rowH + pad * 2;
    const bw = W - lw - vw - 8;
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
      "aria-label": rows.map((r) => `${r.e.name}: ${r.t.positive} positive, ${r.t.neutral} neutral, ${r.t.negative} negative, net ${signed(r.t.net)}`).join("; ") });
    const segs = [["negative", "var(--neg)", "Negative"], ["neutral", "var(--neu)", "Neutral"], ["positive", "var(--pos)", "Positive"]];
    rows.forEach((r, i) => {
      const y = pad + i * rowH + (rowH - barH) / 2;
      const strong = r.e.id === brandId();
      svg.append(s("text", { x: lw - 8, y: y + barH / 2 + 4, "text-anchor": "end", class: strong ? "label-strong" : null, text: truncate(r.e.name, lw) }));
      let x0 = lw + 1;
      const present = segs.filter(([k]) => r.t[k] > 0);
      const gaps = (present.length - 1) * 2;
      present.forEach(([k, color, name], j) => {
        const w = (r.t[k] / r.t.n) * (bw - gaps);
        const last = j === present.length - 1;
        const g = s("g", { class: "mark", tabindex: 0, "aria-label": `${r.e.name} ${name}: ${r.t[k]}` });
        g.append(last ? s("path", { d: hbarPath(x0, y, w, barH), fill: color }) : s("rect", { x: x0, y, width: Math.max(w, 0), height: barH, fill: color }));
        g.append(s("rect", { class: "hit", x: x0, y: y - 6, width: Math.max(w + 2, 4), height: barH + 12 }));
        const tipRows = [
          { k: "Positive", v: `${r.t.positive} (${pct(r.t.positive / r.t.n)})`, color: "var(--pos)" },
          { k: "Neutral", v: `${r.t.neutral} (${pct(r.t.neutral / r.t.n)})`, color: "var(--axis-muted)" },
          { k: "Negative", v: `${r.t.negative} (${pct(r.t.negative / r.t.n)})`, color: "var(--neg)" },
          { k: "Net score", v: signed(r.t.net) },
        ];
        const enter = (ev) => { svg.classList.add("hover-dim"); g.classList.add("on"); showTip(ev.clientX != null ? ev : g.getBoundingClientRect(), r.e.name, tipRows); };
        const leave = () => { svg.classList.remove("hover-dim"); g.classList.remove("on"); hideTip(); };
        g.addEventListener("pointermove", enter); g.addEventListener("pointerleave", leave);
        g.addEventListener("focus", enter); g.addEventListener("blur", leave);
        svg.append(g);
        x0 += w + 2;
      });
      svg.append(s("text", { x: lw + bw + 10, y: y + barH / 2 + 4, class: strong ? "label-strong" : null, style: "font-variant-numeric:tabular-nums", text: signed(r.t.net) }));
    });
    const legend = h("div", { class: "legend" },
      segs.slice().reverse().map(([, color, name]) => h("span", { class: "key" }, h("span", { class: "sw", style: `background:${color}` }), name)));
    el.replaceChildren(svg, legend);
  }

  // ---------------------------------------------------------------------------------
  // Mentions over time: one 2px line per company, colour fixed per entity
  // ---------------------------------------------------------------------------------
  function trendIds(ms) {
    if (state.trend) return [...state.trend];
    const comps = ofType("competitor").map((e) => e.id);
    const c = countBy(ms, comps);
    return [brandId(), ...comps.sort((a, b) => c[b] - c[a]).slice(0, 3)];
  }
  function renderTrend() {
    const el = $("trendChart");
    const ms = slice(0);
    const shown = trendIds(ms);
    // chips
    const chips = $("trendChips");
    chips.replaceChildren(...companies().map((e) => {
      const on = shown.includes(e.id);
      return h("button", { class: "chip", type: "button", "aria-pressed": String(on), onclick: () => {
        const set = new Set(shown);
        if (on && set.size > 1) set.delete(e.id); else set.add(e.id);
        state.trend = set; renderTrend();
      } }, h("span", { class: "ln", style: `background:${colorOf(e.id)}` }), e.name);
    }));
    const B = buckets();
    $("trendSub").textContent = `Mentions per ${B.step === DAY ? "day" : B.step === 3600000 ? "hour" : "week"} · pick companies to compare`;
    const data = shown.map((id) => ({ id, v: series(ms, id, B) }));
    const maxV = Math.max(0, ...data.flatMap((d) => d.v));
    if (!maxV) return empty(el);
    const W = Math.max(300, el.clientWidth), H = 280;
    const m = { l: 36, r: W < 520 ? 12 : 120, t: 12, b: 28 };
    const max = niceMax(maxV);
    const x = (i) => m.l + (B.n === 1 ? (W - m.l - m.r) / 2 : (i / (B.n - 1)) * (W - m.l - m.r));
    const y = (v) => H - m.b - (v / max) * (H - m.t - m.b);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
      "aria-label": `Mentions over time for ${shown.map((i) => ENT[i].name).join(", ")}` });
    const grid = s("g", { class: "grid" }), axis = s("g", { class: "axis" });
    for (const tv of ticks(max)) {
      if (tv > 0) grid.append(s("line", { x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv) }));
      axis.append(s("text", { x: m.l - 8, y: y(tv) + 4, "text-anchor": "end", text: nf.format(tv) }));
    }
    const nLabels = Math.min(B.n, Math.max(2, Math.floor((W - m.l - m.r) / 90)));
    for (let k = 0; k < nLabels; k++) {
      const i = Math.round((k / (nLabels - 1 || 1)) * (B.n - 1));
      axis.append(s("text", { x: x(i), y: H - 8, "text-anchor": k === 0 ? "start" : k === nLabels - 1 ? "end" : "middle", text: B.label(i).replace("Week of ", "") }));
    }
    svg.append(grid, s("line", { class: "baseline", x1: m.l, x2: W - m.r, y1: y(0), y2: y(0) }), axis);
    // Draw brand last so it sits on top.
    const order = [...data].sort((a, b) => (a.id === brandId()) - (b.id === brandId()));
    for (const d of order) {
      const path = d.v.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
      svg.append(s("path", { d: path, fill: "none", stroke: colorOf(d.id), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    for (const d of order) svg.append(s("circle", { cx: x(B.n - 1), cy: y(d.v[B.n - 1]), r: 4, fill: colorOf(d.id), stroke: "var(--surface-1)", "stroke-width": 2 }));
    // Direct end labels only when they don't collide; otherwise the legend carries identity.
    if (m.r > 20) {
      const ends = data.map((d) => ({ id: d.id, y: y(d.v[B.n - 1]) })).sort((a, b) => a.y - b.y);
      const collide = ends.some((e, i) => i && e.y - ends[i - 1].y < 14);
      if (!collide) for (const e of ends) svg.append(s("text", { x: x(B.n - 1) + 10, y: e.y + 4, class: e.id === brandId() ? "label-strong" : null, text: truncate(ENT[e.id].name, m.r - 14) }));
    }
    // Crosshair + tooltip
    const cross = s("line", { class: "crosshair", y1: m.t, y2: H - m.b, visibility: "hidden" });
    const overlay = s("rect", { class: "hit", x: m.l - 6, y: m.t, width: W - m.l - m.r + 12, height: H - m.t - m.b, tabindex: 0, "aria-label": "Trend chart: use arrow keys to read values" });
    svg.append(cross, overlay);
    let idx = B.n - 1;
    const show = (i, ev) => {
      idx = Math.max(0, Math.min(B.n - 1, i));
      cross.setAttribute("x1", x(idx)); cross.setAttribute("x2", x(idx)); cross.setAttribute("visibility", "visible");
      const rows = data.map((d) => ({ k: ENT[d.id].name, v: nf.format(d.v[idx]), color: colorOf(d.id), n: d.v[idx] })).sort((a, b) => b.n - a.n);
      const r = svg.getBoundingClientRect();
      showTip(ev && ev.clientX != null ? ev : { left: r.left + (x(idx) / W) * r.width, top: r.top + m.t, width: 0 }, B.label(idx), rows);
    };
    overlay.addEventListener("pointermove", (ev) => {
      const r = svg.getBoundingClientRect();
      const px = ((ev.clientX - r.left) / r.width) * W;
      show(Math.round(((px - m.l) / (W - m.l - m.r)) * (B.n - 1)), ev);
    });
    overlay.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
    overlay.addEventListener("focus", () => show(idx));
    overlay.addEventListener("blur", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
    overlay.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowLeft") { show(idx - 1); ev.preventDefault(); }
      if (ev.key === "ArrowRight") { show(idx + 1); ev.preventDefault(); }
    });
    const legend = h("div", { class: "legend" }, data.map((d) => h("span", { class: "key" }, h("span", { class: "ln", style: `background:${colorOf(d.id)}` }), ENT[d.id].name)));
    // Table view: every plotted value is reachable without hovering.
    const table = h("details", { class: "older small" }, h("summary", { text: "Show as table" }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", { text: "Period" }), data.map((d) => h("th", { class: "num", text: ENT[d.id].name })))),
        h("tbody", {}, Array.from({ length: B.n }, (_, i) => B.n - 1 - i).map((i) =>
          h("tr", {}, h("td", { text: B.label(i) }), data.map((d) => h("td", { class: "num", text: nf.format(d.v[i]) }))))))));
    el.replaceChildren(svg, legend, table);
  }

  // ---------------------------------------------------------------------------------
  // Geography & topics (BioMar only, single series)
  // ---------------------------------------------------------------------------------
  function brandSlice() { return slice(0).filter((m) => m.e.includes(brandId())); }
  function renderGeo() {
    const c = {};
    for (const m of brandSlice()) c[m.c || ""] = (c[m.c || ""] || 0) + 1;
    let rows = Object.entries(c).map(([k, v]) => ({ label: countryName(k), value: v, color: "var(--accent)", valueText: nf.format(v) }));
    rows.sort((a, b) => b.value - a.value);
    if (rows.length > 10) {
      const rest = rows.slice(9).reduce((a, r) => a + r.value, 0);
      rows = [...rows.slice(0, 9), { label: "Other countries", value: rest, color: "var(--de-emph)", valueText: nf.format(rest) }];
    }
    hbars($("geoChart"), rows);
  }
  function renderTopics() {
    const c = {};
    for (const m of brandSlice()) for (const t of m.tp || []) c[t] = (c[t] || 0) + 1;
    const rows = Object.entries(c).map(([k, v]) => ({ label: k, value: v, color: "var(--accent)", valueText: nf.format(v) })).sort((a, b) => b.value - a.value);
    hbars($("topicChart"), rows, { labelW: 200 });
  }

  // ---------------------------------------------------------------------------------
  // Executives table
  // ---------------------------------------------------------------------------------
  function renderExecutives() {
    const ms = slice(0);
    const execs = ofType("executive").map((e) => {
      const mine = ms.filter((m) => m.e.includes(e.id));
      return { e, t: toneBy(ms, e.id), latest: mine[0] };
    }).sort((a, b) => b.t.n - a.t.n);
    const rows = execs.map(({ e, t, latest }) => h("tr", {},
      h("td", {}, h("div", { style: "font-weight:550", text: e.name }), h("div", { class: "muted small", text: e.role || "" })),
      h("td", { class: "num", text: nf.format(t.n) }),
      h("td", { class: "num", text: t.n ? signed(t.net) : "–" }),
      h("td", {}, latest ? h("a", { href: latest.u, target: "_blank", rel: "noopener noreferrer", class: "small", text: latest.t }) : h("span", { class: "muted small", text: "No coverage in period" }))));
    $("execTable").replaceChildren(h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { text: "Person" }), h("th", { class: "num", text: "Mentions" }), h("th", { class: "num", text: "Net" }), h("th", { text: "Latest" }))),
      h("tbody", {}, rows))));
  }

  // ---------------------------------------------------------------------------------
  // Attention list & feed
  // ---------------------------------------------------------------------------------
  const ownIds = () => [brandId(), ...ofType("executive").map((e) => e.id)];
  function attentionItems(ms) {
    const own = ownIds();
    return ms.filter((m) => m.e.some((e) => own.includes(e)) && (m.im >= 3 || own.some((e) => m.e.includes(e) && (m.se[e] ?? 0) <= -0.25)));
  }
  function toneBadge(m, id) {
    const sc = m.se[id] ?? 0;
    const t = toneOf(sc);
    return h("span", { class: `tone ${t}`, title: `Sentiment toward ${ENT[id] ? ENT[id].name : id}: ${sc}` }, h("span", { class: "dot" }), t[0].toUpperCase() + t.slice(1));
  }
  function itemRow(m) {
    const focus = m.e.includes(brandId()) ? brandId() : m.e[0];
    return h("div", { class: "item" },
      h("a", { class: "title", href: m.u, target: "_blank", rel: "noopener noreferrer", text: m.t }),
      m.o ? h("div", { class: "orig", text: m.o }) : null,
      m.sm ? h("div", { class: "sum", text: m.sm }) : null,
      h("div", { class: "meta" },
        toneBadge(m, focus),
        h("span", { text: m.s }),
        h("span", { text: countryName(m.c) }),
        h("span", { text: relTime(m.p) }),
        m.mb === "search" ? h("span", { title: "The search engine matched the company in the article text; the headline doesn't name it.", text: "Full-text match" }) : null,
        ...m.e.map((e) => h("span", { class: "pill", text: ENT[e] ? ENT[e].name : e })),
        m.im >= 3 ? h("span", { class: "pill", style: "border-color:var(--critical)" }, h("span", { class: "attn-icon", text: "! " }), "Attention") : null));
  }
  function renderAttention() {
    const items = attentionItems(slice(0)).sort((a, b) => b.im - a.im || b._t - a._t).slice(0, 8);
    $("attention").replaceChildren(...(items.length ? items.map(itemRow) : [h("div", { class: "empty", text: "Nothing negative or urgent in this period." })]));
  }
  function feedItems() {
    const f = state.feed;
    return slice(0).filter((m) =>
      (!f.entity || m.e.includes(f.entity)) &&
      (!f.tone || (f.entity ? toneOf(m.se[f.entity] ?? 0) === f.tone : m.e.some((e) => toneOf(m.se[e] ?? 0) === f.tone))) &&
      (!f.q || `${m.t} ${m.o || ""} ${m.s} ${m.sm || ""}`.toLowerCase().includes(f.q)));
  }
  function renderFeed() {
    const items = feedItems();
    const pages = Math.max(1, Math.ceil(items.length / PAGE));
    state.feed.page = Math.min(state.feed.page, pages - 1);
    const pageItems = items.slice(state.feed.page * PAGE, (state.feed.page + 1) * PAGE);
    $("feed").replaceChildren(h("div", { class: "list" }, pageItems.length ? pageItems.map(itemRow) : h("div", { class: "empty", text: "No articles match these filters." })));
    $("pager").replaceChildren(
      h("span", { text: `${nf.format(items.length)} articles` }),
      h("button", { class: "ghost", type: "button", disabled: state.feed.page === 0 ? true : null, onclick: () => { state.feed.page--; renderFeed(); }, text: "Previous" }),
      h("span", { text: `Page ${state.feed.page + 1} of ${pages}` }),
      h("button", { class: "ghost", type: "button", disabled: state.feed.page >= pages - 1 ? true : null, onclick: () => { state.feed.page++; renderFeed(); }, text: "Next" }));
  }
  function downloadCsv() {
    const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const head = ["published", "title_en", "original_title", "publisher", "country", "language", "entities", "sentiment", "topics", "importance", "summary", "url"];
    const lines = feedItems().map((m) => [m.p, m.t, m.o, m.s, m.c, m.l, m.e.map((e) => ENT[e]?.name || e).join("; "),
      Object.entries(m.se).map(([e, v]) => `${ENT[e]?.name || e}: ${v}`).join("; "), (m.tp || []).join("; "), m.im, m.sm, m.u].map(esc).join(","));
    const blob = new Blob([[head.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8" });
    const a = h("a", { href: URL.createObjectURL(blob), download: `biomar-coverage-${new Date().toISOString().slice(0, 10)}.csv` });
    document.body.append(a); a.click(); a.remove();
  }

  // ---------------------------------------------------------------------------------
  // Theme toggle (wins over OS setting both ways)
  // ---------------------------------------------------------------------------------
  const savedTheme = store.get("mm.theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  $("themeToggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("mm.theme", next);
    if (DATA) renderCharts();
  });

  load();
})();
