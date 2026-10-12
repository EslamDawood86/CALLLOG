"use strict";
/* OPS - building blocks shared by the Operations Hub pages (Home, E-mail, SLA, ITSM, My work, System).
   Load it after common.js (window.CL: h(), api(), toast(), modal(), bidi(), normAr() ...) and before the page script.
   Every node is built with CL.h / createElementNS; text from snapshots and e-mails only ever goes in as text nodes,
   and Arabic text is wrapped in dir="auto". No inline styles, no innerHTML, no libraries.

   ----------------------------------------------------------------------------------------------------------
   API  (all on window.OPS)

   Time and numbers (snapshot times are naive local times: they are read as plain numbers, never through the browser's zone)
     parseNaive(iso) -> ms             fmtClock(iso, withSeconds) -> "09:15"       fmtDate(iso) -> "11 Oct"
     fmtWhen(iso, nowIso) -> "09:15" for today, "10 Oct 16:30" otherwise          fmtDuration(minutes) -> "1 h 5 min"
     fmtAgo(iso, nowIso) -> "3 min ago"      fmtPct(n) -> "68.2%"      fmtBytes(n)      fmtUptime(seconds)      debounce(fn, ms)

   Small pieces
     icon(name, cls) -> <svg>   names: alert info check clock mail shield server phone refresh copy arrow external x play lock
                                       download plus trash user activity database dash file eye pause
     chip(text, tone, iconName) -> <span class="op-chip">      tone: danger | warn | ok | info | na  (icon + label, never colour alone)
     statusChip(status) -> chip for "Replied", "Pending Reply", "No Action Needed", "Met", "Breached", "Within SLA",
                           and the SLA codes within | reminder | breached | no_date
     kpi({label, value, sub, tone, href, onclick, icon, key, hero}) -> tile; setKpi(tile, {value, sub, tone})
     emptyState({title, text, icon, action}) -> <div class="op-empty">      card(title, {hint, actions, cls}, ...body) -> <section class="card">
     tip(target, content | () => content)   hover/focus tooltip for any element
     copyText(text) -> Promise<boolean>     works over plain http too (no secure context needed)

   Charts (SVG, 2 px gaps, 4 px rounded data ends, tones from the status palette)
     donut([{label, value, cls}], {size, thickness, centerValue, centerLabel, label}) -> <svg>
     hbars([{label, value, sub, cls, href}], {max, format}) -> <div>        one bar per row, label in HTML (Arabic-safe)
     stackbar([{label, value, cls}], {format}) -> <div>                    one 100 % bar, e.g. within / reminder / breached
     sparkline(values, {width, height, label, tone}) -> <svg>
       cls: "op-s0".."op-s4" (categories) or "op-t-danger" | "op-t-warn" | "op-t-ok" | "op-t-info" | "op-t-na" (status)

   Data
     loader({url, interval, query, since, onData, onUnchanged, onMeta, onError}) -> {start, stop, refresh, answer, busy}
         GET url every `interval` ms (default 15000, random +0..20 %, doubled after each failure up to x8, paused while the tab is hidden,
         immediate refresh when it becomes visible). It sends ?since=<generated_at>&rev=<meta.rev> so an unchanged scan costs almost
         nothing: onData(answer) runs only for a new answer, onUnchanged(answer) for {unchanged:true}, onMeta(meta, answer) for both.
         refresh() fetches at once WITHOUT since (use it after the user changed something); query() may return extra parameters.
     freshnessInfo(meta) -> {tone, icon, text}       meta = the "meta" block of GET /api/ops/<module>
     freshnessBanner() -> {el, update(meta)}         "Last updated 09:15 · collector PC-123 · OK", or failed / offline / stale / outside hours
     live() -> {el, ok(text), bad(text)}             the green "Live · updated ..." dot of the Call Log pages
     sharedStrip(host, {poll, interval, links}) -> {update(data), refresh(), stop()}     the "Team status" strip (GET /api/ops/shared)
         poll:false = the page feeds it with update(data) (Home does, from /api/ops/overview); links:true = tiles link to the module pages

   Tables
     table({columns, rows, pageSize, pageSizes, sort, rowKey, onRowClick, rowClass, empty, search, copy, toolbar, caption, filter})
         -> {el, setRows(rows), setFilter(fn|null), setQuery(text), sortBy(key, desc), goto(n), refresh(), copy(), rows(), shown()}
         columns: {key, label, num, nowrap, cls, sort: true | row => value, render: row => Node|string, text: row => string}
         `text` is what search and copy see (default: row[key]); copy puts the filtered rows on the clipboard as tab-separated text.

   Dialogs: OPS.modal / OPS.confirm are CL.modal / CL.confirmDialog.
   Deep links produced by Home and understood by the module pages when they care:  /email?q=<text>&team=<team>   /sla?q=<text>
   ---------------------------------------------------------------------------------------------------------- */
(function () {
  const { h, svgEl, api, toast, normAr, fmtNum } = CL;

  // ----------------------------------------------------------------------------------------- time and numbers
  const p2 = (n) => String(n).padStart(2, "0");
  const ISO_RE = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?/;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function parseNaive(iso) {
    const m = ISO_RE.exec(String(iso || ""));
    return m ? Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : NaN;
  }
  const localNowIso = () => {
    const d = new Date();
    return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}T${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
  };
  function fmtClock(iso, withSeconds) {
    const m = ISO_RE.exec(String(iso || ""));
    return m ? `${m[4]}:${m[5]}${withSeconds && m[6] ? ":" + m[6] : ""}` : "";
  }
  function fmtDate(iso) {
    const m = ISO_RE.exec(String(iso || ""));
    return m ? `${+m[3]} ${MONTHS[m[2] - 1]}` : "";
  }
  function fmtWhen(iso, nowIso) {
    if (!ISO_RE.test(String(iso || ""))) return "";
    const today = String(nowIso || localNowIso()).slice(0, 10);
    return String(iso).slice(0, 10) === today ? fmtClock(iso) : `${fmtDate(iso)} ${fmtClock(iso)}`;
  }
  function fmtDuration(minutes) {
    if (minutes == null || Number.isNaN(Number(minutes))) return "—";
    const n = Math.round(Number(minutes));
    if (n < 1) return "<1 min";
    if (n < 60) return `${n} min`;
    if (n < 1440) return n % 60 ? `${Math.floor(n / 60)} h ${n % 60} min` : `${n / 60} h`;
    const d = Math.floor(n / 1440), hrs = Math.floor((n % 1440) / 60);
    return hrs ? `${d} d ${hrs} h` : `${d} d`;
  }
  function fmtAgo(iso, nowIso) {
    const t = parseNaive(iso), now = parseNaive(nowIso || localNowIso());
    if (Number.isNaN(t) || Number.isNaN(now)) return "";
    const mins = Math.max(0, Math.floor((now - t) / 60000));
    return mins < 1 ? "just now" : `${fmtDuration(mins)} ago`;
  }
  const fmtPct = (n) => (n == null || Number.isNaN(Number(n)) ? "—" : `${Math.round(Number(n) * 10) / 10}%`);
  function fmtBytes(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${i ? v.toFixed(v < 10 ? 1 : 0) : v} ${units[i]}`;
  }
  function fmtUptime(seconds) {
    return fmtDuration(Math.floor(Number(seconds || 0) / 60));
  }
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  // ------------------------------------------------------------------------------------------------- icons
  const PATHS = {
    alert: "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
    info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
    check: "M20 6 9 17l-5-5",
    clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2",
    mail: "M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM22 6l-10 7L2 6",
    shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
    server: "M4 2h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM4 14h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2zM6 6h.01M6 18h.01",
    phone: "M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z",
    refresh: "M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15",
    copy: "M20 9h-9a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-9a2 2 0 0 0-2-2zM5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1",
    arrow: "M5 12h14M12 5l7 7-7 7",
    external: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3",
    x: "M18 6 6 18M6 6l12 12",
    play: "M5 3l14 9-14 9V3z",
    lock: "M19 11H5a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7a2 2 0 0 0-2-2zM7 11V7a5 5 0 0 1 10 0v4",
    download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
    plus: "M12 5v14M5 12h14",
    trash: "M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6",
    user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    activity: "M22 12h-4l-3 9L9 3l-3 9H2",
    database: "M12 8c5 0 9-1.3 9-3s-4-3-9-3-9 1.3-9 3 4 3 9 3zM21 12c0 1.7-4 3-9 3s-9-1.3-9-3M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5",
    dash: "M6 12h12",
    file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8",
    eye: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    pause: "M6 4h4v16H6zM14 4h4v16h-4z",
  };
  function icon(name, cls) {
    const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "op-ic" + (cls ? " " + cls : "") });
    svg.append(svgEl("path", { d: PATHS[name] || PATHS.info }));
    return svg;
  }

  // ------------------------------------------------------------------------------- chips, tiles, empty states
  const LEVEL_ICON = { danger: "alert", warn: "clock", info: "info", ok: "check", na: "dash" };
  const LEVEL_RANK = { danger: 0, warn: 1, info: 2, ok: 3, na: 4 };
  function chip(text, tone, iconName) {
    return h("span", { class: `op-chip op-t-${tone || "na"}` }, icon(iconName || LEVEL_ICON[tone] || "dash"), h("span", { class: "bidi", dir: "auto" }, text));
  }
  const STATUS_CHIPS = {
    "Replied": ["ok", "Replied"], "Pending Reply": ["warn", "Pending"], "No Action Needed": ["na", "No action needed"],
    "Met": ["ok", "SLA met"], "Within SLA": ["ok", "Within SLA"], "Breached": ["danger", "SLA breached"], "N/A": ["na", "No SLA"],
    within: ["ok", "Within SLA"], reminder: ["warn", "Reminder"], breached: ["danger", "SLA breached"], no_date: ["na", "No sent date"],
  };
  function statusChip(status) {
    const [tone, label] = STATUS_CHIPS[status] || ["na", String(status || "—")];
    return chip(label, tone);
  }
  function kpi({ label, value, sub, tone, href, onclick, icon: iconName, key, hero }) {
    const tag = href ? "a" : onclick ? "button" : "div";
    return h(tag, { class: "op-kpi" + (hero ? " op-hero" : "") + (tone ? ` op-t-${tone}` : ""), href, type: tag === "button" ? "button" : null,
      onclick, "data-kpi": key },
    h("span", { class: "op-kpi-label" }, iconName ? icon(iconName) : null, label),
    h("strong", { class: "op-kpi-value", "data-kpi-value": "" }, value == null ? "—" : String(value)),
    h("span", { class: "op-kpi-sub" }, sub || ""));
  }
  function setKpi(el, { value, sub, tone }) {
    if (value !== undefined) el.querySelector("[data-kpi-value]").textContent = value == null ? "—" : String(value);
    if (sub !== undefined) el.querySelector(".op-kpi-sub").textContent = sub || "";
    if (tone !== undefined) {
      el.className = el.className.replace(/\bop-t-\w+/g, "").trim() + (tone ? ` op-t-${tone}` : "");
    }
  }
  function emptyState({ title, text, icon: iconName, action }) {
    return h("div", { class: "op-empty" }, icon(iconName || "info", "op-empty-ic"), title ? h("strong", {}, title) : null,
      text ? h("p", { class: "muted" }, text) : null, action || null);
  }
  function card(title, opts, ...body) {
    const o = opts || {};
    return h("section", { class: "card" + (o.cls ? " " + o.cls : "") },
      title ? h("div", { class: "card-head" }, h("h2", {}, title), o.hint ? h("span", { class: "hint" }, o.hint) : null,
        o.actions ? [h("span", { class: "grow" }), o.actions] : null) : null, body);
  }

  // ------------------------------------------------------------------------------------------------ tooltip
  let tipEl = null;
  function tip(target, content) {
    const host = () => tipEl || (tipEl = document.body.appendChild(h("div", { class: "op-tip", role: "tooltip", hidden: true })));
    const place = (x, y) => {
      const t = host(), pad = 8;
      const left = Math.min(Math.max(pad, x), window.innerWidth - t.offsetWidth - pad);
      const top = y + t.offsetHeight + 16 > window.innerHeight ? y - t.offsetHeight - 14 : y + 16;
      t.style.left = `${left}px`;
      t.style.top = `${Math.max(pad, top)}px`;
    };
    const show = (x, y) => {
      const t = host(), c = typeof content === "function" ? content() : content;
      if (c == null || c === "") return;
      t.replaceChildren(...[].concat(c).map((n) => (n && n.nodeType ? n : document.createTextNode(String(n)))));
      t.hidden = false;
      place(x, y);
    };
    const hide = () => { if (tipEl) tipEl.hidden = true; };
    target.addEventListener("pointerenter", (e) => show(e.clientX, e.clientY));
    target.addEventListener("pointermove", (e) => tipEl && !tipEl.hidden && place(e.clientX, e.clientY));
    target.addEventListener("pointerleave", hide);
    target.addEventListener("focus", () => { const r = target.getBoundingClientRect(); show(r.left + r.width / 2, r.top); });
    target.addEventListener("blur", hide);
    return target;
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch (e) { /* fall through to the old way */ }
    const box = h("textarea", { class: "sr-only", "aria-hidden": "true", tabindex: "-1" });
    box.value = text;
    document.body.append(box);
    box.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    box.remove();
    return ok;
  }

  // ------------------------------------------------------------------------------------------------ charts
  const SERIES = ["op-s0", "op-s1", "op-s2", "op-s3", "op-s4"];
  const sizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver((entries) => {
    for (const e of entries) {
      const w = Math.floor(e.contentRect.width);
      if (e.target._draw && w > 0 && w !== e.target._w) { e.target._w = w; e.target._draw(w); }
    }
  }) : null;
  /** Draw `svg` with `draw(width)` now and whenever its width changes (bars need real pixels for 4 px corners). */
  function responsive(svg, draw) {
    svg._draw = draw;
    if (sizeObserver) sizeObserver.observe(svg);
    else draw(240);
  }
  /** Bar from x=0 to x=w, square at the baseline (left), `r` px rounded at the data end (right). */
  const barPath = (w, hgt, r) => {
    r = Math.max(0, Math.min(r, hgt / 2, w));
    return `M0,0H${w - r}${r ? `Q${w},0 ${w},${r}V${hgt - r}Q${w},${hgt} ${w - r},${hgt}` : `V${hgt}`}H0Z`;
  };

  function donut(slices, opts) {
    const o = opts || {}, size = o.size || 132, thick = o.thickness || 16, r = (size - thick) / 2, c = 2 * Math.PI * r, mid = size / 2;
    const live = slices.filter((s) => s.value > 0), total = live.reduce((a, s) => a + s.value, 0);
    const svg = svgEl("svg", { class: "op-donut", viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: "img",
      "aria-label": o.label || slices.map((s) => `${s.label}: ${s.value}`).join(", ") });
    svg.append(svgEl("circle", { class: "op-donut-track", cx: mid, cy: mid, r, fill: "none", "stroke-width": thick }));
    let offset = 0;
    live.forEach((s, i) => {
      const len = (s.value / total) * c, dash = Math.max(0.5, len - (live.length > 1 ? 2 : 0));   // 2 px of surface between slices
      const seg = svgEl("circle", { class: `op-donut-seg ${s.cls || SERIES[i % SERIES.length]}`, cx: mid, cy: mid, r, fill: "none",
        "stroke-width": thick, "stroke-dasharray": `${dash} ${c - dash}`, "stroke-dashoffset": -offset, transform: `rotate(-90 ${mid} ${mid})`,
        tabindex: "0", role: "img", "aria-label": `${s.label}: ${s.value}` });
      tip(seg, () => `${s.label}: ${s.value} (${Math.round((s.value / total) * 100)}%)`);
      svg.append(seg);
      offset += len;
    });
    if (o.centerValue != null) {
      const v = svgEl("text", { class: "op-donut-value", x: mid, y: o.centerLabel ? mid - 2 : mid, "text-anchor": "middle", "dominant-baseline": "central" });
      v.textContent = String(o.centerValue);
      svg.append(v);
      if (o.centerLabel) {
        const l = svgEl("text", { class: "op-donut-label", x: mid, y: mid + 16, "text-anchor": "middle", "dominant-baseline": "central" });
        l.textContent = o.centerLabel;
        svg.append(l);
      }
    }
    return svg;
  }

  function hbars(items, opts) {
    const o = opts || {}, fmt = o.format || ((v) => fmtNum(v)), max = o.max || Math.max(1, ...items.map((i) => i.value));
    return h("div", { class: "op-hbars" }, items.map((item, idx) => {
      const svg = svgEl("svg", { class: "op-hbar-svg", height: 10, "aria-hidden": "true" });
      responsive(svg, (w) => {
        svg.setAttribute("viewBox", `0 0 ${w} 10`);
        const bar = item.value > 0 ? Math.max(3, (w * item.value) / max) : 0;
        svg.replaceChildren(svgEl("path", { class: "op-hbar-track", d: barPath(w, 10, 4) }),
          ...(bar ? [svgEl("path", { class: `op-hbar-fill ${item.cls || SERIES[idx % SERIES.length]}`, d: barPath(bar, 10, 4) })] : []));
      });
      const label = h(item.href ? "a" : "span", { class: "op-hbar-label", href: item.href },
        h("span", { class: "bidi", dir: "auto" }, item.label), item.sub ? h("span", { class: "hint" }, item.sub) : null);
      return h("div", { class: "op-hbar" }, label, svg, h("strong", { class: "op-hbar-val" }, fmt(item.value)));
    }));
  }

  function stackbar(segments, opts) {
    const o = opts || {}, fmt = o.format || ((v) => fmtNum(v)), live = segments.filter((s) => s.value > 0);
    const total = live.reduce((a, s) => a + s.value, 0);
    const svg = svgEl("svg", { class: "op-stackbar-svg", height: 12, role: "img",
      "aria-label": segments.map((s) => `${s.label}: ${s.value}`).join(", ") });
    responsive(svg, (w) => {
      svg.setAttribute("viewBox", `0 0 ${w} 12`);
      svg.replaceChildren(svgEl("path", { class: "op-hbar-track", d: barPath(w, 12, 4) }));
      const usable = w - 2 * Math.max(0, live.length - 1);
      let x = 0;
      live.forEach((s, i) => {
        const len = Math.max(3, (usable * s.value) / total), last = i === live.length - 1;
        const seg = svgEl("path", { class: `op-hbar-fill ${s.cls || SERIES[i % SERIES.length]}`, d: barPath(len, 12, last ? 4 : 0),
          transform: `translate(${x} 0)` });
        svg.append(seg);
        x += len + 2;
      });
    });
    const legend = h("div", { class: "op-legend" }, segments.map((s, i) => h("span", { class: "op-legend-item" },
      h("i", { class: `op-swatch ${s.cls || SERIES[i % SERIES.length]}` }), `${fmt(s.value)} `, h("span", { class: "muted" }, s.label))));
    return h("div", { class: "op-stackbar" }, total ? svg : h("p", { class: "hint" }, "Nothing to show yet."), legend);
  }

  function sparkline(values, opts) {
    const o = opts || {}, w = o.width || 140, hgt = o.height || 36, pad = 4, n = values.length;
    const svg = svgEl("svg", { class: `op-spark ${o.tone ? "op-t-" + o.tone : ""}`, viewBox: `0 0 ${w} ${hgt}`, width: w, height: hgt, role: "img",
      "aria-label": o.label || "trend" });
    if (!n) return svg;
    const lo = Math.min(...values), span = Math.max(...values) - lo;
    const x = (i) => (n === 1 ? w / 2 : pad + (i * (w - 2 * pad)) / (n - 1));
    const y = (v) => (span === 0 ? hgt / 2 : hgt - pad - ((v - lo) / span) * (hgt - 2 * pad));
    const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    svg.append(svgEl("path", { class: "op-spark-area", d: `M${x(0).toFixed(1)},${hgt - pad}L${pts.join("L")}L${x(n - 1).toFixed(1)},${hgt - pad}Z` }),
      svgEl("polyline", { class: "op-spark-line", points: pts.join(" "), fill: "none" }),
      svgEl("circle", { class: "op-spark-dot", cx: x(n - 1).toFixed(1), cy: y(values[n - 1]).toFixed(1), r: 4 }));
    return svg;
  }

  // ------------------------------------------------------------------------------------------------- loader
  function loader(opts) {
    const interval = opts.interval || 15000;
    let timer = null, busy = false, running = false, fails = 0, generated = "", rev = null, answer = null;

    function url() {
      const base = typeof opts.url === "function" ? opts.url() : opts.url;
      return base;
    }
    async function load(force) {
      if (busy) return;
      busy = true;
      clearTimeout(timer);
      try {
        const params = new URLSearchParams(opts.query ? opts.query() : {});
        if (opts.since !== false && generated && !force) {
          params.set("since", generated);
          if (rev != null) params.set("rev", rev);
        }
        const base = url(), qs = params.toString();
        const got = await api("GET", qs ? `${base}${base.includes("?") ? "&" : "?"}${qs}` : base);
        fails = 0;
        if (got.unchanged) {
          if (opts.onUnchanged) opts.onUnchanged(got);
        } else {
          answer = got;
          generated = got.generated_at || "";
          rev = got.meta && got.meta.rev != null ? got.meta.rev : null;
          if (opts.onData) opts.onData(got);
        }
        if (opts.onMeta) opts.onMeta(got.meta || null, got);
      } catch (e) {
        fails++;
        if (opts.onError) opts.onError(e, fails);
      } finally {
        busy = false;
        schedule();
      }
    }
    function schedule() {
      clearTimeout(timer);
      if (!running || document.hidden) return;
      const wait = interval * Math.min(8, 2 ** fails) * (1 + Math.random() * 0.2);
      timer = setTimeout(() => load(false), wait);
    }
    document.addEventListener("visibilitychange", () => { if (running && !document.hidden) load(false); });
    window.addEventListener("online", () => { if (running) load(false); });
    return {
      start() { running = true; load(true); },
      stop() { running = false; clearTimeout(timer); },
      refresh() { return load(true); },
      get answer() { return answer; },
      get busy() { return busy; },
    };
  }

  // -------------------------------------------------------------------------------- freshness of a module
  function freshnessInfo(meta) {
    if (!meta) return { tone: "info", icon: "info", text: "Waiting for data…" };
    const label = { email: "E-mail", sla: "Security requests", itsm: "ITSM" }[meta.module] || "This module";
    if (meta.problem) return { tone: "danger", icon: "alert", text: `The ${label} data file cannot be read: ${meta.problem}` };
    if (!meta.enabled) return { tone: "info", icon: "pause", text: `${label} is switched off in the settings.` };
    if (!meta.generated_at) {
      return { tone: "info", icon: "info", text: meta.online ? "No scan yet - the collector is running its first scan." : "No data yet - the collector has not run." };
    }
    const when = fmtWhen(meta.generated_at, meta.server_time), host = meta.host ? ` · collector ${meta.host}` : "";
    if (meta.last_status === "error") {
      return { tone: "danger", icon: "alert", text: `Last scan failed: ${meta.last_error || "unknown error"} · showing the scan from ${when}${host}` };
    }
    if (!meta.online) {
      return meta.active_now === false
        ? { tone: "info", icon: "clock", text: `Outside working hours · showing the last scan from ${when}${host}` }
        : { tone: "danger", icon: "alert", text: `Collector offline · showing the last scan from ${when}${host}` };
    }
    if (meta.stale) return { tone: "warn", icon: "clock", text: `Data is ${fmtDuration(meta.age_minutes)} old · last updated ${when}${host}` };
    if (meta.running) return { tone: "ok", icon: "refresh", text: `Last updated ${when}${host} · scanning now…` };
    return { tone: "ok", icon: "check", text: `Last updated ${when}${host} · OK` };
  }
  function freshnessBanner() {
    const el = h("div", { class: "op-banner op-t-info", role: "status" });
    function update(meta) {
      const f = freshnessInfo(meta);
      el.className = `op-banner op-t-${f.tone}`;
      el.replaceChildren(icon(f.icon), h("span", { class: "op-banner-text bidi", dir: "auto" }, f.text));
    }
    update(null);
    return { el, update };
  }
  function live() {
    const text = h("span", {}, "Connecting…"), el = h("span", { class: "live", role: "status" }, h("i", { class: "live-dot" }), text);
    return {
      el,
      ok(t) { el.className = "live ok"; text.textContent = t; },
      bad(t) { el.className = "live bad"; text.textContent = t; },
    };
  }

  // ------------------------------------------------------------------------------ the shared "Team status" strip
  function stripGroup(opts, link) {
    const body = [h("span", { class: "op-strip-label" }, icon(opts.icon), opts.label),
      h("strong", { class: "op-strip-value" }, opts.value),
      h("span", { class: "op-strip-sub" }, ...opts.sub)];
    return link ? h("a", { class: "op-strip-group", href: link }, body) : h("div", { class: "op-strip-group" }, body);
  }
  const note = (tone, text) => h("span", { class: `op-note op-t-${tone}` }, icon(LEVEL_ICON[tone]), text);
  const short = (category) => String(category).replace(/ requests?$/i, "");

  function renderStrip(d, links) {
    const link = (path) => (links ? path : null);
    const c = d.calls || {}, e = d.email || {}, s = d.sla || {}, i = d.itsm || {}, col = d.collector || {};
    const groups = [];
    groups.push(stripGroup({ icon: "phone", label: "Calls today", value: fmtNum(c.total || 0),
      sub: [`${c.active_employees || 0} of ${c.employees || 0} employees took calls`] }, link("/calls")));
    groups.push(e.available
      ? stripGroup({ icon: "mail", label: "E-mails pending", value: fmtNum(e.pending || 0),
        sub: [`${fmtPct(e.reply_rate)} replied · `, (e.sla_breached || 0) ? note("danger", `${e.sla_breached} past SLA`) : note("ok", "none past SLA")] }, link("/email"))
      : stripGroup({ icon: "mail", label: "E-mails pending", value: "—", sub: ["No e-mail data yet"] }, link("/email")));
    groups.push(s.available
      ? stripGroup({ icon: "shield", label: "Security requests open", value: fmtNum(s.open || 0),
        sub: [(s.breached || 0) ? note("danger", `${s.breached} breached`) : note("ok", "none breached"), ` · ${s.reminder || 0} at reminder`] }, link("/sla"))
      : stripGroup({ icon: "shield", label: "Security requests open", value: "—", sub: ["No SLA data yet"] }, link("/sla")));
    const totals = (i.totals && Object.entries(i.totals)) || [];
    groups.push(i.available
      ? stripGroup({ icon: "server", label: "ITSM requests", value: fmtNum(totals.reduce((a, [, n]) => a + (n || 0), 0)),
        sub: [totals.map(([k, n]) => `${short(k)} ${n}`).join(" · ") || "No categories"] }, link("/itsm"))
      : stripGroup({ icon: "server", label: "ITSM requests", value: "—", sub: ["No ITSM data yet"] }, link("/itsm")));
    const mods = Object.values(col.modules || {}), up = mods.filter((m) => m.online).length;
    const tone = col.online ? "ok" : up ? "warn" : "danger";
    groups.push(stripGroup({ icon: "activity", label: "Collectors",
      value: col.online ? "Online" : up ? `${up} of ${mods.length} online` : "Offline",
      sub: [note(tone, col.last_update ? `data from ${fmtWhen(col.last_update, d.generated_at)}` : "no data yet")] }, link("/system")));
    return groups;
  }

  function sharedStrip(host, opts) {
    const o = opts || {};
    let timer = null, seen = "", stopped = false, last = null;
    host.classList.add("op-strip");
    host.setAttribute("aria-label", "Team status");
    const title = h("h2", {}, "Team status"), stamp = h("span", { class: "hint" });
    const body = h("div", { class: "op-strip-groups" });
    host.replaceChildren(h("div", { class: "op-strip-head" }, title, stamp), body);
    function update(d) {
      const key = JSON.stringify(d, (k, v) => (k === "generated_at" ? undefined : v));
      last = d;
      stamp.textContent = d.generated_at ? `updated ${fmtClock(d.generated_at, true)}` : "";
      if (key === seen) return;
      seen = key;
      body.replaceChildren(...renderStrip(d, !!o.links));
    }
    async function refresh() {
      try {
        update(await api("GET", "/api/ops/shared"));
        host.classList.remove("op-strip-error");
      } catch (e) {
        host.classList.add("op-strip-error");
        stamp.textContent = e.network ? "offline - retrying" : e.message;
      }
      schedule();
    }
    function schedule() {
      clearTimeout(timer);
      if (o.poll === false || stopped || document.hidden) return;
      timer = setTimeout(refresh, (o.interval || 15000) * (1 + Math.random() * 0.2));
    }
    if (o.poll !== false) {
      document.addEventListener("visibilitychange", () => { if (!stopped && !document.hidden) refresh(); });
      refresh();
    }
    return { update, refresh, stop() { stopped = true; clearTimeout(timer); }, get data() { return last; } };
  }

  // ------------------------------------------------------------------------------------------------- tables
  function table(cfg) {
    const cols = cfg.columns, sizes = cfg.pageSizes || [25, 50, 100];
    const st = { rows: cfg.rows || [], query: "", filter: cfg.filter || null, page: 1, size: cfg.pageSize || sizes[0],
      sortKey: cfg.sort ? cfg.sort.key : null, desc: cfg.sort ? !!cfg.sort.desc : false, view: [] };
    const textOf = (col, row) => (col.text ? col.text(row) : row[col.key] == null ? "" : String(row[col.key]));
    const sortVal = (col, row) => (typeof col.sort === "function" ? col.sort(row) : row[col.key]);

    const search = cfg.search === false ? null : h("input", { type: "search", class: "search op-table-search", "aria-label": "Search the table",
      placeholder: cfg.searchPlaceholder || "Search", dir: "auto" });
    const count = h("span", { class: "hint op-table-count" });
    const copyBtn = cfg.copy === false ? null : h("button", { class: "btn btn-ghost btn-sm", type: "button", title: "Copy the shown rows as text",
      onclick: () => copy() }, icon("copy"), "Copy");
    const bar = h("div", { class: "op-table-bar" }, search, count, h("span", { class: "grow" }), cfg.toolbar || null, copyBtn);
    const head = h("tr", {});
    const body = h("tbody", {});
    const foot = h("div", { class: "pager op-table-foot" });
    const empty = h("div", { class: "op-table-empty" });
    const tbl = h("table", { class: "data op-table" }, cfg.caption ? h("caption", { class: "sr-only" }, cfg.caption) : null, h("thead", {}, head), body);
    const wrap = h("div", { class: "table-wrap" }, tbl);
    const el = h("div", { class: "op-table-box" }, cfg.search === false && cfg.copy === false && !cfg.toolbar ? null : bar, wrap, empty, foot);

    function filtered() {
      const q = normAr(st.query).trim(), words = q ? q.split(/\s+/) : [];
      let rows = st.rows;
      if (st.filter) rows = rows.filter(st.filter);
      if (words.length) {
        rows = rows.filter((r) => {
          const hay = normAr(cols.map((c) => textOf(c, r)).join(" "));
          return words.every((w) => hay.includes(w));
        });
      }
      const col = cols.find((c) => c.key === st.sortKey);
      if (col && col.sort) {
        const dir = st.desc ? -1 : 1;
        rows = rows.map((r, i) => [r, i]).sort((a, b) => {
          const x = sortVal(col, a[0]), y = sortVal(col, b[0]);
          if (x == null && y == null) return a[1] - b[1];
          if (x == null) return 1;
          if (y == null) return -1;
          const c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
          return c * dir || a[1] - b[1];
        }).map((p) => p[0]);
      }
      return rows;
    }

    function drawHead() {
      head.replaceChildren(...cols.map((c) => {
        const sorted = st.sortKey === c.key;
        const th = h("th", { class: (c.sort ? "sortable " : "") + (c.num ? "num " : "") + (c.cls || ""), scope: "col",
          "aria-sort": c.sort ? (sorted ? (st.desc ? "descending" : "ascending") : "none") : null, tabindex: c.sort ? "0" : null },
        c.label, sorted ? (st.desc ? " ▼" : " ▲") : "");
        if (c.sort) {
          th.addEventListener("click", () => sortBy(c.key));
          th.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); sortBy(c.key); } });
        }
        return th;
      }));
    }

    function render() {
      const rows = filtered();
      st.view = rows;
      const pages = Math.max(1, Math.ceil(rows.length / st.size));
      st.page = Math.min(st.page, pages);
      const from = (st.page - 1) * st.size, slice = rows.slice(from, from + st.size);
      drawHead();
      body.replaceChildren(...slice.map((row) => {
        const tr = h("tr", { class: (cfg.rowClass ? cfg.rowClass(row) : "") + (cfg.onRowClick ? " clickable" : ""),
          tabindex: cfg.onRowClick ? "0" : null, "data-key": cfg.rowKey ? cfg.rowKey(row) : null },
        cols.map((c) => h("td", { class: (c.num ? "num " : "") + (c.nowrap ? "nowrap " : "") + (c.cls || "") },
          c.render ? c.render(row) : textOf(c, row))));
        if (cfg.onRowClick) {
          tr.addEventListener("click", (ev) => { if (!ev.target.closest("a,button,input,select,textarea")) cfg.onRowClick(row); });
          tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target === tr) cfg.onRowClick(row); });
        }
        return tr;
      }));
      const none = rows.length === 0;
      wrap.hidden = none;
      empty.hidden = !none;
      if (none) {
        const e = typeof cfg.empty === "function" ? cfg.empty(!!(st.query || st.filter)) : cfg.empty;
        empty.replaceChildren(e && e.nodeType ? e : emptyState(e || { title: "Nothing to show", text: "No rows match.", icon: "info" }));
      }
      count.textContent = rows.length === st.rows.length ? `${fmtNum(rows.length)} row${rows.length === 1 ? "" : "s"}`
        : `${fmtNum(rows.length)} of ${fmtNum(st.rows.length)}`;
      foot.hidden = rows.length <= Math.min(...sizes);
      const sizeSel = h("select", { "aria-label": "Rows per page", onchange: (ev) => { st.size = +ev.target.value; st.page = 1; render(); } },
        sizes.map((n) => h("option", { value: n, selected: n === st.size }, `${n} per page`)));
      foot.replaceChildren(`${from + 1}–${Math.min(from + st.size, rows.length)} of ${fmtNum(rows.length)}`,
        h("button", { class: "btn btn-ghost btn-sm", type: "button", disabled: st.page <= 1, onclick: () => goto(st.page - 1) }, "Previous"),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", disabled: st.page >= pages, onclick: () => goto(st.page + 1) }, "Next"), sizeSel);
    }
    function sortBy(key, desc) {
      st.desc = desc === undefined ? (st.sortKey === key ? !st.desc : false) : !!desc;
      st.sortKey = key;
      st.page = 1;
      render();
    }
    function goto(n) { st.page = Math.max(1, n); render(); }
    async function copy() {
      const lines = [cols.map((c) => c.label).join("\t"), ...st.view.map((r) => cols.map((c) => textOf(c, r).replace(/\s+/g, " ")).join("\t"))];
      const ok = await copyText(lines.join("\n"));
      toast(ok ? `Copied ${st.view.length} row${st.view.length === 1 ? "" : "s"}` : "Copy is blocked by this browser", ok ? undefined : "error");
    }
    if (search) search.addEventListener("input", debounce(() => { st.query = search.value; st.page = 1; render(); }, 150));
    render();
    return {
      el, refresh: render, copy, goto, sortBy,
      setRows(rows) { st.rows = rows || []; render(); },
      setFilter(fn) { st.filter = fn; st.page = 1; render(); },
      setQuery(text) { st.query = text || ""; if (search) search.value = st.query; st.page = 1; render(); },
      rows: () => st.rows, shown: () => st.view,
    };
  }

  window.OPS = { parseNaive, fmtClock, fmtDate, fmtWhen, fmtDuration, fmtAgo, fmtPct, fmtBytes, fmtUptime, debounce, localNowIso,
    icon, chip, statusChip, kpi, setKpi, emptyState, card, tip, copyText, LEVEL_ICON, LEVEL_RANK,
    donut, hbars, stackbar, sparkline, loader, freshnessInfo, freshnessBanner, live, sharedStrip, table,
    modal: CL.modal, confirm: CL.confirmDialog, text: CL.bidi };
})();
