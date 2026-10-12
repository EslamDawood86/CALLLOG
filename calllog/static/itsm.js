"use strict";
/* ITSM page (/itsm): implementation / rollback requests per ITSM role (docs/HUB-DESIGN.md sections 5.2 and 7).

   Reads GET /api/ops/itsm (the last good scoreboard scan + collector health) every 15 s with ?since=. Per role a card
   shows the category counts (0 = green, more = amber, rollback requests = red, "not on this scoreboard" = grey) and,
   behind each count, the request numbers with their summaries (with a copy button). A role the collector could not
   read shows its error message instead. Above the cards: totals, what changed since the previous scan and today's
   history. "Run scan now" asks the collector for a scan and waits until a newer one appears.

   Every scraped text goes in with textContent (CL.h) - never innerHTML - and Arabic text carries dir="auto". */
(function () {
  const { h, $, api, toast, bidi, svgEl, normAr, fmtNum } = CL;
  const SELF = document.currentScript ? document.currentScript.src : "";

  const POLL_MS = 15000;
  const WAIT_POLL_MS = 3000;
  const WAIT_LIMIT_MS = 6 * 60 * 1000;      // an ITSM scan visits every role, so it is the slowest scan
  const TICK_MS = 10000;
  const VIEW_KEY = "ops.itsm.view";
  const AUTO_OPEN_MAX = 8;                  // lists up to this long start open, longer ones start closed
  const CHANGES_MAX = 50;

  const PATHS = {
    refresh: "M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5", alert: "M12 3 2 20h20L12 3zM12 10v4M12 17h.01",
    clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2", check: "M20 6 9 17l-5-5", dash: "M6 12h12",
    x: "M18 6 6 18M6 6l12 12", download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
    wifi: "M2 9a15 15 0 0 1 20 0M5.5 12.5a10 10 0 0 1 13 0M9 16a5 5 0 0 1 6 0M12 20h.01",
    copy: "M9 9h11v11H9zM5 15V5a1 1 0 0 1 1-1h10", caret: "M9 18l6-6-6-6",
  };
  const TONE_ICON = { ok: "check", warn: "alert", danger: "alert", na: "dash" };

  const state = {
    snap: null, meta: null, generatedAt: "", history: null, today: "", failure: null, waiting: null,
    q: "", chartAsTable: false,
    open: {},                 // "role|category" -> true / false when the person opened / closed a list by hand
  };
  const el = {};
  let skew = 0;

  // ================================================================== shared kit (same code as sla.js)
  function glyph(name) {
    const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "sx-ic" });
    svg.append(svgEl("path", { d: PATHS[name] }));
    return svg;
  }
  function naiveMs(iso) {
    const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?/.exec(String(iso || ""));
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : null;
  }
  function noteServerTime(iso) {
    const t = naiveMs(iso);
    if (t != null) skew = t - Date.now();
  }
  const serverNow = () => Date.now() + skew;
  const hhmm = (iso) => String(iso || "").slice(11, 16);
  const pad2 = (n) => String(n).padStart(2, "0");
  function minutesAgo(iso) {
    const t = naiveMs(iso);
    return t == null ? null : Math.max(0, Math.floor((serverNow() - t) / 60000));
  }
  function fmtAge(min) {
    if (min == null) return "—";
    if (min < 60) return `${min} min`;
    const hrs = Math.floor(min / 60);
    if (hrs < 24) return `${hrs} h ${pad2(min % 60)} min`;
    return `${Math.floor(hrs / 24)} d ${hrs % 24} h`;
  }
  function ensureStyles() {
    if (document.querySelector('link[href*="ops-sla.css"]')) return;
    const href = SELF ? new URL("ops-sla.css", SELF).href : "/static/ops-sla.css";
    document.head.append(h("link", { rel: "stylesheet", href }));
  }
  function keepFocus(root, render) {
    const active = document.activeElement;
    const id = active && root.contains(active) ? active.dataset.focusId : null;
    render();
    if (id) {
      const again = [...root.querySelectorAll("[data-focus-id]")].find((n) => n.dataset.focusId === id);
      if (again) again.focus({ preventScroll: true });
    }
  }
  const NICE = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
  function niceTop(max) {
    const step = NICE.find((n) => max / n <= 5) || Math.ceil(max / 5);
    return { top: Math.max(step, Math.ceil(max / step) * step), step };
  }

  /** Line chart over the hours of today. m = {points: [{t, ts, v: {key: n}}], series: [{key, label, cls}], note(p), ariaLabel}. */
  function paintLines(host, m) {
    const { points, series } = m;
    host._width = host.clientWidth;
    const W = Math.max(host.clientWidth, 320), H = 250;
    const pad = { l: 34, r: 118, t: 14, b: 28 };
    const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
    let t0 = points[0].t, t1 = points[points.length - 1].t;
    if (t1 - t0 < 45 * 60000) { const mid = (t0 + t1) / 2; t0 = mid - 22.5 * 60000; t1 = mid + 22.5 * 60000; }
    const { top, step } = niceTop(Math.max(1, ...points.flatMap((p) => series.map((s) => p.v[s.key] || 0))));
    const X = (t) => pad.l + ((t - t0) / (t1 - t0)) * pw, Y = (v) => pad.t + ph - (v / top) * ph;

    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": m.ariaLabel });
    for (let v = 0; v <= top; v += step) {
      svg.append(svgEl("line", { class: v === 0 ? "axis-line" : "grid-line", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }));
      const t = svgEl("text", { class: "ax", x: pad.l - 6, y: Y(v) + 4, "text-anchor": "end" });
      t.textContent = v;
      svg.append(t);
    }
    const stepMin = [10, 15, 30, 60, 120, 180].find((s) => ((t1 - t0) / 60000 / s) <= Math.max(3, pw / 70)) || 240;
    for (let t = Math.ceil(t0 / (stepMin * 60000)) * stepMin * 60000; t <= t1; t += stepMin * 60000) {
      const d = new Date(t), tx = svgEl("text", { class: "ax", x: X(t), y: H - 8, "text-anchor": "middle" });
      tx.textContent = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
      svg.append(tx);
    }
    for (const s of series) {
      svg.append(svgEl("path", { class: `ln ${s.cls}`, d: points.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Y(p.v[s.key] || 0).toFixed(1)}`).join(" ") }));
    }
    const last = points[points.length - 1];
    const ends = series.map((s) => Y(last.v[s.key] || 0)).sort((a, b) => a - b);
    const crowded = ends.some((y, i) => i && y - ends[i - 1] < 16);        // colliding labels are left to the legend
    for (const s of series) {
      const y = Y(last.v[s.key] || 0);
      svg.append(svgEl("circle", { class: `dot-end ${s.cls}`, cx: X(last.t), cy: y, r: 4 }));
      if (!crowded) {
        const t = svgEl("text", { class: "end-label", x: X(last.t) + 10, y: y + 4 });
        t.textContent = `${s.label.split(" ")[0]} ${last.v[s.key] || 0}`;
        svg.append(t);
      }
    }
    const cross = svgEl("line", { class: "crosshair", y1: pad.t, y2: pad.t + ph, x1: 0, x2: 0, visibility: "hidden" });
    svg.append(cross);
    for (const mk of m.markers || []) {
      const cx = X(mk.t), cy = Y(mk.y);
      svg.append(svgEl("path", { class: "mark-break", d: `M${cx},${cy - 6} L${cx + 6},${cy} L${cx},${cy + 6} L${cx - 6},${cy} Z` }));
    }
    const hit = svgEl("rect", { class: "hit", x: pad.l, y: pad.t, width: pw, height: ph + 6, tabindex: "0", role: "group",
      "aria-label": "Chart values. Use the left and right arrow keys to move between scans." });
    svg.append(hit);

    const tip = host._tip || (host._tip = h("div", { class: "tooltip sx-tip", hidden: true, role: "status" }));
    let idx = points.length - 1;
    const show = (i) => {
      idx = Math.max(0, Math.min(points.length - 1, i));
      const p = points[idx], note = m.note ? m.note(p) : "";
      tip.replaceChildren(h("div", { class: "tt-title" }, `Scan at ${hhmm(p.ts)}`),
        ...series.map((s) => h("div", { class: "tt-row" }, h("i", { class: `key ${s.cls}` }), h("strong", {}, p.v[s.key] || 0), h("span", { class: "label" }, s.label))),
        note ? h("div", { class: "tt-note" }, note) : null,
        ...(m.markers || []).filter((mk) => mk.t === p.t).map((mk) => h("div", { class: "tt-note" }, mk.text(), ...mk.users.slice(0, 3).map((u, n) => [n ? ", " : ": ", bidi(u)]))));
      tip.hidden = false;
      const x = X(p.t);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      tip.style.left = `${Math.min(Math.max(x - tip.offsetWidth / 2, 0), W - tip.offsetWidth)}px`;
      tip.style.top = `${Math.max(0, Y(Math.max(...series.map((s) => p.v[s.key] || 0))) - tip.offsetHeight - 12)}px`;
    };
    const hide = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
    const nearest = (ev) => {
      const box = svg.getBoundingClientRect(), x = ((ev.clientX - box.left) / box.width) * W;
      let best = 0;
      points.forEach((p, i) => { if (Math.abs(X(p.t) - x) < Math.abs(X(points[best].t) - x)) best = i; });
      return best;
    };
    hit.addEventListener("pointermove", (ev) => show(nearest(ev)));
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", () => show(idx));
    hit.addEventListener("blur", hide);
    hit.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") { ev.preventDefault(); show(idx + (ev.key === "ArrowRight" ? 1 : -1)); }
    });
    host.replaceChildren(svg, tip);
  }

  /** The browser's clipboard needs https; the Hub is usually plain http on the bank network, so fall back to a hidden textarea. */
  async function copyText(text) {
    if (window.isSecureContext && navigator.clipboard) {
      try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall through */ }
    }
    const box = h("textarea", { class: "sr-only", "aria-hidden": "true", tabindex: "-1", readonly: "" });
    box.value = text;
    document.body.append(box);
    box.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    box.remove();
    return ok;
  }

  // ================================================================== view memory
  function loadView() {
    try {
      const v = JSON.parse(sessionStorage.getItem(VIEW_KEY) || "{}");
      if (typeof v.q === "string") state.q = v.q.slice(0, 100);
      if (v.open && typeof v.open === "object") for (const [k, on] of Object.entries(v.open)) if (typeof on === "boolean") state.open[k] = on;
    } catch (e) { /* no storage: fine */ }
  }
  function saveView() {
    try { sessionStorage.setItem(VIEW_KEY, JSON.stringify({ q: state.q, open: state.open })); } catch (e) { /* ignore */ }
  }

  // ================================================================== data helpers
  const isRollback = (category) => /rollback/i.test(String(category));
  const categoryList = (data) => {
    const names = [...(data.categories || [])];
    for (const role of data.roles || []) for (const c of Object.keys(role.categories || {})) if (!names.includes(c)) names.push(c);
    return names;
  };
  /** 0 = ok (green), more = warn (amber), rollback requests above 0 = danger (red), null = na (grey). */
  function tone(category, count) {
    if (count == null) return "na";
    if (count === 0) return "ok";
    return isRollback(category) ? "danger" : "warn";
  }
  function categoryTotal(data, category) {
    if (data.totals && data.totals[category] != null) return data.totals[category];
    return (data.roles || []).reduce((n, r) => n + (((r.categories || {})[category] || {}).count || 0), 0);
  }
  const roleTotal = (role) => Object.values(role.categories || {}).reduce((n, c) => n + (c && c.count ? c.count : 0), 0);
  const words = () => normAr(state.q).split(/\s+/).filter(Boolean);

  function visibleRows(entry) {
    const w = words(), rows = entry.rows || [];
    if (!w.length) return rows;
    return rows.filter((r) => {
      const hay = r._hay || (r._hay = normAr(`${r.number} ${r.summary}`));
      return w.every((x) => hay.includes(x));
    });
  }
  function isOpen(role, category, entry) {
    if (words().length) return visibleRows(entry).length > 0;
    const key = `${role.role}|${category}`;
    if (key in state.open) return state.open[key];
    return (entry.count || 0) > 0 && (entry.rows || []).length > 0 && (entry.rows || []).length <= AUTO_OPEN_MAX;
  }

  // ================================================================== banner
  function failureBanner(snap) {
    const f = state.failure, lead = f.network ? "Cannot reach the server" : `The server reported a problem (${f.message})`;
    return { level: "warn", icon: "wifi", text: `${lead} - retrying.${snap ? ` Showing the scan from ${hhmm(snap.generated_at)}.` : ""}` };
  }
  function freshness() {
    const m = state.meta, snap = state.snap;
    if (state.failure) return failureBanner(snap);
    if (!m) return { level: "info", icon: "clock", text: "Loading…" };
    if (m.problem) return { level: "danger", icon: "alert", text: `The scan file cannot be read: ${m.problem}` };
    if (!snap) {
      if (m.enabled === false) return { level: "info", icon: "dash", text: "ITSM monitoring is switched off in Settings." };
      return { level: "info", icon: "clock", text: m.online ? "No scan yet - the collector is running and the first scan will appear here." : "No scan yet - the collector has not run." };
    }
    const when = hhmm(snap.generated_at), ago = minutesAgo(snap.generated_at);
    const agoText = ago == null ? "" : ago < 1 ? "just now" : `${fmtAge(ago)} ago`;
    if (m.enabled === false) return { level: "info", icon: "dash", text: `Monitoring is switched off. Showing the last scan from ${when}.` };
    if (m.last_status === "error") {
      return { level: "danger", icon: "alert", text: `The last scan failed${m.last_run_at ? ` (${hhmm(m.last_run_at)})` : ""}: ${m.last_error || "unknown error"}. Showing the scan from ${when}.` };
    }
    if (!m.online) return { level: "danger", icon: "alert", text: `The collector is offline. Showing the scan from ${when} (${agoText}).` };
    if (m.stale) return { level: "warn", icon: "clock", text: `The data is ${fmtAge(ago)} old although scans run every ${m.interval_minutes} min. Showing the scan from ${when}.` };
    if (!m.active_now) return { level: "info", icon: "clock", text: `Outside working hours - showing the last scan from ${when}.` };
    const next = m.next_run_at ? ` · next about ${hhmm(m.next_run_at)}` : "";
    return { level: "ok", icon: "check", text: `Live · last scan ${when} (${agoText})${next}${m.host ? ` · ${m.host}` : ""}` };
  }
  function renderBanner() {
    const f = freshness(), key = `${f.level}|${f.text}`;
    if (el.banner._key === key) return;                           // a live region must not repeat itself every few seconds
    el.banner._key = key;
    el.banner.className = `sx-banner sx-fresh ${f.level}`;
    el.banner.replaceChildren(glyph(f.icon), h("span", { class: "sx-banner-text", dir: "auto" }, f.text));
  }

  // ================================================================== totals strip
  function tile(label, value, sub, toneName, key) {
    return h("div", { class: `sx-kpi static tone-${toneName}`, "data-kpi": key },
      h("span", { class: "sx-kpi-label" }, glyph(TONE_ICON[toneName]), label),
      h("strong", { class: "sx-kpi-value", "data-kpi-value": key }, value), h("span", { class: "sx-kpi-sub" }, sub || ""));
  }

  function renderTotals() {
    const data = state.snap.data, roles = data.roles || [];
    const failed = roles.filter((r) => r.error).length;
    const tiles = categoryList(data).map((c) => {
      const total = categoryTotal(data, c), n = roles.filter((r) => ((r.categories || {})[c] || {}).count > 0).length;
      return tile(c, fmtNum(total), total === 0 ? "nothing waiting" : `in ${n} role${n === 1 ? "" : "s"}`, tone(c, total), c);
    });
    tiles.push(tile("Roles read", roles.length ? `${roles.length - failed} of ${roles.length}` : "0", failed ? `${failed} could not be read` : roles.length ? "all roles answered" : "no role in this scan", failed ? "danger" : roles.length ? "ok" : "na", "roles"));
    el.totals.replaceChildren(...tiles);
    el.errorsBanner.hidden = !failed;
    if (failed) {
      el.errorsBanner.replaceChildren(glyph("alert"), h("span", { class: "sx-banner-text" },
        `${failed} of ${roles.length} role${roles.length === 1 ? "" : "s"} could not be read, so ${failed === 1 ? "its" : "their"} requests are missing from the totals. The reason is shown on the role card.`));
    }
  }

  // ================================================================== role cards
  function copyButton(number) {
    const btn = h("button", { type: "button", class: "sx-copy", title: "Copy the request number", "aria-label": `Copy request number ${number}` }, glyph("copy"));
    btn.addEventListener("click", async () => {
      const ok = await copyText(String(number));
      toast(ok ? `Copied ${number}` : "Could not copy - select the number and press Ctrl+C.", ok ? undefined : "error");
      if (ok) { btn.replaceChildren(glyph("check")); setTimeout(() => btn.replaceChildren(glyph("copy")), 1400); }
    });
    return btn;
  }

  function requestList(rows) {
    return h("ul", { class: "sx-reqs" }, rows.map((r) => h("li", { "data-number": r.number },
      h("span", { class: "sx-num-cell" }, r.number, copyButton(r.number)), h("span", { class: "sx-summary" }, bidi(r.summary || "")))));
  }

  function categoryRow(role, category) {
    const entry = (role.categories || {})[category];
    const count = entry ? entry.count : null, t = tone(category, count);
    if (count == null) {
      return h("div", { class: "sx-cat", "data-category": category, "data-tone": "na" },
        h("div", { class: "sx-cat-head" }, h("span", { class: "sx-cat-name muted" }, category), h("span", { class: "sx-na" }, "not on this scoreboard")));
    }
    const chipEl = h("span", { class: `sx-count-chip tone-${t}`, "data-count": String(count) }, glyph(TONE_ICON[t]), fmtNum(count));
    const rows = visibleRows(entry), open = isOpen(role, category, entry);
    const expandable = count > 0;
    const key = `${role.role}|${category}`;
    const head = expandable
      ? h("div", { class: "sx-cat-head" },
        h("button", { type: "button", class: "sx-cat-toggle", "aria-expanded": String(open), "data-focus-id": `cat-${key}`,
          title: open ? "Hide the requests" : "Show the requests", onclick: () => toggle(key, !open) }, caret(), h("span", { class: "sx-cat-name" }, category)), chipEl)
      : h("div", { class: "sx-cat-head" }, h("span", { class: "sx-cat-name" }, category), chipEl);
    const notes = [];
    if (entry.warning) notes.push(h("p", { class: "sx-cat-note warn" }, entry.warning));
    if (entry.note) notes.push(h("p", { class: "sx-cat-note" }, entry.note));
    let body = null;
    if (expandable && open) {
      body = rows.length ? requestList(rows) : h("p", { class: "sx-cat-note" }, words().length ? "No request matches the search." : "The scan did not read the request numbers for this list.");
    }
    return h("div", { class: "sx-cat", "data-category": category, "data-tone": t }, head, ...notes, body);
  }
  function caret() {
    const c = glyph("caret");
    c.classList.add("sx-caret");
    return c;
  }

  function toggle(key, on) {
    state.open[key] = on;
    saveView();
    keepFocus(el.cards, renderCards);
  }

  function roleMatches(role) {
    if (!words().length) return true;
    return Object.values(role.categories || {}).some((e) => e && visibleRows(e).length > 0);
  }

  function roleCard(role, categories) {
    const total = roleTotal(role), err = role.error;
    const id = `sx-role-${(state.snap.data.roles || []).indexOf(role)}`;
    const head = h("div", { class: "sx-role-head" }, h("h3", { id }, bidi(role.role)),
      err ? h("span", { class: "sx-count-chip tone-danger" }, glyph("alert"), "Error")
        : h("span", { class: "hint", title: "Open requests in this role" }, `${fmtNum(total)} open`));
    if (err) {
      return h("section", { class: "card sx-role has-error", "aria-labelledby": id, "data-role": role.role, "data-error": "1" }, head,
        h("div", { class: "sx-error", role: "group" }, glyph("alert"), h("div", {}, h("strong", {}, "This role could not be read"), h("span", { dir: "auto" }, String(err)))));
    }
    return h("section", { class: "card sx-role", "aria-labelledby": id, "data-role": role.role }, head,
      h("div", { class: "sx-role-body" }, categories.map((c) => categoryRow(role, c))));
  }

  function renderCards() {
    const data = state.snap.data, categories = categoryList(data);
    const roles = (data.roles || []).filter((r) => r.error ? !words().length : roleMatches(r));
    roles.sort((a, b) => !!b.error - !!a.error);                 // a role that could not be read comes first; the sort is stable
    el.cards.replaceChildren(...roles.map((r) => roleCard(r, categories)));
    el.noRoles.hidden = roles.length > 0 || !(data.roles || []).length;
    el.noRoles.textContent = "No request matches the search.";
    el.noData.hidden = (data.roles || []).length > 0;
  }

  function setAll(on) {
    const data = state.snap.data;
    for (const role of data.roles || []) {
      for (const [c, e] of Object.entries(role.categories || {})) if (e && e.count > 0) state.open[`${role.role}|${c}`] = on;
    }
    saveView();
    keepFocus(el.cards, renderCards);
  }

  // ================================================================== changes since the previous scan
  function changeItem(c) {
    return h("li", { "data-number": c.number },
      h("span", { class: "sx-num-cell" }, c.number, copyButton(c.number)), h("span", { class: "sx-summary" }, bidi(c.summary || "")),
      h("span", { class: "where" }, bidi(c.role), " · ", c.category));
  }
  function changeColumn(title, items, toneName, icon) {
    const shown = items.slice(0, CHANGES_MAX);
    return h("div", { "data-changes": title.toLowerCase() },
      h("h3", {}, h("span", { class: `sx-count-chip tone-${toneName}` }, glyph(icon), fmtNum(items.length)), title),
      items.length ? h("ul", {}, shown.map(changeItem), items.length > shown.length ? h("li", { class: "where" }, `and ${items.length - shown.length} more`) : null)
        : h("p", { class: "sx-quiet" }, "None"));
  }
  function renderChanges() {
    const ch = state.snap.data.changes || {}, news = ch.new || [], closed = ch.closed || [];
    el.changes.replaceChildren(!news.length && !closed.length
      ? h("div", { class: "sx-calm ok" }, glyph("check"), h("div", {}, h("strong", {}, "No changes"), h("p", { class: "muted" }, "Nothing new and nothing closed since the previous scan.")))
      : h("div", { class: "sx-changes" }, changeColumn("New", news, "warn", "alert"), changeColumn("Closed", closed, "ok", "check")));
    el.changesCount.textContent = news.length || closed.length ? `${news.length} new · ${closed.length} closed` : "";
  }

  // ================================================================== history chart
  async function loadHistory() {
    try {
      const answer = await api("GET", "/api/ops/itsm/history?days=1");
      const day = (answer.days || []).find((d) => d.day === state.today);
      state.history = { points: day ? day.points || [] : [] };
    } catch (e) {
      state.history = null;
    }
  }
  function historySeries() {
    return categoryList(state.snap.data).slice(0, 4).map((c, i) => ({ key: c, label: c, cls: `ln-c${i}` }));
  }
  function historyPoints() {
    return ((state.history && state.history.points) || []).map((p) => ({ ts: p.ts, t: naiveMs(p.ts), v: p.totals || {} })).filter((p) => p.t != null).sort((a, b) => a.t - b.t);
  }
  function drawHistory() {
    const host = el.chart, points = historyPoints(), series = historySeries();
    host._model = null;
    el.chartLegend.replaceChildren(...series.map((s) => h("span", {}, h("i", { class: `key ${s.cls}` }), s.label)));
    if (!points.length) {
      host.replaceChildren(h("p", { class: "empty" }, "The history fills in as scans happen during the day."));
      el.chartToggle.hidden = true;
      el.chartTable.hidden = true;
      host.hidden = false;
      return;
    }
    host._model = { points, series, ariaLabel: `Open requests per category today, ${points.length} scans from ${hhmm(points[0].ts)} to ${hhmm(points[points.length - 1].ts)}` };
    paintLines(host, host._model);
    if (!host._ro) {
      host._ro = new ResizeObserver(() => { if (host._model && host.clientWidth !== host._width) paintLines(host, host._model); });
      host._ro.observe(host);
    }
    el.chartTable.replaceChildren(h("table", { class: "data sx-table" }, h("caption", { class: "sr-only" }, "Open requests per category at each scan today"),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Scan"), series.map((s) => h("th", { scope: "col", class: "num" }, s.label)))),
      h("tbody", {}, [...points].reverse().map((p) => h("tr", {}, h("th", { scope: "row" }, hhmm(p.ts)), series.map((s) => h("td", { class: "num" }, p.v[s.key] == null ? "—" : p.v[s.key])))))));
    el.chartToggle.hidden = false;
    applyChartView();
  }
  function applyChartView() {
    const has = !!el.chart._model;
    el.chartToggle.textContent = state.chartAsTable ? "View as chart" : "View as table";
    el.chart.hidden = has && !!state.chartAsTable;
    el.chartTable.hidden = !(has && state.chartAsTable);
  }

  // ================================================================== "Run scan now"
  /** The sentence is a polite live region and changes only with the phase; the running clock beside it is silent. */
  function setRunLine(text, clock) {
    el.runLine.hidden = !text;
    if (el.runText.textContent !== (text || "")) el.runText.textContent = text || "";
    el.runTimer.textContent = clock || "";
  }
  function updateRunUI() {
    const w = state.waiting;
    el.runBtn.disabled = !!w;
    el.runBtn.replaceChildren(glyph("refresh"), w ? "Scanning…" : "Run scan now");
    el.runBtn.classList.toggle("is-busy", !!w);
    if (!w) return;
    const secs = Math.floor((Date.now() - w.since) / 1000), m = state.meta || {};
    const phase = m.running ? `The collector${m.host ? ` (${m.host})` : ""} is reading the ITSM scoreboards now`
      : m.online === false ? "The collector looks offline - the request waits until it is back" : "Waiting for the collector to pick up the request";
    setRunLine(`${phase}…`, `${Math.floor(secs / 60)}:${pad2(secs % 60)}`);
  }
  async function runNow() {
    if (state.waiting) return;
    state.waiting = { since: Date.now(), generated: state.generatedAt, lastRun: state.meta ? state.meta.last_run_at : null, timer: null };
    updateRunUI();
    try {
      const answer = await api("POST", "/api/ops/jobs/itsm/run", {});
      if (answer.already_pending) toast("A scan was already requested - waiting for it.");
    } catch (e) {
      state.waiting = null;
      updateRunUI();
      toast(e.message, "error");
      return;
    }
    state.waiting.timer = setInterval(updateRunUI, 1000);
    schedule(500);
  }
  function endWaiting(message, kind) {
    if (!state.waiting) return;
    clearInterval(state.waiting.timer);
    state.waiting = null;
    updateRunUI();
    setRunLine(message || "");
    if (message) toast(message, kind);
    if (message && kind !== "error") setTimeout(() => { if (!state.waiting) setRunLine(""); }, 6000);
  }
  function checkWaiting() {
    const w = state.waiting, m = state.meta || {};
    if (!w) return;
    if (state.generatedAt && state.generatedAt !== w.generated) {
      const data = state.snap.data, total = categoryList(data).reduce((n, c) => n + categoryTotal(data, c), 0);
      endWaiting(`Scan finished at ${hhmm(state.generatedAt)}: ${fmtNum(total)} open request${total === 1 ? "" : "s"}.`);
    } else if (m.last_run_at && m.last_run_at !== w.lastRun && m.last_status === "error") {
      endWaiting(`The scan failed: ${m.last_error || "unknown error"}.`, "error");
    } else if (Date.now() - w.since > WAIT_LIMIT_MS) {
      endWaiting("The collector has not delivered a new scan yet. The request stays queued and this page updates by itself.", "error");
    }
  }

  // ================================================================== data loop
  let timer = null, failures = 0;

  let inflight = null;
  function refresh(force) {
    if (!inflight) inflight = load(force).finally(() => { inflight = null; });
    return inflight;
  }

  async function load(force) {
    const since = !force && state.generatedAt ? `?since=${encodeURIComponent(state.generatedAt)}` : "";
    const answer = await api("GET", `/api/ops/itsm${since}`);
    state.failure = null;
    state.meta = answer.meta;
    noteServerTime(answer.meta.server_time);
    state.today = String(answer.meta.server_time || "").slice(0, 10);
    if (!answer.unchanged) {
      const fresh = answer.data ? answer : null;
      const isNew = (fresh && fresh.generated_at) !== state.generatedAt;
      state.snap = fresh;
      state.generatedAt = fresh ? fresh.generated_at : "";
      if (isNew) await loadHistory();
    }
    render();
    checkWaiting();
  }
  function schedule(ms) {
    clearTimeout(timer);
    const delay = ms != null ? ms : (state.waiting ? WAIT_POLL_MS : Math.min(POLL_MS * 2 ** Math.min(failures, 2), 60000)) + Math.random() * 1500;
    timer = setTimeout(poll, delay);
  }
  async function poll() {
    if (document.hidden) return;                    // visibilitychange restarts the loop
    try {
      await refresh(false);
      failures = 0;
    } catch (e) {
      failures += 1;
      state.failure = { network: !!e.network, message: e.message };
      if (!e.network && failures === 1) toast(e.message, "error");
      renderBanner();
    }
    schedule();
  }

  // ================================================================== page
  function render() {
    renderBanner();
    const snap = state.snap;
    el.content.hidden = !snap;
    el.empty.hidden = !!snap;
    el.exportLink.hidden = !snap;
    el.scanInfo.textContent = snap ? `Scan of ${snap.generated_at.replace("T", " ").slice(0, 16)}${snap.duration_seconds ? ` · took ${Math.round(snap.duration_seconds)} s` : ""}` : "";
    if (!snap) return;
    if (el.content.dataset.rendered !== snap.generated_at) {
      el.content.dataset.rendered = snap.generated_at;
      renderTotals();
      renderChanges();
      drawHistory();
      keepFocus(el.cards, renderCards);
    }
  }

  function build() {
    el.runBtn = h("button", { type: "button", class: "btn btn-primary", onclick: runNow }, glyph("refresh"), "Run scan now");
    el.exportLink = h("a", { class: "btn btn-ghost", href: "/export/ops/itsm.xlsx", download: "" }, glyph("download"), "Export Excel");
    el.scanInfo = h("span", { class: "hint sx-scan" });
    el.banner = h("div", { class: "sx-banner sx-fresh info", role: "status" });
    el.runText = h("span");
    el.runTimer = h("span", { class: "sx-timer", "aria-hidden": "true" });
    el.runLine = h("p", { class: "sx-runline", role: "status", "aria-live": "polite", hidden: true }, el.runText, " ", el.runTimer);
    el.errorsBanner = h("div", { class: "sx-banner danger", role: "alert", hidden: true });
    el.totals = h("div", { class: "sx-totals", role: "group", "aria-label": "Open requests per category" });

    el.changes = h("div");
    el.changesCount = h("span", { class: "hint" });
    el.changesCard = h("section", { class: "card", "aria-labelledby": "sx-ch-h" }, h("div", { class: "card-head" }, h("h2", { id: "sx-ch-h" }, "Changes since the previous scan"), el.changesCount), el.changes);

    el.chart = h("div", { class: "chart sx-chart" });
    el.chartTable = h("div", { class: "table-wrap sx-chart-table", hidden: true });
    el.chartToggle = h("button", { type: "button", class: "btn btn-ghost btn-sm", hidden: true, onclick: () => { state.chartAsTable = !state.chartAsTable; applyChartView(); } }, "View as table");
    el.chartLegend = h("div", { class: "legend sx-legend" });
    el.chartCard = h("section", { class: "card chart-card", "aria-labelledby": "sx-hi-h" },
      h("div", { class: "card-head" }, h("h2", { id: "sx-hi-h" }, "Open requests today"), el.chartToggle), el.chartLegend, el.chart, el.chartTable);

    let typing = 0;
    el.search = h("input", { type: "search", class: "search", placeholder: "Find a request number or text…", "aria-label": "Search the requests", dir: "auto", value: state.q,
      oninput: () => { clearTimeout(typing); typing = setTimeout(() => { state.q = el.search.value; saveView(); keepFocus(el.cards, renderCards); }, 150); } });
    el.cards = h("div", { class: "sx-cards" });
    el.noRoles = h("p", { class: "empty", hidden: true });
    el.noData = h("div", { class: "sx-calm", hidden: true }, glyph("dash"), h("div", {}, h("strong", {}, "No roles in this scan"), h("p", { class: "muted" }, "The scan read no ITSM role. Check the roles in Settings.")));
    el.toolbar = h("div", { class: "sx-toolbar" }, h("h2", {}, "Roles"), el.search, h("span", { class: "grow" }),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => setAll(true) }, "Expand all"),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => setAll(false) }, "Collapse all"));

    el.content = h("div", { class: "sx-stack", hidden: true }, el.errorsBanner, el.totals, h("div", { class: "sx-two" }, el.changesCard, el.chartCard), el.toolbar, el.cards, el.noRoles, el.noData);
    el.empty = h("section", { class: "card sx-calm big", hidden: true }, glyph("clock"),
      h("div", {}, h("strong", {}, "No scan yet"), h("p", { class: "muted" }, "The collector writes the first ITSM scan here as soon as it has run. You can ask for one now with the button above.")));

    const root = document.getElementById("itsm-page") || document.getElementById("app") || $("main") || document.body;
    root.replaceChildren(h("div", { class: "sx-page" },
      h("div", { class: "sx-titlebar" }, h("div", {}, h("h1", {}, "ITSM requests"), el.scanInfo), h("span", { class: "grow" }), el.runBtn, el.exportLink),
      el.banner, el.runLine, el.empty, el.content));
  }

  function start() {
    ensureStyles();
    loadView();
    build();
    refresh(true).catch((e) => { state.failure = { network: !!e.network, message: e.message }; renderBanner(); if (!e.network) toast(e.message, "error"); }).finally(() => schedule());
    setInterval(() => { if (!document.hidden && state.snap) renderBanner(); }, TICK_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { renderBanner(); schedule(0); } });
  }

  start();
})();
