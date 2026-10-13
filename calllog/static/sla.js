"use strict";
/* SLA page (/sla): security requests waiting for the "security users" (docs/HUB-DESIGN.md sections 5.1 and 7).

   Reads GET /api/ops/sla (the last good scan + collector health) every 15 s with ?since=, so an unchanged scan costs
   almost nothing, and draws: KPI tiles, a per-security-user table, the request list (filters, sorting, paging) and
   today's timeline. Ages tick live between scans from "sent_at" and the server clock. "Run scan now" asks the
   collector for a scan and waits (visibly, with a time limit) until a newer scan appears.

   Every scraped text goes in with textContent (CL.h) - never innerHTML - and Arabic text carries dir="auto". */
(function () {
  const { h, $, api, toast, bidi, svgEl, normAr, fmtNum } = CL;
  const SELF = document.currentScript ? document.currentScript.src : "";

  const POLL_MS = 15000;          // how often the page asks for news
  const WAIT_POLL_MS = 3000;      // ... while "Run scan now" is waiting
  const WAIT_LIMIT_MS = 4 * 60 * 1000;
  const TICK_MS = 10000;          // how often ages / "x min ago" are recomputed
  const PAGE_SIZES = [25, 50, 100];
  const VIEW_KEY = "ops.sla.view";

  const STATUSES = ["breached", "reminder", "within", "no_date"];
  const LABELS = { breached: "Breached", reminder: "Reminder", within: "Within SLA", no_date: "No sent date" };
  const RANK = { breached: 0, reminder: 1, within: 2, no_date: 3 };
  const STATUS_ICON = { breached: "alert", reminder: "clock", within: "check", no_date: "dash" };
  const PATHS = {
    refresh: "M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5", alert: "M12 3 2 20h20L12 3zM12 10v4M12 17h.01",
    clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2", check: "M20 6 9 17l-5-5", dash: "M6 12h12",
    x: "M18 6 6 18M6 6l12 12", download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
    wifi: "M2 9a15 15 0 0 1 20 0M5.5 12.5a10 10 0 0 1 13 0M9 16a5 5 0 0 1 6 0M12 20h.01",
  };

  const state = {
    snap: null,              // last full answer (envelope with data)
    meta: null,              // collector health that came with it
    generatedAt: "",
    history: null,           // today's {points, first_breach}
    today: "",
    failure: null,
    waiting: null,           // {since, generated, lastRun} while "Run scan now" waits
    filters: { status: "", user: "", q: "" },
    sort: { key: "priority", desc: false },
    userSort: { key: "breached", desc: true },
    page: 1,
    pageSize: PAGE_SIZES[0],
    viewAsTable: false,
  };
  const el = {};
  let skew = 0;              // server clock minus this browser's clock, in ms

  // ------------------------------------------------------------------ small helpers
  function glyph(name) {
    const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "sx-ic" });
    svg.append(svgEl("path", { d: PATHS[name] }));
    return svg;
  }

  /** Dates in the snapshots are naive local time of the PCs: read them as plain numbers, never through the browser's zone. */
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

  function fmtAge(min) {
    if (min == null) return "—";
    if (min < 60) return `${min} min`;
    const hrs = Math.floor(min / 60);
    if (hrs < 24) return `${hrs} h ${pad2(min % 60)} min`;
    return `${Math.floor(hrs / 24)} d ${hrs % 24} h`;
  }
  const fmtLimit = (min) => (min && min % 60 === 0 ? `${min / 60} h` : `${min} min`);

  /** Minutes since the request was sent to the security user, counted now (null when there is no sent date). */
  function ageOf(r) {
    const t = r._t === undefined ? (r._t = naiveMs(r.sent_at)) : r._t;
    if (t == null) return null;
    return Math.max(0, Math.floor((serverNow() - t) / 60000));
  }
  function minutesAgo(iso) {
    const t = naiveMs(iso);
    return t == null ? null : Math.max(0, Math.floor((serverNow() - t) / 60000));
  }
  const limits = () => {
    const t = (state.snap && state.snap.data.thresholds) || {};
    return { warn: t.warning_minutes || 60, breach: t.breach_minutes || 120 };
  };

  /** The status a request has reached by the clock now; the stored one is the status at the last scan. */
  function statusNow(r) {
    const age = ageOf(r);
    if (age == null) return r.status;
    const { warn, breach } = limits();
    const live = age >= breach ? "breached" : age >= warn ? "reminder" : "within";
    return RANK[live] < RANK[r.status] ? live : r.status;
  }

  function loadView() {
    try {
      const v = JSON.parse(sessionStorage.getItem(VIEW_KEY) || "{}");
      if (STATUSES.includes(v.filters && v.filters.status)) state.filters.status = v.filters.status;
      if (v.filters && typeof v.filters.user === "string") state.filters.user = v.filters.user.slice(0, 200);
      if (v.filters && typeof v.filters.q === "string") state.filters.q = v.filters.q.slice(0, 100);
      if (v.sort && ["priority", "age", "user"].includes(v.sort.key)) state.sort = { key: v.sort.key, desc: !!v.sort.desc };
      if (v.userSort && ["user", "total", "within", "reminder", "breached", "oldest"].includes(v.userSort.key)) state.userSort = { key: v.userSort.key, desc: !!v.userSort.desc };
      if (PAGE_SIZES.includes(v.pageSize)) state.pageSize = v.pageSize;
    } catch (e) { /* no storage (private window): the page works without it */ }
  }
  function saveView() {
    try {
      sessionStorage.setItem(VIEW_KEY, JSON.stringify({ filters: state.filters, sort: state.sort, userSort: state.userSort, pageSize: state.pageSize }));
    } catch (e) { /* ignore */ }
  }

  /** The page's own stylesheet. A template may link it itself; if not, it is added here and the page waits for it (no unstyled flash). */
  function ensureStyles() {
    if (document.querySelector('link[href*="ops-sla.css"]')) return Promise.resolve();
    const href = SELF ? new URL("ops-sla.css", SELF).href : "/static/ops-sla.css";
    return new Promise((done) => {
      const link = h("link", { rel: "stylesheet", href });
      link.addEventListener("load", done);
      link.addEventListener("error", done);
      setTimeout(done, 2000);
      document.head.append(link);
    });
  }

  /** Re-render a block without losing keyboard focus on the control that was used. */
  function keepFocus(root, render) {
    const active = document.activeElement;
    const id = active && root.contains(active) ? active.dataset.focusId : null;
    render();
    if (id) {
      const again = [...root.querySelectorAll("[data-focus-id]")].find((n) => n.dataset.focusId === id);
      if (again) again.focus({ preventScroll: true });
    }
  }

  // ------------------------------------------------------------------ banner (freshness / offline)
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
      if (m.enabled === false) return { level: "info", icon: "dash", text: "Security-request monitoring is switched off in Settings." };
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

  // ------------------------------------------------------------------ KPI tiles
  function renderKpis() {
    const { data } = state.snap, t = data.totals || {}, lim = limits();
    const open = t.open || 0, pct = (n) => (open ? `${Math.round((n / open) * 100)}% of open` : "");
    const users = (data.by_user || []).length;
    const oldest = oldestRequest();
    const tile = (key, label, value, sub, status) => {
      const pressed = key === "open" ? !state.filters.status : state.filters.status === key;
      const body = [h("span", { class: "sx-kpi-label" }, status ? glyph(STATUS_ICON[status]) : null, label),
        h("strong", { class: "sx-kpi-value", "data-kpi-value": key }, value), h("span", { class: "sx-kpi-sub" }, sub || "")];
      if (key === "oldest") return h("div", { class: "sx-kpi static", "data-kpi": key, "data-minutes": oldest.minutes == null ? "" : String(oldest.minutes) }, body);
      return h("button", { type: "button", class: `sx-kpi${status ? ` st-${status}` : ""}`, "data-kpi": key, "data-value": String(value), "data-focus-id": `kpi-${key}`,
        "aria-pressed": String(pressed), title: key === "open" ? "Show all requests" : `Show only: ${label}`,
        onclick: () => setFilter({ status: key === "open" || state.filters.status === key ? "" : key }) }, body);
    };
    el.kpis.replaceChildren(
      tile("open", "Open requests", open, open ? `waiting for ${fmtNum(users)} security user${users === 1 ? "" : "s"}` : "nothing waiting"),
      tile("within", "Within SLA", t.within || 0, [`under ${fmtLimit(lim.warn)}`, pct(t.within || 0)].filter(Boolean).join(" · "), "within"),
      tile("reminder", `Reminder ≥ ${fmtLimit(lim.warn)}`, t.reminder || 0, pct(t.reminder || 0), "reminder"),
      tile("breached", `Breached ≥ ${fmtLimit(lim.breach)}`, t.breached || 0, pct(t.breached || 0), "breached"),
      tile("no_date", "No sent date", t.no_date || 0, "not counted in the SLA", "no_date"),
      tile("oldest", "Oldest", oldest.minutes == null ? "—" : fmtAge(oldest.minutes), oldest.user || ""));
  }

  /** Oldest open request by the clock now (falls back to the scan's number when the rows carry no dates). */
  function oldestRequest() {
    let best = { minutes: null, user: "" };
    for (const r of state.snap.data.requests || []) {
      const age = ageOf(r);
      if (age != null && (best.minutes == null || age > best.minutes)) best = { minutes: age, user: r.security_user };
    }
    if (best.minutes == null && (state.snap.data.totals || {}).oldest_minutes) best.minutes = state.snap.data.totals.oldest_minutes;
    return best;
  }

  // ------------------------------------------------------------------ per-user table
  const USER_COLUMNS = [["user", "Security user", false], ["total", "Total", true], ["within", "Within", true],
    ["reminder", "Reminder", true], ["breached", "Breached", true], ["oldest", "Oldest", true]];

  function liveOldestByUser() {
    const out = new Map();
    for (const r of state.snap.data.requests || []) {
      const age = ageOf(r);
      if (age != null && age > (out.get(r.security_user) ?? -1)) out.set(r.security_user, age);
    }
    return out;
  }

  function sortedUsers() {
    const live = liveOldestByUser();
    const rows = (state.snap.data.by_user || []).map((u) => ({ ...u, oldest: live.has(u.user) ? live.get(u.user) : u.total > (u.no_date || 0) ? u.oldest_minutes : null }));
    const { key, desc } = state.userSort;
    const val = (u) => (key === "user" ? normAr(u.user) : u[key] == null ? -1 : u[key]);
    rows.sort((a, b) => {
      const x = val(a), y = val(b);
      const c = typeof x === "string" ? x.localeCompare(y) : x - y;
      return (desc ? -c : c) || b.breached - a.breached || b.reminder - a.reminder || b.total - a.total;      // a tie keeps the scan's order
    });
    return rows;
  }

  function userHead() {
    return h("tr", {}, USER_COLUMNS.map(([key, label, num]) => sortHeader(label, key, num, state.userSort, (k) => {
      state.userSort = { key: k, desc: state.userSort.key === k ? !state.userSort.desc : k !== "user" };
      saveView();
      renderUsers();
    }, "u")));
  }

  function sortHeader(label, key, num, sort, onSort, scope) {
    const active = sort.key === key;
    const dir = !active ? "none" : sort.desc ? "descending" : "ascending";
    return h("th", { scope: "col", class: num ? "num" : "", "aria-sort": dir },
      h("button", { type: "button", class: "sx-sort", "data-focus-id": `sort-${scope}-${key}`, onclick: () => onSort(key) },
        label, h("span", { class: "sx-arrow", "aria-hidden": "true" }, active ? (sort.desc ? "▼" : "▲") : "")));
  }

  function renderUsers() {
    const users = sortedUsers();
    const selected = state.filters.user;
    const count = (n, status) => h("td", { class: "num" }, n ? h("span", { class: `sx-num has st-${status}` }, fmtNum(n)) : h("span", { class: "sx-num zero" }, "0"));
    keepFocus(el.userCard, () => {
      el.userHead.replaceChildren(userHead());
      el.userBody.replaceChildren(...users.map((u) => h("tr", { class: u.user === selected ? "is-selected" : "", "data-user": u.user },
        h("th", { scope: "row", class: "sx-user" },
          h("button", { type: "button", class: "sx-link", "data-focus-id": `user-${u.user}`, "aria-pressed": String(u.user === selected),
            title: u.user === selected ? "Show all users again" : "Show only this user's requests",
            onclick: () => setFilter({ user: u.user === selected ? "" : u.user }) }, bidi(u.user))),
        h("td", { class: "num" }, h("strong", {}, fmtNum(u.total))), count(u.within, "within"), count(u.reminder, "reminder"), count(u.breached, "breached"),
        h("td", { class: "num sx-oldest", "data-oldest-user": u.user }, fmtAge(u.oldest)))));
    });
    el.userEmpty.hidden = users.length > 0;
    el.userTable.hidden = users.length === 0;
  }

  // ------------------------------------------------------------------ request list
  function visibleRequests() {
    const f = state.filters, words = normAr(f.q).split(/\s+/).filter(Boolean);
    const rows = (state.snap.data.requests || []).filter((r) => {
      if (f.status && r.status !== f.status) return false;
      if (f.user && r.security_user !== f.user) return false;
      if (!words.length) return true;
      const hay = r._hay || (r._hay = normAr([r.security_user, ...Object.values(r.fields || {})].join(" ")));
      return words.every((w) => hay.includes(w));
    });
    const { key, desc } = state.sort;
    const byAge = (a, b) => {                       // oldest first; requests without a date go last
      const x = ageOf(a), y = ageOf(b);
      return x == null && y == null ? 0 : x == null ? 1 : y == null ? -1 : y - x;
    };
    rows.sort((a, b) => {
      if (key === "age") {
        const x = ageOf(a), y = ageOf(b);
        if (x == null || y == null) return byAge(a, b);
        return desc ? y - x : x - y;
      }
      if (key === "user") return (desc ? -1 : 1) * normAr(a.security_user).localeCompare(normAr(b.security_user)) || RANK[a.status] - RANK[b.status] || byAge(a, b);
      return (desc ? -1 : 1) * (RANK[a.status] - RANK[b.status]) || byAge(a, b);       // priority: breached first
    });
    return rows;
  }

  function detailColumns() {
    const cols = [];
    for (const r of state.snap.data.requests || []) {
      for (const k of Object.keys(r.fields || {})) if (!cols.includes(k)) cols.push(k);
      if (cols.length >= 8) break;
    }
    return cols.slice(0, 8);
  }

  function chip(status) {
    return h("span", { class: `sx-chip st-${status}` }, glyph(STATUS_ICON[status]), LABELS[status]);
  }

  /** "now over 2 h" - shown while the clock has passed a limit that the last scan has not seen yet. */
  function cueText(r) {
    const now = statusNow(r);
    if (now === r.status) return "";
    return now === "breached" ? `now over ${fmtLimit(limits().breach)}` : `now over ${fmtLimit(limits().warn)}`;
  }

  function sinceText(sentAt) {
    if (!sentAt) return "";
    const t = hhmm(sentAt), today = state.snap.generated_at.slice(0, 10);
    return sentAt.slice(0, 10) === today ? `since ${t}` : `since ${sentAt.slice(8, 10)}/${sentAt.slice(5, 7)} ${t}`;
  }

  function requestRow(r, cols) {
    const cue = cueText(r);
    const tr = h("tr", { class: `st-${r.status}`, "data-key": r.key, "data-status": r.status },
      h("td", { class: "sx-status" }, chip(r.status), h("span", { class: "sx-cue hint", "data-cue": "" }, cue)),
      h("td", { class: "sx-who" }, bidi(r.security_user)),
      h("td", { class: "sx-age nowrap" }, h("strong", { "data-age": "" }, fmtAge(ageOf(r))), h("span", { class: "hint" }, sinceText(r.sent_at))),
      ...cols.map((c) => h("td", { class: "sx-field" }, (r.fields || {})[c] ? bidi(r.fields[c]) : h("span", { class: "sx-none" }, "—"))));
    tr._req = r;
    return tr;
  }

  function renderRequests() {
    const all = state.snap.data.requests || [];
    el.reqCard.hidden = false;
    el.reqEmpty.hidden = all.length > 0;
    el.reqTools.hidden = el.reqWrap.hidden = el.pager.hidden = all.length === 0;
    if (!all.length) return;
    const rows = visibleRequests(), cols = detailColumns();
    const pages = Math.max(1, Math.ceil(rows.length / state.pageSize));
    state.page = Math.min(Math.max(1, state.page), pages);
    const slice = rows.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);

    el.reqHead.replaceChildren(h("tr", {},
      sortHeader("Status", "priority", false, state.sort, sortRequests, "r"),
      sortHeader("Security user", "user", false, state.sort, sortRequests, "r"),
      sortHeader("Age", "age", false, state.sort, sortRequests, "r"),
      ...cols.map((c) => h("th", { scope: "col" }, bidi(c)))));
    el.reqBody.replaceChildren(...slice.map((r) => requestRow(r, cols)));
    el.noMatch.hidden = rows.length > 0;
    el.reqWrap.hidden = rows.length === 0;
    el.count.textContent = rows.length === all.length ? `${fmtNum(all.length)} open request${all.length === 1 ? "" : "s"}` : `${fmtNum(rows.length)} of ${fmtNum(all.length)} requests`;
    renderPager(rows.length, pages);
    syncControls();
  }

  function sortRequests(key) {
    state.sort = { key, desc: state.sort.key === key ? !state.sort.desc : key === "age" };
    state.page = 1;
    saveView();
    keepFocus(el.reqCard, renderRequests);
  }

  function renderPager(total, pages) {
    const from = total ? (state.page - 1) * state.pageSize + 1 : 0, to = Math.min(total, state.page * state.pageSize);
    el.pager.hidden = total === 0;
    el.pagerText.textContent = total ? `${from}–${to} of ${fmtNum(total)}` : "";
    el.prev.disabled = state.page <= 1;
    el.next.disabled = state.page >= pages;
    el.pageSize.value = String(state.pageSize);
    el.pageInfo.textContent = pages > 1 ? `Page ${state.page} of ${pages}` : "";
  }

  function fillUserFilter() {
    const names = new Set((state.snap.data.by_user || []).map((u) => u.user));
    for (const r of state.snap.data.requests || []) names.add(r.security_user);
    if (state.filters.user) names.add(state.filters.user);
    const sorted = [...names].sort((a, b) => normAr(a).localeCompare(normAr(b)));
    el.userSelect.replaceChildren(h("option", { value: "" }, "All security users"), ...sorted.map((n) => h("option", { value: n }, n)));
    el.userSelect.value = state.filters.user;
  }

  function syncControls() {
    const f = state.filters;
    el.statusSelect.value = f.status;
    el.userSelect.value = f.user;
    if (document.activeElement !== el.search) el.search.value = f.q;
    el.clear.hidden = !(f.status || f.user || f.q);
  }

  function setFilter(patch) {
    Object.assign(state.filters, patch);
    state.page = 1;
    saveView();
    keepFocus(el.kpis, renderKpis);
    renderUsers();
    keepFocus(el.reqCard, renderRequests);
  }

  // ------------------------------------------------------------------ timeline
  const SERIES = [{ key: "open", label: "Open", cls: "ln-open" }, { key: "breached", label: "Breached", cls: "ln-breached" }];

  /** Today's scans as [{t (ms), ts, v: {open, within, reminder, breached}}], oldest first. */
  function timelinePoints() {
    const hist = state.history;
    if (!hist) return [];
    return (hist.points || []).map((p) => ({ ts: p.ts, t: naiveMs(p.ts), v: p })).filter((p) => p.t != null).sort((a, b) => a.t - b.t);
  }

  /** Requests that first crossed the breach limit today, grouped by the scan that saw it (a diamond on the breached line). */
  function breachMarkers(points) {
    const by = new Map();
    const open = new Map((state.snap.data.requests || []).map((r) => [r.key, r]));
    for (const [key, ts] of Object.entries((state.history && state.history.first_breach) || {})) {
      const t = naiveMs(ts);
      if (t == null) continue;
      if (!by.has(t)) by.set(t, { t, ts, count: 0, users: [] });
      const m = by.get(t);
      m.count += 1;
      const r = open.get(key);
      if (r && !m.users.includes(r.security_user)) m.users.push(r.security_user);
    }
    return [...by.values()].sort((a, b) => a.t - b.t).map((m) => {
      const at = [...points].reverse().find((p) => p.t <= m.t) || points[0];
      return { ...m, y: at ? at.v.breached || 0 : 0,
        text: () => `${m.count} request${m.count === 1 ? "" : "s"} passed ${fmtLimit(limits().breach)} here` };
    });
  }

  function timelineModel(points) {
    return { points, series: SERIES, markers: breachMarkers(points), note: (p) => `Within ${p.v.within || 0} · Reminder ${p.v.reminder || 0}`,
      ariaLabel: `Open and breached security requests today, ${points.length} scans from ${hhmm(points[0].ts)} to ${hhmm(points[points.length - 1].ts)}` };
  }

  function drawTimeline() {
    const host = el.chart, points = timelinePoints();
    host._model = null;
    if (!points.length) {
      host.replaceChildren(h("p", { class: "empty" }, "The timeline fills in as scans happen during the day."));
      el.chartLegend.hidden = true;
      return;
    }
    el.chartLegend.hidden = false;
    host._model = timelineModel(points);
    paintLines(host, host._model);
    if (!host._ro) {
      host._ro = new ResizeObserver(() => { if (host._model && host.clientWidth !== host._width) paintLines(host, host._model); });
      host._ro.observe(host);
    }
    fillChartTable(points, host._model.markers);
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

  function fillChartTable(points, markers) {
    const rows = [...points].reverse().map((p) => {
      const first = markers.filter((mk) => mk.t === p.t).reduce((n, mk) => n + mk.count, 0);
      return h("tr", {}, h("th", { scope: "row" }, hhmm(p.ts)), h("td", { class: "num" }, p.v.open), h("td", { class: "num" }, p.v.within),
        h("td", { class: "num" }, p.v.reminder), h("td", { class: "num" }, p.v.breached), h("td", { class: "num" }, first || ""));
    });
    el.chartTable.replaceChildren(h("table", { class: "data sx-table" },
      h("caption", { class: "sr-only" }, "Security requests at each scan today"),
      h("thead", {}, h("tr", {}, ["Scan", "Open", "Within", "Reminder", "Breached", "First over limit"].map((c, i) => h("th", { scope: "col", class: i ? "num" : "" }, c)))),
      h("tbody", {}, rows)));
  }

  function toggleChartView() {
    state.viewAsTable = !state.viewAsTable;
    applyChartView();
  }
  function applyChartView() {
    const has = !!(el.chart._model);
    el.chartToggle.textContent = state.viewAsTable ? "View as chart" : "View as table";
    el.chartToggle.hidden = !has;
    el.chart.hidden = has && state.viewAsTable;
    el.chartTable.hidden = !(has && state.viewAsTable);
  }

  async function loadHistory() {
    try {
      const answer = await api("GET", "/api/ops/sla/history?days=1");
      const day = (answer.days || []).find((d) => d.day === state.today);
      state.history = day ? { points: day.points || [], first_breach: day.first_breach || day.first_break || {} } : { points: [], first_breach: {} };
    } catch (e) {
      state.history = null;                    // the timeline is a bonus: the page works without it
    }
  }

  // ------------------------------------------------------------------ "Run scan now"
  /** The sentence is a polite live region and changes only with the phase; the running clock beside it is silent. */
  function setWaiting(text, clock) {
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
    const clock = `${Math.floor(secs / 60)}:${pad2(secs % 60)}`;
    const phase = m.running ? `The collector${m.host ? ` (${m.host})` : ""} is scanning now`
      : m.online === false ? "The collector looks offline - the request waits until it is back" : "Waiting for the collector to pick up the request";
    setWaiting(`${phase}…`, clock);
  }

  async function runNow() {
    if (state.waiting) return;
    state.waiting = { since: Date.now(), generated: state.generatedAt, lastRun: state.meta ? state.meta.last_run_at : null, timer: null };
    updateRunUI();
    try {
      const answer = await api("POST", "/api/ops/jobs/sla/run", {});
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
    setWaiting(message || "");
    if (message) toast(message, kind);
    if (message && kind !== "error") setTimeout(() => { if (!state.waiting) setWaiting(""); }, 6000);
  }

  function checkWaiting() {
    const w = state.waiting, m = state.meta || {};
    if (!w) return;
    if (state.generatedAt && state.generatedAt !== w.generated) {
      const t = (state.snap && state.snap.data.totals) || {};
      endWaiting(`Scan finished at ${hhmm(state.generatedAt)}: ${fmtNum(t.open || 0)} open, ${fmtNum(t.breached || 0)} breached.`);
    } else if (m.last_run_at && m.last_run_at !== w.lastRun && m.last_status === "error") {
      endWaiting(`The scan failed: ${m.last_error || "unknown error"}.`, "error");
    } else if (Date.now() - w.since > WAIT_LIMIT_MS) {
      endWaiting("The collector has not delivered a new scan yet. The request stays queued and this page updates by itself.", "error");
    }
  }

  // ------------------------------------------------------------------ data loop
  let timer = null, failures = 0;

  let inflight = null;
  function refresh(force) {
    if (!inflight) inflight = load(force).finally(() => { inflight = null; });
    return inflight;
  }

  async function load(force) {
    const since = !force && state.generatedAt ? `?since=${encodeURIComponent(state.generatedAt)}` : "";
    const answer = await api("GET", `/api/ops/sla${since}`);
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
    tick();                                         // the clock offset may have changed: ages move even when the scan did not
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

  /** Between scans only the clock moves: ages, cues and "x min ago" are rewritten in place. */
  function tick() {
    if (document.hidden || !state.snap) return;
    renderBanner();
    for (const tr of el.reqBody.querySelectorAll("tr")) {
      const r = tr._req;
      if (!r) continue;
      tr.querySelector("[data-age]").textContent = fmtAge(ageOf(r));
      tr.querySelector("[data-cue]").textContent = cueText(r);
    }
    const oldest = oldestRequest(), tile = el.kpis.querySelector('[data-kpi="oldest"]');
    if (tile) {
      tile.querySelector("[data-kpi-value]").textContent = oldest.minutes == null ? "—" : fmtAge(oldest.minutes);
      tile.dataset.minutes = oldest.minutes == null ? "" : String(oldest.minutes);
    }
    const live = liveOldestByUser();
    for (const cell of el.userBody.querySelectorAll("[data-oldest-user]")) {
      if (live.has(cell.dataset.oldestUser)) cell.textContent = fmtAge(live.get(cell.dataset.oldestUser));
    }
  }

  // ------------------------------------------------------------------ page
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
      fillUserFilter();
      renderKpis();
      renderUsers();
      renderRequests();
      drawTimeline();
      applyChartView();
    }
  }

  function build() {
    el.runBtn = h("button", { type: "button", class: "btn btn-primary", onclick: runNow }, glyph("refresh"), "Run scan now");
    el.exportLink = h("a", { class: "btn btn-ghost", href: "/export/ops/sla.xlsx", download: "" }, glyph("download"), "Export Excel");
    el.scanInfo = h("span", { class: "hint sx-scan" });
    el.banner = h("div", { class: "sx-banner sx-fresh info", role: "status" });
    el.runText = h("span");
    el.runTimer = h("span", { class: "sx-timer", "aria-hidden": "true" });
    el.runLine = h("p", { class: "sx-runline", role: "status", "aria-live": "polite", hidden: true }, el.runText, " ", el.runTimer);
    el.kpis = h("div", { class: "sx-kpis", role: "group", "aria-label": "Security request totals" });

    el.userHead = h("thead");
    el.userBody = h("tbody");
    el.userTable = h("div", { class: "table-wrap" }, h("table", { class: "data sx-table sx-users" }, h("caption", { class: "sr-only" }, "Requests per security user"), el.userHead, el.userBody));
    el.userEmpty = h("p", { class: "empty", hidden: true }, "No security user has an open request.");
    el.userCard = h("section", { class: "card", "aria-labelledby": "sx-users-h" },
      h("div", { class: "card-head" }, h("h2", { id: "sx-users-h" }, "By security user"), h("span", { class: "hint" }, "click a name to filter the list")), el.userTable, el.userEmpty);

    el.chart = h("div", { class: "chart sx-chart" });
    el.chartTable = h("div", { class: "table-wrap sx-chart-table", hidden: true });
    el.chartToggle = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: toggleChartView }, "View as table");
    el.chartLegend = h("div", { class: "legend sx-legend" },
      h("span", {}, h("i", { class: "key ln-open" }), "Open"), h("span", {}, h("i", { class: "key ln-breached" }), "Breached"),
      h("span", {}, h("i", { class: "key mark-key" }), "First went over the limit"));
    el.chartCard = h("section", { class: "card chart-card", "aria-labelledby": "sx-time-h" },
      h("div", { class: "card-head" }, h("h2", { id: "sx-time-h" }, "Today"), el.chartToggle), el.chartLegend, el.chart, el.chartTable);

    el.statusSelect = h("select", { "aria-label": "Status", onchange: () => setFilter({ status: el.statusSelect.value }) },
      h("option", { value: "" }, "All statuses"), ...STATUSES.map((s) => h("option", { value: s }, LABELS[s])));
    el.userSelect = h("select", { "aria-label": "Security user", onchange: () => setFilter({ user: el.userSelect.value }) }, h("option", { value: "" }, "All security users"));
    let typing = 0;
    el.search = h("input", { type: "search", class: "search", placeholder: "Search name, user number, branch…", "aria-label": "Search requests", dir: "auto",
      oninput: () => { clearTimeout(typing); typing = setTimeout(() => setFilter({ q: el.search.value }), 150); } });
    el.clear = h("button", { type: "button", class: "btn btn-ghost btn-sm", hidden: true, onclick: () => setFilter({ status: "", user: "", q: "" }) }, glyph("x"), "Clear filters");
    el.count = h("span", { class: "sx-count", role: "status" });
    el.reqTools = h("div", { class: "sx-filters" }, el.statusSelect, el.userSelect, el.search, el.clear, h("span", { class: "grow" }), el.count);
    el.reqHead = h("thead");
    el.reqBody = h("tbody");
    el.reqWrap = h("div", { class: "table-wrap" }, h("table", { class: "data sx-table sx-requests" }, h("caption", { class: "sr-only" }, "Open security requests"), el.reqHead, el.reqBody));
    el.noMatch = h("div", { class: "empty", hidden: true }, h("p", {}, "No requests match these filters."),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => setFilter({ status: "", user: "", q: "" }) }, "Clear filters"));
    el.reqEmpty = h("div", { class: "sx-calm ok", hidden: true }, glyph("check"), h("div", {}, h("strong", {}, "No open requests"), h("p", { class: "muted" }, "Nothing is waiting for the security users.")));
    el.prev = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { state.page -= 1; renderRequests(); } }, "Previous");
    el.next = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { state.page += 1; renderRequests(); } }, "Next");
    el.pageSize = h("select", { "aria-label": "Rows per page", onchange: () => { state.pageSize = +el.pageSize.value; state.page = 1; saveView(); renderRequests(); } },
      PAGE_SIZES.map((n) => h("option", { value: String(n) }, `${n} per page`)));
    el.pagerText = h("span");
    el.pageInfo = h("span", { class: "hint" });
    el.pager = h("div", { class: "pager sx-pager" }, el.pagerText, el.pageInfo, el.pageSize, el.prev, el.next);
    el.reqCard = h("section", { class: "card", "aria-labelledby": "sx-req-h" },
      h("div", { class: "card-head" }, h("h2", { id: "sx-req-h" }, "Requests")), el.reqTools, el.reqWrap, el.noMatch, el.reqEmpty, el.pager);

    el.content = h("div", { class: "sx-stack", hidden: true }, el.kpis, h("div", { class: "sx-two" }, el.userCard, el.chartCard), el.reqCard);
    el.empty = h("section", { class: "card sx-calm big", hidden: true }, glyph("clock"),
      h("div", {}, h("strong", {}, "No scan yet"), h("p", { class: "muted" }, "The collector writes the first security-request scan here as soon as it has run. You can ask for one now with the button above.")));

    const root = document.getElementById("sla-page") || document.getElementById("app") || $("main") || document.body;
    root.replaceChildren(
      h("div", { class: "sx-page" },
        h("div", { class: "sx-titlebar" }, h("div", {}, h("h1", {}, "Security requests"), el.scanInfo), h("span", { class: "grow" }), el.runBtn, el.exportLink),
        el.banner, el.runLine, el.empty, el.content));
  }

  async function start() {
    await ensureStyles();
    loadView();
    build();
    refresh(true).catch((e) => { state.failure = { network: !!e.network, message: e.message }; renderBanner(); if (!e.network) toast(e.message, "error"); }).finally(() => schedule());
    setInterval(tick, TICK_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { tick(); schedule(0); } });
  }

  start();
})();
