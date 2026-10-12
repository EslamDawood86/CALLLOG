"use strict";
/* E-mail page (/email): the web replacement for the old staff + manager Outlook dashboards
   (docs/HUB-DESIGN.md sections 5.3 and 7, brief docs/handoff/briefs/page-email.md).

   Reads GET /api/ops/email every 15 s through OPS.loader (?since=, random start offset, back-off, paused while the tab
   is hidden). A manager only ever receives the rows of their own team(s) - the API scopes them; an employee never
   reaches this page (the /email route sends them to /my). Six tabs: Overview, Pending, Replied, Quality, Trends and
   Unclassified. Actions (assign, no-action, change team, quality review, create/remove a classification rule) confirm,
   call the API, toast and re-fetch. Every scraped / e-mail text goes in with textContent (CL.h) and Arabic carries
   dir="auto"; there is no innerHTML and no inline style (strict CSP). */
(function () {
  const { h, $, api, toast, bidi, normAr, fmtNum } = CL;
  const SELF = document.currentScript ? document.currentScript.src : "";
  const page = $("#email-page");
  const ROLE = page.dataset.role || "manager";
  const MY_TEAMS = (page.dataset.teams || page.dataset.team || "").split(";").map((t) => t.trim()).filter(Boolean);
  const VIEW_KEY = "ops.email.view";
  const WAIT_LIMIT_MS = 4 * 60 * 1000;

  const TABS = [
    { key: "overview", label: "Overview" },
    { key: "pending", label: "Pending" },
    { key: "replied", label: "Replied" },
    { key: "quality", label: "Quality" },
    { key: "trends", label: "Trends" },
    { key: "unclassified", label: "Unclassified" },
  ];

  const state = {
    answer: null, data: null, options: null, meta: null, generatedAt: "",
    tab: "overview",
    filters: { team: "", system: "", assigned: "", carryover: false, escalated: false, q: "" },
    history: null, historyLoaded: false,
    waiting: null,
  };
  const el = {};
  const tables = {};

  // ------------------------------------------------------------------ small helpers
  const canActOnTeam = (team) => ROLE === "admin" || MY_TEAMS.includes("__all__")
    || MY_TEAMS.some((t) => normAr(t) === normAr(team));
  const systemsOf = (row) => String(row.detected_systems || "").split(/\s*;\s*/).filter(Boolean);
  const isAssigned = (row) => !!(row.assigned_to_name || row.assigned_to_email);
  const rowKey = (row) => row.message_id || row.row_id || row.entry_id || "";

  function loadView() {
    try {
      const v = JSON.parse(sessionStorage.getItem(VIEW_KEY) || "{}");
      if (TABS.some((t) => t.key === v.tab)) state.tab = v.tab;
      if (v.filters && typeof v.filters === "object") Object.assign(state.filters, v.filters);
    } catch (e) { /* private window: the page still works */ }
  }
  function saveView() {
    try { sessionStorage.setItem(VIEW_KEY, JSON.stringify({ tab: state.tab, filters: state.filters })); } catch (e) { /* ignore */ }
  }

  function ensureStyles() {
    if (document.querySelector('link[href*="ops-email.css"]')) return Promise.resolve();
    const href = SELF ? new URL("ops-email.css", SELF).href : "/static/ops-email.css";
    return new Promise((done) => {
      const link = h("link", { rel: "stylesheet", href });
      link.addEventListener("load", done);
      link.addEventListener("error", done);
      setTimeout(done, 2000);
      document.head.append(link);
    });
  }

  const stats = () => (state.data && state.data.stats) || {};
  const rows = () => (state.data && state.data.rows) || [];
  const teams = () => (state.options && state.options.teams) || (state.data && state.data.teams) || [];
  const peopleByTeam = () => {
    const out = new Map();
    for (const p of (state.options && state.options.people) || []) {
      const key = p.team || "—";
      if (!out.has(key)) out.set(key, []);
      out.get(key).push(p);
    }
    return out;
  };

  // ------------------------------------------------------------------ actions (confirm -> API -> toast -> refetch)
  async function afterAction(msg) {
    toast(msg);
    try { await loader.refresh(); } catch (e) { /* the poll will catch up */ }
  }

  async function assignDialog(row) {
    const byTeam = peopleByTeam();
    const select = h("select", { "aria-label": "Assign to", required: true },
      h("option", { value: "" }, "Choose a person…"),
      [...byTeam.entries()].map(([team, people]) => h("optgroup", { label: team },
        people.map((p) => h("option", { value: p.email }, `${p.name || p.email}${p.email ? ` · ${p.email}` : ""}`)))));
    if (isAssigned(row) && row.assigned_to_email) select.value = row.assigned_to_email;
    const note = h("textarea", { rows: "3", maxlength: "300", "aria-label": "Note (optional)", placeholder: "Optional note for the assignee", dir: "auto" });
    note.value = row.assigned_note || "";
    const people = (state.options && state.options.people) || [];
    await OPS.modal({
      title: "Assign this e-mail", submitLabel: "Assign",
      body: h("div", {},
        h("p", { class: "muted", dir: "auto" }, bidi(row.subject || "(no subject)")),
        h("label", { class: "em-field" }, h("span", {}, "Assign to"), select),
        h("label", { class: "em-field" }, h("span", {}, "Note"), note)),
      onSubmit: async () => {
        const address = select.value;
        if (!address) throw new Error("Choose a person to assign to.");
        const match = people.find((p) => p.email === address);
        await api("POST", "/api/ops/email/assign", { message_id: rowKey(row), assigned_to_email: address,
          assigned_to_name: match ? match.name : "", note: note.value.trim() });
        await afterAction("E-mail assigned.");
      },
    });
  }

  async function noActionDialog(row) {
    const ok = await CL.confirmDialog({ title: "Mark as needing no action",
      message: `Mark "${row.subject || "(no subject)"}" as needing no reply? It will stop counting as pending.`,
      confirmLabel: "Mark no action", danger: false });
    if (!ok) return;
    try {
      await api("POST", "/api/ops/email/no-action", { message_id: rowKey(row) });
      await afterAction("Marked as needing no action.");
    } catch (e) { toast(e.message, "error"); }
  }

  async function changeTeamDialog(row) {
    const choices = teams().filter((t) => ROLE === "admin" || canActOnTeam(t) || t === row.team);
    const select = h("select", { "aria-label": "Team", required: true },
      choices.map((t) => h("option", { value: t, selected: t === row.team }, t)));
    await OPS.modal({
      title: "Change the team", submitLabel: "Change team",
      body: h("div", {}, h("p", { class: "muted", dir: "auto" }, bidi(row.subject || "(no subject)")),
        h("label", { class: "em-field" }, h("span", {}, "Team"), select)),
      onSubmit: async () => {
        await api("POST", "/api/ops/email/team", { message_id: rowKey(row), team: select.value });
        await afterAction("Team changed.");
      },
    });
  }

  async function reviewDialog(row) {
    const ratings = (state.options && state.options.ratings) || ["Excellent", "Good", "Needs Improvement"];
    const reasons = (state.options && state.options.quality_reasons) || [];
    const ratingSel = h("select", { "aria-label": "Rating", required: true },
      h("option", { value: "" }, "Choose a rating…"), ratings.map((r) => h("option", { value: r, selected: r === row.quality_rating }, r)));
    const boxes = reasons.map((r) => h("label", { class: "em-check" },
      h("input", { type: "checkbox", value: r, checked: String(row.quality_reasons || "").includes(r) }), r));
    const note = h("textarea", { rows: "3", maxlength: "500", "aria-label": "Note", dir: "auto" });
    note.value = row.quality_note || "";
    await OPS.modal({
      title: "Review the reply quality", submitLabel: "Save review",
      body: h("div", {},
        h("p", { class: "muted", dir: "auto" }, bidi(row.subject || "(no subject)"), row.first_reply_by ? ` · replied by ${row.first_reply_by}` : ""),
        h("label", { class: "em-field" }, h("span", {}, "Rating"), ratingSel),
        reasons.length ? h("div", { class: "em-field" }, h("span", {}, "Reasons"), h("div", { class: "em-reasons" }, boxes)) : null,
        h("label", { class: "em-field" }, h("span", {}, "Note"), note)),
      onSubmit: async () => {
        if (!ratingSel.value) throw new Error("Choose a rating.");
        const picked = boxes.filter((b) => b.querySelector("input").checked).map((b) => b.querySelector("input").value);
        await api("POST", "/api/ops/email/quality", { message_id: rowKey(row), rating: ratingSel.value, note: note.value.trim(), reasons: picked });
        await afterAction("Quality review saved.");
      },
    });
  }

  async function ruleDialog(hintKeyword) {
    const choices = teams().filter((t) => t !== "Unclassified" && (ROLE === "admin" || canActOnTeam(t)));
    const keyword = h("input", { type: "text", maxlength: "80", required: true, "aria-label": "Keyword or application", dir: "auto" });
    keyword.value = hintKeyword || "";
    const teamSel = h("select", { "aria-label": "Team", required: true },
      h("option", { value: "" }, "Choose a team…"), choices.map((t) => h("option", { value: t }, t)));
    await OPS.modal({
      title: "Create a classification rule", submitLabel: "Create rule",
      body: h("div", {},
        h("p", { class: "muted" }, "E-mails whose detected words contain this keyword will be put in the chosen team from the next scan on."),
        h("label", { class: "em-field" }, h("span", {}, "Keyword / application"), keyword),
        h("label", { class: "em-field" }, h("span", {}, "Team"), teamSel)),
      onSubmit: async () => {
        if (!keyword.value.trim()) throw new Error("Enter a keyword.");
        if (!teamSel.value) throw new Error("Choose a team.");
        await api("POST", "/api/ops/email/classification/rule", { keyword: keyword.value.trim(), team: teamSel.value });
        await afterAction("Rule created - it applies from the next scan.");
      },
    });
  }

  async function removeRule(keyword) {
    const ok = await CL.confirmDialog({ title: "Remove the rule", message: `Remove the rule for "${keyword}"?`, confirmLabel: "Remove" });
    if (!ok) return;
    try {
      await api("DELETE", `/api/ops/email/classification/rule?keyword=${encodeURIComponent(keyword)}`);
      await afterAction("Rule removed.");
    } catch (e) { toast(e.message, "error"); }
  }

  function rowActionButtons(row) {
    const btns = [];
    if (canActOnTeam(row.team)) {
      if (row.status !== "Replied") {
        btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); assignDialog(row); } }, "Assign"));
        btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); noActionDialog(row); } }, "No action"));
      }
      btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); changeTeamDialog(row); } }, "Team"));
    }
    return h("div", { class: "op-row-actions" }, btns.length ? btns : h("span", { class: "hint" }, "—"));
  }

  // ------------------------------------------------------------------ row drawer
  function detailDrawer(row) {
    const field = (label, value) => (value == null || value === "" ? null
      : [h("dt", {}, label), h("dd", { dir: "auto" }, bidi(String(value)))]);
    const body = h("div", {},
      h("dl", { class: "em-drawer-grid" },
        field("Subject", row.subject), field("Customer", row.customer_name), field("Team", row.team),
        field("Status", row.status), field("SLA", row.sla_status), field("Received", OPS.fmtWhen(row.received_time, state.meta && state.meta.server_time)),
        field("Waiting", row.current_waiting_time), field("First reply by", row.first_reply_by),
        field("First reply", OPS.fmtWhen(row.first_reply_time, state.meta && state.meta.server_time)),
        field("Response", row.response_minutes != null ? OPS.fmtDuration(row.response_minutes) : null),
        field("Detected systems", row.detected_systems), field("Match method", row.match_method),
        field("Assigned to", row.assigned_to_name), field("Quality", row.quality_rating),
        field("No-action reason", row.no_action_reason)),
      row.body_preview ? h("div", {}, h("div", { class: "hint" }, "Message preview"), h("div", { class: "em-preview", dir: "auto" }, row.body_preview)) : null,
      h("div", { class: "em-drawer-actions" }, rowActionButtons(row)));
    OPS.modal({ title: "E-mail details", wide: true, body });
  }

  // ------------------------------------------------------------------ Overview tab
  function kpiTiles() {
    const s = stats();
    const n = (v) => fmtNum(v || 0);
    const tile = (label, value, sub, tone, icon) => OPS.kpi({ label, value, sub, tone, icon });
    el.kpis.replaceChildren(
      tile("Business e-mails", n(s.business_emails), "to the teams today", null, "mail"),
      tile("Replied", n(s.replied), OPS.fmtPct(s.reply_rate) + " reply rate", s.replied ? "ok" : null, "check"),
      tile("Pending", n(s.pending), "need a reply", s.pending ? "warn" : null, "clock"),
      tile("No action", n(s.no_action_needed), "marked no reply", null, "dash"),
      tile("Past SLA", n(s.sla_breached), OPS.fmtPct(s.sla_compliance_rate) + " on time", s.sla_breached ? "danger" : "ok", "alert"),
      tile("Escalated", n(s.escalated_count), "still no reply", s.escalated_count ? "danger" : null, "alert"),
      tile("Carry-over", n(s.carryover_count), s.carryover_count ? `oldest ${s.carryover_oldest_days} d` : "none", s.carryover_count ? "warn" : null, "clock"),
      tile("Assigned", n(s.assigned_count), "to a person", null, "user"),
      tile("Avg first reply", OPS.fmtDuration(s.average_response), s.fastest_response ? `fastest ${OPS.fmtDuration(s.fastest_response)}` : "", null, "activity"));
  }

  function statusDonut() {
    const s = stats();
    return OPS.donut([
      { label: "Replied", value: s.replied || 0, cls: "op-t-ok" },
      { label: "Pending", value: s.pending || 0, cls: "op-t-warn" },
      { label: "No action", value: s.no_action_needed || 0, cls: "op-t-na" },
    ], { size: 150, thickness: 18, centerValue: OPS.fmtPct(s.reply_rate).replace(/\.\d+/, ""), centerLabel: "replied" });
  }

  function teamTable() {
    const ts = stats().team_stats || {};
    const data = Object.entries(ts).map(([team, t]) => ({
      team, business_emails: t.business_emails || 0, replied: t.replied || 0, pending: t.pending || 0,
      no_action: t.no_action_needed || 0, escalated: t.escalated_count || 0, carryover: t.carryover_count || 0,
      breached: t.sla_breached_count || 0, sent: t.team_emails_sent || 0,
      avg: t.response_count ? Math.round((t.response_minutes_sum || 0) / t.response_count) : null,
    }));
    return OPS.table({
      search: false, copy: true, caption: "E-mail by team", pageSize: 25, pageSizes: [25, 50],
      sort: { key: "pending", desc: true },
      columns: [
        { key: "team", label: "Team", sort: true, render: (r) => bidi(r.team) },
        { key: "business_emails", label: "E-mails", num: true, sort: true },
        { key: "replied", label: "Replied", num: true, sort: true },
        { key: "pending", label: "Pending", num: true, sort: true },
        { key: "no_action", label: "No action", num: true, sort: true },
        { key: "escalated", label: "Escalated", num: true, sort: true, render: (r) => (r.escalated ? h("strong", { class: "op-t-danger" }, fmtNum(r.escalated)) : "0") },
        { key: "breached", label: "Past SLA", num: true, sort: true },
        { key: "carryover", label: "Carry-over", num: true, sort: true },
        { key: "avg", label: "Avg reply", num: true, sort: true, render: (r) => OPS.fmtDuration(r.avg) },
        { key: "sent", label: "Replies sent", num: true, sort: true },
      ],
      rows: data,
      empty: { title: "No team data yet", icon: "mail", text: "Teams appear after the first scan." },
    });
  }

  function arrivalsSparkline() {
    const buckets = new Array(24).fill(0);
    for (const r of rows()) {
      const m = /T(\d\d):/.exec(String(r.received_time || ""));
      if (m) buckets[+m[1]] += 1;
    }
    const first = buckets.findIndex((n) => n > 0), last = 23 - [...buckets].reverse().findIndex((n) => n > 0);
    if (first < 0) return h("p", { class: "hint" }, "No arrival times in this scan.");
    const series = buckets.slice(first, last + 1);
    return h("div", {}, OPS.sparkline(series, { width: 260, height: 44, label: "E-mails per hour today" }),
      h("span", { class: "hint" }, `per hour, ${String(first).padStart(2, "0")}:00–${String(last).padStart(2, "0")}:00`));
  }

  function topApps() {
    const vol = stats().system_volume || {};
    const items = Object.entries(vol).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([label, value]) => ({ label, value }));
    if (!items.length) return h("p", { class: "hint" }, "No application was detected in this scan.");
    return OPS.hbars(items, { max: Math.max(1, ...items.map((i) => i.value)) });
  }

  function slowestList() {
    const items = (stats().top_slowest_replies || []).slice(0, 6);
    if (!items.length) return h("p", { class: "hint" }, "No replies to rank yet.");
    return h("ul", { class: "op-list" }, items.map((i) => h("li", {},
      h("span", { dir: "auto" }, bidi(i.subject || "(no subject)")),
      h("span", { class: "hint" }, ` ${i.team || ""} · ${OPS.fmtDuration(i.response_minutes)}${i.first_reply_by ? " · " + i.first_reply_by : ""}`))));
  }

  function escalatedList() {
    const items = (stats().escalated_items || []).slice(0, 8);
    if (!items.length) return OPS.emptyState({ title: "Nothing escalated", icon: "check", text: "No e-mail is waiting past escalation." });
    return h("ul", { class: "op-list" }, items.map((i) => h("li", {},
      OPS.icon("alert"), h("span", { dir: "auto" }, bidi(i.subject || "(no subject)")),
      h("span", { class: "hint" }, ` ${i.team || ""} · waiting ${i.current_waiting_time || ""}`))));
  }

  function renderOverview() {
    kpiTiles();
    const matched = stats();
    el.panels.overview.replaceChildren(
      el.kpis,
      h("div", { class: "em-two" },
        OPS.card("Status", {}, h("div", { class: "op-viz op-viz-donut" }, statusDonut(),
          h("div", { class: "op-legend op-legend-col" },
            h("span", { class: "op-legend-item" }, h("i", { class: "op-swatch op-t-ok" }), `${fmtNum(matched.replied || 0)} replied`),
            h("span", { class: "op-legend-item" }, h("i", { class: "op-swatch op-t-warn" }), `${fmtNum(matched.pending || 0)} pending`),
            h("span", { class: "op-legend-item" }, h("i", { class: "op-swatch op-t-na" }), `${fmtNum(matched.no_action_needed || 0)} no action`)))),
        OPS.card("E-mails by team", {}, teamTable().el)),
      h("div", { class: "em-two" },
        OPS.card("Arrivals", { hint: "when e-mails came in today" }, arrivalsSparkline()),
        OPS.card("Top applications", {}, topApps())),
      h("div", { class: "em-two" },
        OPS.card("Slowest first replies", {}, slowestList()),
        OPS.card("Escalated (no reply)", {}, escalatedList())),
      matchDiagnostic());
  }

  function matchDiagnostic() {
    const s = stats();
    const parts = [["ConversationID", s.conversation_matches], ["Index", s.conversation_index_matches],
      ["Subject", s.subject_matches], ["No match", s.no_matches]].filter(([, n]) => n);
    if (!parts.length) return null;
    return OPS.card("How replies were matched", { hint: "diagnostic" },
      h("p", { class: "hint" }, parts.map(([label, n]) => `${label}: ${fmtNum(n)}`).join(" · ")));
  }

  // ------------------------------------------------------------------ Pending / Replied filters
  function pendingFilter() {
    const f = state.filters;
    return (r) => {
      if (f.team && !(normAr(r.team) === normAr(f.team) || (r.all_teams || []).some((t) => normAr(t) === normAr(f.team)))) return false;
      if (f.system && !systemsOf(r).some((sys) => normAr(sys) === normAr(f.system))) return false;
      if (f.carryover && !r.is_carryover) return false;
      if (f.escalated && !r.escalated) return false;
      if (f.assigned === "yes" && !isAssigned(r)) return false;
      if (f.assigned === "no" && isAssigned(r)) return false;
      return true;
    };
  }

  function filterBar(onChange) {
    const f = state.filters;
    const teamSel = h("select", { "aria-label": "Team", onchange: () => { f.team = teamSel.value; onChange(); } },
      h("option", { value: "" }, "All teams"), teams().map((t) => h("option", { value: t, selected: t === f.team }, t)));
    const sysSet = new Set();
    for (const r of rows()) systemsOf(r).forEach((s) => sysSet.add(s));
    const sysSel = h("select", { "aria-label": "Application", onchange: () => { f.system = sysSel.value; onChange(); } },
      h("option", { value: "" }, "All applications"), [...sysSet].sort().map((s) => h("option", { value: s, selected: s === f.system }, s)));
    const assignedSel = h("select", { "aria-label": "Assignment", onchange: () => { f.assigned = assignedSel.value; onChange(); } },
      h("option", { value: "", selected: !f.assigned }, "Any assignment"),
      h("option", { value: "yes", selected: f.assigned === "yes" }, "Assigned"),
      h("option", { value: "no", selected: f.assigned === "no" }, "Unassigned"));
    const carry = h("input", { type: "checkbox", checked: f.carryover, onchange: () => { f.carryover = carry.checked; onChange(); } });
    const esc = h("input", { type: "checkbox", checked: f.escalated, onchange: () => { f.escalated = esc.checked; onChange(); } });
    return h("div", { class: "em-filters" }, teamSel, sysSel, assignedSel,
      h("label", { class: "em-check" }, carry, "Carry-over only"),
      h("label", { class: "em-check" }, esc, "Escalated only"));
  }

  function pendingTable() {
    const now = state.meta && state.meta.server_time;
    return OPS.table({
      search: true, searchPlaceholder: "Search subject, customer, system…", copy: true,
      caption: "Pending e-mails", pageSize: 25, rowKey, onRowClick: detailDrawer,
      sort: { key: "received", desc: false },
      toolbar: filterBar(() => { tables.pending.setFilter(pendingFilter()); saveView(); }),
      filter: pendingFilter(),
      columns: [
        { key: "status", label: "SLA", render: (r) => OPS.statusChip(r.escalated ? "Breached" : r.sla_status || r.status), text: (r) => r.sla_status || r.status },
        { key: "subject", label: "Subject", render: (r) => bidi(r.subject || "(no subject)"), text: (r) => r.subject },
        { key: "customer_name", label: "Customer", render: (r) => bidi(r.customer_name || "—"), text: (r) => r.customer_name },
        { key: "team", label: "Team", render: (r) => bidi(r.team), text: (r) => r.team },
        { key: "received", label: "Waiting", sort: (r) => OPS.parseNaive(r.received_time) || 0, render: (r) => r.current_waiting_time || OPS.fmtWhen(r.received_time, now), text: (r) => r.current_waiting_time },
        { key: "assigned_to_name", label: "Assigned", render: (r) => (r.assigned_to_name ? bidi(r.assigned_to_name) : h("span", { class: "hint" }, "—")), text: (r) => r.assigned_to_name },
        { key: "actions", label: "", render: rowActionButtons },
      ],
      rows: rows().filter((r) => r.status === "Pending Reply"),
      empty: (filtered) => ({ title: filtered ? "No pending e-mail matches" : "Nothing pending", icon: "check",
        text: filtered ? "Try clearing the filters." : "Every business e-mail has a reply or is marked no-action." }),
    });
  }

  function renderPending() {
    if (!tables.pending) {
      tables.pending = pendingTable();
      el.panels.pending.replaceChildren(OPS.card("Pending e-mails", { hint: "click a row for details and actions" }, tables.pending.el));
    } else {
      tables.pending.setRows(rows().filter((r) => r.status === "Pending Reply"));
    }
  }

  function repliedTable() {
    const now = state.meta && state.meta.server_time;
    const canReview = ROLE === "admin" || ROLE === "manager";
    return OPS.table({
      search: true, searchPlaceholder: "Search subject, customer, replier…", copy: true,
      caption: "Replied e-mails", pageSize: 25, rowKey, onRowClick: detailDrawer,
      sort: { key: "response_minutes", desc: true },
      columns: [
        { key: "subject", label: "Subject", render: (r) => bidi(r.subject || "(no subject)"), text: (r) => r.subject },
        { key: "customer_name", label: "Customer", render: (r) => bidi(r.customer_name || "—"), text: (r) => r.customer_name },
        { key: "team", label: "Team", render: (r) => bidi(r.team), text: (r) => r.team },
        { key: "first_reply_by", label: "Replied by", render: (r) => bidi(r.first_reply_by || "—"), text: (r) => r.first_reply_by },
        { key: "response_minutes", label: "Response", num: true, sort: true, render: (r) => OPS.fmtDuration(r.response_minutes) },
        { key: "sla_status", label: "SLA", render: (r) => OPS.statusChip(r.sla_status), text: (r) => r.sla_status },
        { key: "quality_rating", label: "Quality", render: (r) => (r.quality_rating ? OPS.chip(r.quality_rating, r.quality_rating === "Needs Improvement" ? "warn" : "ok") : h("span", { class: "hint" }, "—")), text: (r) => r.quality_rating },
        { key: "actions", label: "", render: (r) => (canReview && !r.reply_by_manager && canActOnTeam(r.team)
          ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); reviewDialog(r); } }, r.quality_rating ? "Re-review" : "Review")
          : h("span", { class: "hint" }, r.reply_by_manager ? "manager reply" : "—")) },
      ],
      rows: rows().filter((r) => r.status === "Replied"),
      empty: (filtered) => ({ title: filtered ? "No replied e-mail matches" : "No replies yet", icon: "mail",
        text: filtered ? "Try clearing the search." : "Replies appear here as the teams answer." }),
    });
  }

  function renderReplied() {
    if (!tables.replied) {
      tables.replied = repliedTable();
      el.panels.replied.replaceChildren(OPS.card("Replied e-mails", { hint: "managers can review the reply quality" }, tables.replied.el));
    } else {
      tables.replied.setRows(rows().filter((r) => r.status === "Replied"));
    }
  }

  // ------------------------------------------------------------------ Quality tab
  function renderQuality() {
    const q = (state.data && state.data.quality) || {};
    const sq = stats().quality || {};
    const kpis = h("div", { class: "em-kpis" },
      OPS.kpi({ label: "Reviewed", value: fmtNum(sq.reviewed || 0), sub: OPS.fmtPct(sq.coverage_rate) + " coverage", icon: "check" }),
      OPS.kpi({ label: "Excellent", value: fmtNum(sq.excellent || 0), tone: "ok", icon: "check" }),
      OPS.kpi({ label: "Good", value: fmtNum(sq.good || 0), icon: "check" }),
      OPS.kpi({ label: "Needs improvement", value: fmtNum(sq.needs_improvement || 0), tone: sq.needs_improvement ? "warn" : null, icon: "alert" }),
      OPS.kpi({ label: "Appeals", value: fmtNum(sq.appeals || 0), icon: "info" }));
    const scorecard = Array.isArray(q.scorecard) ? q.scorecard : [];
    let board;
    if (!scorecard.length) {
      board = OPS.emptyState({ title: "No quality reviews yet", icon: "activity",
        text: "Once replies are reviewed, a per-person scorecard, team index and leaderboard appear here." });
    } else {
      board = OPS.table({
        search: true, copy: true, caption: "Quality scorecard", pageSize: 25,
        sort: { key: "reviewed", desc: true },
        columns: [
          { key: "name", label: "Person", render: (r) => bidi(r.name || "—"), text: (r) => r.name },
          { key: "team", label: "Team", render: (r) => bidi(r.team || "—"), text: (r) => r.team },
          { key: "reviewed", label: "Reviewed", num: true, sort: true },
          { key: "excellent", label: "Excellent", num: true, sort: true },
          { key: "good", label: "Good", num: true, sort: true },
          { key: "needs_improvement", label: "Needs improvement", num: true, sort: true },
        ],
        rows: scorecard,
      }).el;
    }
    el.panels.quality.replaceChildren(kpis, OPS.card("Quality", { hint: "from the daily reviews" }, board));
  }

  // ------------------------------------------------------------------ Trends tab
  async function ensureHistory() {
    if (state.historyLoaded) return;
    state.historyLoaded = true;
    try {
      const answer = await api("GET", "/api/ops/email/history?days=14");
      state.history = answer.days || [];
    } catch (e) { state.history = null; }
  }
  function trendChart(label, key, tone) {
    const days = state.history || [];
    const series = days.map((d) => (d.final ? d.final[key] : (d[key] || 0)) || 0);
    return OPS.card(label, {}, series.length
      ? h("div", {}, OPS.sparkline(series, { width: 260, height: 50, label, tone }),
        h("p", { class: "hint" }, `${days.length} day${days.length === 1 ? "" : "s"}, latest ${fmtNum(series[series.length - 1] || 0)}`))
      : h("p", { class: "hint" }, "No history yet."));
  }
  async function renderTrends() {
    el.panels.trends.replaceChildren(h("p", { class: "empty" }, "Loading the history…"));
    await ensureHistory();
    if (state.history === null) {
      el.panels.trends.replaceChildren(OPS.emptyState({ title: "History unavailable", icon: "alert", text: "The daily history could not be read." }));
      return;
    }
    el.panels.trends.replaceChildren(h("div", { class: "em-trend-grid" },
      trendChart("Replied per day", "replied", "op-t-ok"),
      trendChart("Pending per day", "pending", "op-t-warn"),
      trendChart("Past SLA per day", "sla_breached", "op-t-danger")));
  }

  // ------------------------------------------------------------------ Unclassified tab
  function unclassifiedRows() {
    return rows().filter((r) => normAr(r.team) === normAr("Unclassified"));
  }
  function renderUnclassified() {
    const list = unclassifiedRows();
    const ruleMap = (state.options && state.options.system_team_map) || {};
    const ruleEntries = Object.entries(ruleMap);
    const unclTeam = (state.options && state.options.unclassified_team) || "";

    const table = list.length ? OPS.table({
      search: true, copy: true, caption: "Unclassified e-mails", pageSize: 25, rowKey, onRowClick: detailDrawer,
      columns: [
        { key: "subject", label: "Subject", render: (r) => bidi(r.subject || "(no subject)"), text: (r) => r.subject },
        { key: "customer_name", label: "Customer", render: (r) => bidi(r.customer_name || "—"), text: (r) => r.customer_name },
        { key: "detected_systems", label: "Detected words", render: (r) => bidi(r.classification_hint || r.detected_systems || "—"), text: (r) => r.detected_systems },
        { key: "mailbox", label: "Sent to", render: (r) => bidi(r.mailbox || "—"), text: (r) => r.mailbox },
        { key: "actions", label: "", render: (r) => h("div", { class: "op-row-actions" },
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); changeTeamDialog(r); } }, "Assign team"),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); ruleDialog(systemsOf(r)[0] || ""); } }, "Create rule")) },
      ],
      rows: list,
    }).el : OPS.emptyState({ title: "Nothing unclassified", icon: "check",
      text: "Every e-mail could be put in a team. New ones without a team appear here." });

    const rulesBox = h("div", {},
      h("div", { class: "card-head" }, h("h3", {}, "Classification rules"),
        h("span", { class: "grow" }), h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => ruleDialog("") }, OPS.icon("plus"), "New rule")),
      ruleEntries.length
        ? h("ul", { class: "em-rules" }, ruleEntries.sort((a, b) => a[0].localeCompare(b[0])).map(([kw, team]) => h("li", { class: "em-rule" },
          h("span", { dir: "auto" }, h("code", {}, kw), " → ", bidi(team)),
          h("span", { class: "grow" }),
          canActOnTeam(team) ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => removeRule(kw) }, OPS.icon("trash"), "Remove") : null)))
        : h("p", { class: "hint" }, "No custom keyword rules yet."),
      h("p", { class: "hint" }, unclTeam
        ? `E-mails with no detected team currently go to "${unclTeam}" (change it in System › Settings › Classification).`
        : "Where e-mails with no detected team go is set in System › Settings › Classification."));

    el.panels.unclassified.replaceChildren(
      OPS.card("Unclassified e-mails", { hint: "assign a team or create a rule" }, table),
      OPS.card("", {}, rulesBox));
  }

  // ------------------------------------------------------------------ tabs
  function badges() {
    const pending = rows().filter((r) => r.status === "Pending Reply").length;
    const uncl = unclassifiedRows().length;
    setBadge("pending", pending, "warn");
    setBadge("unclassified", uncl, "info");
  }
  function setBadge(key, n, tone) {
    const btn = el.tabEls[key];
    if (!btn) return;
    let badge = btn.querySelector(".em-badge");
    if (!n) { if (badge) badge.remove(); return; }
    if (!badge) { badge = h("span", { class: "em-badge" }); btn.append(badge); }
    badge.className = "em-badge " + (tone || "");
    badge.textContent = fmtNum(n);
  }

  function showTab(key) {
    state.tab = key;
    saveView();
    for (const t of TABS) {
      el.tabEls[t.key].setAttribute("aria-selected", String(t.key === key));
      el.panels[t.key].hidden = t.key !== key;
    }
    renderTab(key);
  }

  function renderTab(key) {
    if (!state.data) return;
    if (key === "overview") renderOverview();
    else if (key === "pending") renderPending();
    else if (key === "replied") renderReplied();
    else if (key === "quality") renderQuality();
    else if (key === "trends") renderTrends();
    else if (key === "unclassified") renderUnclassified();
  }

  // ------------------------------------------------------------------ run scan now
  async function runNow() {
    if (state.waiting) return;
    state.waiting = { since: Date.now(), generated: state.generatedAt };
    updateRunUI();
    try {
      await api("POST", "/api/ops/jobs/email/run", {});
    } catch (e) {
      state.waiting = null; updateRunUI(); toast(e.message, "error"); return;
    }
    state.waiting.timer = setInterval(tickRun, 2000);
  }
  function tickRun() {
    const w = state.waiting;
    if (!w) return;
    if (state.generatedAt && state.generatedAt !== w.generated) { endRun(`New scan at ${OPS.fmtClock(state.generatedAt)}.`); return; }
    if (Date.now() - w.since > WAIT_LIMIT_MS) { endRun("The collector has not delivered a scan yet - it stays queued.", "error"); return; }
    loader.refresh().catch(() => {});
    updateRunUI();
  }
  function endRun(msg, kind) {
    if (state.waiting) clearInterval(state.waiting.timer);
    state.waiting = null;
    updateRunUI();
    if (msg) toast(msg, kind);
  }
  function updateRunUI() {
    el.runBtn.disabled = !!state.waiting;
    el.runBtn.replaceChildren(OPS.icon("refresh"), state.waiting ? "Scanning…" : "Run scan now");
  }

  // ------------------------------------------------------------------ data in
  function onData(answer) {
    state.answer = answer;
    state.data = answer.data || null;
    state.options = answer.options || state.options;
    state.generatedAt = answer.generated_at || "";
    el.scanInfo.textContent = answer.generated_at
      ? `Scan of ${String(answer.generated_at).replace("T", " ").slice(0, 16)}${answer.duration_seconds ? ` · ${Math.round(answer.duration_seconds)} s` : ""}` : "";
    el.empty.hidden = !!state.data;
    el.content.hidden = !state.data;
    el.exportLink.hidden = !state.data;
    if (!state.data) return;
    badges();
    renderTab(state.tab);
    if (state.waiting) tickRun();
  }
  function onMeta(meta) {
    if (!meta) return;
    state.meta = meta;
    el.banner.update(meta);
  }
  function onError(e) {
    if (!state.data) el.banner.update({ problem: e.network ? null : e.message, module: "email" });
    if (!e.network && !state.data) toast(e.message, "error");
  }

  // ------------------------------------------------------------------ shell
  function build() {
    el.scanInfo = h("span", { class: "hint" });
    el.runBtn = h("button", { type: "button", class: "btn btn-primary", onclick: runNow }, OPS.icon("refresh"), "Run scan now");
    el.exportLink = h("a", { class: "btn btn-ghost", href: "/export/ops/email.xlsx", download: "", hidden: true }, OPS.icon("download"), "Export Excel");
    const fb = OPS.freshnessBanner();
    el.banner = fb;
    el.kpis = h("div", { class: "em-kpis" });

    el.tabEls = {};
    el.panels = {};
    const tabbar = h("div", { class: "em-tabs", role: "tablist", "aria-label": "E-mail views" });
    for (const t of TABS) {
      const btn = h("button", { type: "button", class: "em-tab", role: "tab", id: `em-tab-${t.key}`,
        "aria-selected": String(t.key === state.tab), "aria-controls": `em-panel-${t.key}`, onclick: () => showTab(t.key) }, t.label);
      el.tabEls[t.key] = btn;
      tabbar.append(btn);
      el.panels[t.key] = h("div", { class: "em-panel", role: "tabpanel", id: `em-panel-${t.key}`, "aria-labelledby": `em-tab-${t.key}`, hidden: t.key !== state.tab });
    }
    el.content = h("div", { hidden: true }, tabbar, ...TABS.map((t) => el.panels[t.key]));
    el.empty = OPS.emptyState({ title: "No scan yet", icon: "mail",
      text: "The collector writes the first e-mail scan here as soon as it has run. You can ask for one with the button above." });

    page.replaceChildren(
      h("div", { class: "op-titlebar" },
        h("div", {}, h("h1", {}, "E-mail"), el.scanInfo),
        h("span", { class: "grow" }), el.runBtn, el.exportLink),
      fb.el, el.content, el.empty);
  }

  const loader = OPS.loader({ url: "/api/ops/email", interval: 15000, onData, onUnchanged: () => { if (state.waiting) tickRun(); }, onMeta, onError });

  async function start() {
    await ensureStyles();
    loadView();
    build();
    loader.start();
    document.addEventListener("visibilitychange", () => { /* OPS.loader handles pause/resume */ });
  }
  start();
})();
