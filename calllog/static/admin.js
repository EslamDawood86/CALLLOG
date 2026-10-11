"use strict";
/* Administrator dashboard. One shared date range scopes every tab; the open tab re-fetches every 5 s. */
(function () {
  const { h, $, api, toast, modal, confirmDialog, icon, editRecord, deleteRecord, rowActions, statusChip, trimHours,
    legend, chartWithToggle, bidi, reasonBars, barRows, fmtNum, hhmm, fmtDay, addDays, pct, agoText, STATUSES, STATUS_NAMES } = CL;

  const TABS = ["overview", "records", "employees", "branches", "audit", "export"];
  const REFRESH_MS = 5000;
  const state = {
    reasons: [], reasonByKey: {}, branches: [], today: "", tab: "overview", preset: "today", from: "", to: "",
    rec: { page: 1 }, audit: { page: 1 }, sort: { key: "total", dir: -1 }, users: [], seen: {}, busy: false,
  };
  const AUDIT_LABELS = {
    login: "Signed in", logout: "Signed out", login_failed: "Failed sign-in", login_blocked: "Sign-in blocked (too many attempts)",
    password_changed: "Changed own password", record_created: "Logged a call", record_updated: "Edited a call",
    record_deleted: "Deleted a call", user_created: "Created user", user_updated: "Updated user",
    password_reset: "Reset a password", users_imported: "Imported users", branches_imported: "Imported the branch list", export: "Exported data",
  };
  const panel = (name) => $(`#tab-${name}`);

  // ------------------------------------------------------------- utilities
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const qs = (obj) => new URLSearchParams(Object.entries(obj).filter(([, v]) => v !== "" && v != null)).toString();
  const rangeQS = () => qs({ from: state.from, to: state.to });
  const reasonLabel = (key) => (state.reasonByKey[key] ? state.reasonByKey[key].label_ar : key);

  function setLive(ok, text) {
    $("#live").className = "live " + (ok ? "ok" : "bad");
    $("#liveText").textContent = text;
  }

  /** Skip re-rendering when nothing visible changed (keeps hover/tooltips/scroll steady between polls). */
  function changed(tab, data) {
    const key = JSON.stringify(data, (k, v) => (k === "server_time" ? v.slice(0, 16) : v));
    if (state.seen[tab] === key) return false;
    state.seen[tab] = key;
    return true;
  }

  function pager(data, onPage) {
    if (data.pages <= 1) return h("div", { class: "pager" }, `${fmtNum(data.total)} total`);
    return h("div", { class: "pager" }, `${fmtNum(data.total)} total · page ${data.page} of ${data.pages}`,
      h("button", { class: "btn btn-ghost btn-sm", type: "button", disabled: data.page <= 1, onclick: () => onPage(data.page - 1) }, "Previous"),
      h("button", { class: "btn btn-ghost btn-sm", type: "button", disabled: data.page >= data.pages, onclick: () => onPage(data.page + 1) }, "Next"));
  }

  function dlLink(label, href, kind) {
    return h("a", { class: "btn btn-ghost btn-sm", href, download: "" }, icon("download"), label || kind);
  }

  // ------------------------------------------------------------- date range
  function applyPreset(p) {
    state.preset = p;
    const t = state.today;
    if (p === "today") [state.from, state.to] = [t, t];
    else if (p === "yesterday") [state.from, state.to] = [addDays(t, -1), addDays(t, -1)];
    else if (p === "week") [state.from, state.to] = [addDays(t, -6), t];
    else if (p === "month") [state.from, state.to] = [t.slice(0, 8) + "01", t];
    syncFilters();
  }
  function syncFilters() {
    $("#fFrom").value = state.from;
    $("#fTo").value = state.to;
    document.querySelectorAll("#presets button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.preset === state.preset)));
  }
  function onRangeInput() {
    const from = $("#fFrom").value, to = $("#fTo").value;
    if (!from || !to) return;
    if (from > to) { toast("'From' must not be after 'To'.", "error"); syncFilters(); return; }
    [state.from, state.to, state.preset] = [from, to, "custom"];
    state.rec.page = state.audit.page = 1;
    syncFilters();
    refresh(true);
  }

  // ------------------------------------------------------------------ tabs
  function showTab(name) {
    if (!TABS.includes(name)) name = "overview";
    state.tab = name;
    if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    document.querySelectorAll("#tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    TABS.forEach((t) => (panel(t).hidden = t !== name));
    $("#filters").hidden = name === "employees" || name === "branches";
    document.querySelectorAll(".filter-group").forEach((g) => (g.hidden = g.dataset.for !== name));
    refresh(true);
  }

  /** How often the open tab re-fetches. Past days cannot change, so they are checked rarely; the share stays quiet. */
  function pollDelay() {
    if (state.tab === "export" || state.tab === "branches") return Infinity;
    if (state.tab === "employees") return 30000;
    if (state.tab !== "audit" && state.to < state.today) return 60000;
    if (state.tab === "audit") return 10000;
    return state.from === state.to ? REFRESH_MS : 15000;
  }

  const loaders = { overview: loadOverview, records: loadRecords, employees: loadEmployees, branches: loadBranches, audit: loadAudit, export: renderExport };

  async function refresh(force) {
    if (state.busy) return;
    state.busy = true;
    const tab = state.tab;
    state.lastRefresh = Date.now();
    try {
      const serverTime = await loaders[tab](!!force);
      if (serverTime) setLive(true, `Live · updated ${serverTime.slice(11, 19)}`);
    } catch (e) {
      setLive(false, e.network ? "Offline · retrying…" : e.message);
      if (!e.network && force) toast(e.message, "error");
    } finally {
      state.busy = false;
    }
  }

  // -------------------------------------------------------------- overview
  function tile(label, value, sub, extra) {
    return h("div", { class: "tile" + (extra ? " " + extra : "") }, h("span", { class: "tile-label" }, label), h("strong", {}, value), h("span", { class: "tile-sub" }, sub || ""));
  }

  async function loadOverview(force) {
    const d = await api("GET", `/api/admin/overview?${rangeQS()}`);
    if (force || changed("overview", d)) renderOverview(d);
    return d.server_time;
  }

  function chartBuckets(d) {
    if (d.chart.kind === "hour") {
      return trimHours(d.chart.buckets).map((b) => {
        const hh = String(b.key).padStart(2, "0");
        return { ...b, label: hh, title: `${hh}:00 – ${hh}:59` };
      });
    }
    const sameMonth = d.range.from.slice(0, 7) === d.range.to.slice(0, 7);
    return d.chart.buckets.map((b) => ({ ...b, label: sameMonth ? b.key.slice(8) : `${b.key.slice(8)}/${b.key.slice(5, 7)}`, title: fmtDay(b.key) }));
  }

  function renderOverview(d) {
    const t = d.totals, single = d.range.from === d.range.to;
    const whenText = single ? fmtDay(d.range.from) : `${fmtDay(d.range.from)} → ${fmtDay(d.range.to)}`;
    const statusTile = (s) => h("div", { class: "tile" },
      h("span", { class: "tile-label" }, h("i", { class: `dot s-${s}` }), STATUS_NAMES[s]), h("strong", {}, fmtNum(t[s])), h("span", { class: "tile-sub" }, pct(t[s], t.total)));
    const heroTile = h("div", { class: "tile hero" }, h("span", { class: "tile-label" }, "Total calls"), h("strong", {}, fmtNum(t.total)), h("span", { class: "tile-sub" }, whenText));

    const chartHost = h("div", { class: "chart" });
    const toggle = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, "View as table");
    const legendHost = h("div", { class: "legend" });
    legend(legendHost);
    const renderChart = chartWithToggle(chartHost, toggle, () => ({ buckets: chartBuckets(d), firstColumn: single ? "Hour" : "Day" }));

    const rows = [...d.employees].sort((a, b) => {
      const { key, dir } = state.sort;
      const av = a[key] ?? "", bv = b[key] ?? "";
      return (typeof av === "number" ? av - bv : String(av).localeCompare(String(bv))) * dir || a.name.localeCompare(b.name);
    });
    const th = (key, label, num) => h("th", { class: "sortable" + (num ? " num" : ""), tabindex: "0", "aria-sort": state.sort.key === key ? (state.sort.dir > 0 ? "ascending" : "descending") : "none",
      onclick: () => sortBy(key, d), onkeydown: (e) => e.key === "Enter" && sortBy(key, d) }, label + (state.sort.key === key ? (state.sort.dir > 0 ? " ▲" : " ▼") : ""));
    const employees = h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, th("name", "Employee"), th("department", "Department"), th("total", "Calls", true), th("resolved", "Resolved", true),
        th("transferred", "Transferred", true), th("ticket_inquiry", "Ticket Inq.", true), th("last_call", "Last call"))),
      h("tbody", {}, rows.map((e) => h("tr", { class: "clickable" + (e.total ? "" : " zero"), tabindex: "0", title: "Show this employee's calls",
        onclick: () => openEmployee(e.user_id), onkeydown: (ev) => ev.key === "Enter" && openEmployee(e.user_id) },
        h("td", {}, bidi(e.name), h("span", { class: "hint" }, `  ${e.user_id}`), e.active ? null : h("span", { class: "pill off" }, "disabled")),
        h("td", {}, bidi(e.department)),
        h("td", { class: "num" }, h("strong", {}, fmtNum(e.total))), h("td", { class: "num" }, fmtNum(e.resolved)),
        h("td", { class: "num" }, fmtNum(e.transferred)), h("td", { class: "num" }, fmtNum(e.ticket_inquiry)),
        h("td", { class: "nowrap" }, e.last_call ? [single ? hhmm(e.last_call.slice(11)) : e.last_call.slice(0, 16),
          h("span", { class: "hint" }, "  " + agoText(d.server_time, e.last_call))] : "—"))))));
    const feed = d.recent.length
      ? h("ul", { class: "feed" }, d.recent.map((r) => h("li", {},
        h("span", { class: "t" }, single ? hhmm(r.time) : `${r.date.slice(5)} ${hhmm(r.time)}`),
        h("span", { class: "who-line" }, bidi(r.name), h("span", { class: "hint" }, `  →  IP ${r.ip_phone}`)),
        h("span", { class: "what" }, statusChip(r.status, r.reason_ar)))))
      : h("p", { class: "empty" }, "No calls yet in this period.");

    panel("overview").replaceChildren(
      h("div", { class: "kpis five" }, heroTile, ...STATUSES.map(statusTile),
        tile("Active employees", `${t.active_employees} / ${t.employees}`, "took at least one call")),
      h("div", { class: "card chart-card" },
        h("div", { class: "card-head" }, h("h2", {}, single ? "Calls by hour" : "Calls by day"), legendHost, toggle), chartHost),
      h("div", { class: "two-col" },
        h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Employees"), h("span", { class: "hint" }, "click a row to see their calls")), employees),
        h("div", { class: "side-stack" },
          h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Calls by reason")), reasonBars(state.reasons, d.by_reason)),
          branchCards(d.branches),
          h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Latest calls"), h("span", { class: "hint" }, "live")), feed))));
    renderChart();
    state.departments = d.departments;
    fillDepartments();
  }

  /** Where the calls came from: regions and the busiest branches. Calls from sectors/others have no branch. */
  function branchCards(b) {
    const regions = b.by_region.length
      ? barRows(b.by_region.map((r) => ({ label: r.region, value: r.total }))) : h("p", { class: "empty" }, "No branch calls in this period.");
    const top = b.top.length ? h("ol", { class: "top-list" }, b.top.map((t) => h("li", {},
      h("span", {}, h("strong", {}, t.code), "  ", bidi(t.name)), h("span", { class: "hint" }, t.region), h("strong", {}, fmtNum(t.total)))))
      : h("p", { class: "empty" }, "No branch calls in this period.");
    const note = b.without_branch ? h("p", { class: "hint" }, `${fmtNum(b.without_branch)} call${b.without_branch === 1 ? "" : "s"} came from sectors or other callers (no branch).`) : null;
    return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Calls by region")), regions,
      h("div", { class: "card-head spaced" }, h("h2", {}, "Top branches"), h("span", { class: "hint" }, `${b.distinct} branch${b.distinct === 1 ? "" : "es"} called`)), top, note);
  }

  function sortBy(key, d) {
    state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : (["name", "department"].includes(key) ? 1 : -1) };
    renderOverview(d);
  }
  function openEmployee(uid) {
    state.rec = { page: 1, user_id: uid };
    $("#fUser").value = uid;
    showTab("records");
  }

  // --------------------------------------------------------------- records
  function recFilters() {
    const f = state.rec;
    return { from: state.from, to: state.to, user_id: f.user_id || "", department: f.department || "", region: f.region || "", reason: f.reason || "", q: f.q || "" };
  }

  async function loadRecords(force) {
    const d = await api("GET", `/api/admin/records?${qs({ ...recFilters(), page: state.rec.page, page_size: 50 })}`);
    state.rec.page = d.page;
    if (force || changed("records", d)) renderRecords(d);
    return d.server_time;
  }

  function renderRecords(d) {
    const single = d.range.from === d.range.to;
    const exportQS = qs(recFilters());
    const table = d.items.length ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", {}, single ? "Time" : "Date / time"), h("th", {}, "Employee"), h("th", {}, "Department"), h("th", {}, "IP phone"), h("th", {}, "Branch"),
        h("th", {}, "Reason"), h("th", {}, "Note"), h("th", { class: "actions-col" }, h("span", { class: "sr-only" }, "Actions")))),
      h("tbody", {}, d.items.map((r) => h("tr", {},
        h("td", { class: "nowrap" }, single ? hhmm(r.time) : `${r.date} ${hhmm(r.time)}`, h("span", { class: "hint" }, r.time.slice(5))),
        h("td", {}, bidi(r.name), h("span", { class: "hint" }, `  ${r.user_id}`)),
        h("td", {}, bidi(r.department)),
        h("td", { class: "ip" }, r.ip_phone),
        h("td", { class: "branch-cell" }, r.branch ? [h("strong", {}, r.branch), bidi(r.branch_name), h("div", { class: "hint" }, r.region)] : h("span", { class: "branch-none" }, "—")),
        h("td", {}, statusChip(r.status, r.reason_ar)),
        h("td", { class: "note" }, bidi(r.note || ""), r.edited ? h("span", { class: "edited-mark", title: `Edited ${r.updated_at} by ${r.updated_by}` }, "edited") : null),
        h("td", { class: "actions-col" }, rowActions(r, () => editAny(r), () => deleteAny(r)))))))) : h("p", { class: "empty" }, "No calls match these filters.");
    panel("records").replaceChildren(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, `${fmtNum(d.total)} call${d.total === 1 ? "" : "s"}`), h("span", { class: "grow" }),
        dlLink("Excel", `/export/records.xlsx?${exportQS}`), dlLink("CSV", `/export/records.csv?${exportQS}`)),
      table, pager(d, (p) => { state.rec.page = p; refresh(true); })));
  }

  async function editAny(rec) {
    if (await editRecord(rec, state.reasons, state.branches)) refresh(true);
  }
  async function deleteAny(rec) {
    if (await deleteRecord(rec)) refresh(true);
  }

  // ------------------------------------------------------------- employees
  async function loadEmployees(force) {
    const d = await api("GET", "/api/admin/users");
    state.users = d.users;
    fillUserFilter();
    if (force || changed("employees", d)) renderEmployees(d.users);
    return new Date().toTimeString().slice(0, 8).padStart(19, " ");
  }

  function renderEmployees(users) {
    const me = window.CL_ME;
    const rows = users.map((u) => h("tr", { class: u.active ? "" : "zero" },
      h("td", { class: "ip" }, u.user_id), h("td", {}, bidi(u.name)), h("td", {}, bidi(u.department)),
      h("td", {}, u.role === "admin" ? h("span", { class: "pill" }, "Admin") : "Employee"),
      h("td", {}, u.active ? h("span", { class: "pill on" }, "Active") : h("span", { class: "pill off" }, "Disabled"),
        u.must_change_password ? h("span", { class: "hint" }, "  must change password") : null),
      h("td", { class: "nowrap" }, u.last_login ? u.last_login.slice(0, 16) : h("span", { class: "hint" }, "never")),
      h("td", { class: "actions-col" }, h("span", { class: "actions" },
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => userForm(u) }, icon("pencil"), "Edit"),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => resetPassword(u) }, icon("key"), "Reset password"),
        u.user_id === (me && me.user_id) ? null : h("button", { class: "btn btn-ghost btn-sm " + (u.active ? "btn-danger-text" : ""), type: "button", onclick: () => toggleActive(u) }, u.active ? "Disable" : "Enable")))));
    panel("employees").replaceChildren(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, `${users.length} accounts`), h("span", { class: "grow" }),
        h("button", { class: "btn btn-ghost", type: "button", onclick: importUsers }, icon("upload"), "Import from Excel / CSV"),
        h("button", { class: "btn btn-primary", type: "button", onclick: () => userForm(null) }, icon("plus"), "Add employee")),
      h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, ["User ID", "Name", "Department", "Role", "Status", "Last sign-in"].map((c) => h("th", {}, c)), h("th", { class: "actions-col" }, h("span", { class: "sr-only" }, "Actions")))),
        h("tbody", {}, rows)))));
  }

  function credentialsDialog(title, user, password, note) {
    return modal({ title, body: h("div", { class: "dlg-body" },
      h("p", { class: "muted" }, note),
      h("div", { class: "field" }, "User ID", h("div", { class: "cred" }, user)),
      h("div", { class: "field" }, "Temporary password", h("div", { class: "cred" }, password)),
      h("p", { class: "hint" }, "This password is shown only once. The employee will be asked to choose their own at first sign-in.")) });
  }

  async function userForm(user) {
    const editing = !!user;
    const f = {
      id: h("input", { value: user ? user.user_id : "", disabled: editing, autocomplete: "off", spellcheck: "false", "aria-label": "User ID" }),
      name: h("input", { value: user ? user.name : "", autocomplete: "off", maxlength: "80", dir: "auto", "aria-label": "Name" }),
      dept: h("input", { value: user ? user.department : "", autocomplete: "off", maxlength: "80", dir: "auto", "aria-label": "Department", list: "deptList" }),
      role: h("select", { "aria-label": "Role" }, h("option", { value: "user" }, "Employee (logs calls)"), h("option", { value: "admin" }, "Administrator")),
      pw: h("input", { type: "text", autocomplete: "off", placeholder: "Leave empty to generate one", "aria-label": "Initial password" }),
    };
    f.role.value = user ? user.role : "user";
    const depts = h("datalist", { id: "deptList" }, (state.departments || []).map((d) => h("option", { value: d })));
    const body = h("div", { class: "dlg-body" },
      h("label", { class: "field" }, "User ID (employee number)", f.id), h("label", { class: "field" }, "Full name", f.name),
      h("label", { class: "field" }, "Department", f.dept, depts), h("label", { class: "field" }, "Role", f.role),
      editing ? null : h("label", { class: "field" }, "Initial password", f.pw));
    const result = await modal({
      title: editing ? `Edit ${user.name}` : "Add employee", body, submitLabel: editing ? "Save changes" : "Create account",
      onSubmit: async () => {
        if (editing) {
          await api("PUT", `/api/admin/users/${encodeURIComponent(user.user_id)}`, { name: f.name.value, department: f.dept.value, role: f.role.value });
          toast("Account updated");
          return { updated: true };
        }
        return api("POST", "/api/admin/users", { user_id: f.id.value, name: f.name.value, department: f.dept.value, role: f.role.value, password: f.pw.value });
      },
    });
    if (result && result.temp_password) {
      await credentialsDialog("Account created", result.user.user_id, result.temp_password, `Give these sign-in details to ${result.user.name}.`);
    }
    if (result) refresh(true);
  }

  async function resetPassword(u) {
    const ok = await confirmDialog({ title: `Reset password for ${u.name}?`, message: "A new temporary password will be generated. Their current password stops working immediately.", confirmLabel: "Reset password", danger: false });
    if (!ok) return;
    try {
      const { temp_password } = await api("POST", `/api/admin/users/${encodeURIComponent(u.user_id)}/reset-password`, {});
      await credentialsDialog("Password reset", u.user_id, temp_password, `Give this new password to ${u.name}.`);
      refresh(true);
    } catch (e) { toast(e.message, "error"); }
  }

  async function toggleActive(u) {
    if (u.active) {
      const ok = await confirmDialog({ title: `Disable ${u.name}?`, message: "They will be signed out and cannot sign in again until you enable the account. Their recorded calls are kept.", confirmLabel: "Disable account" });
      if (!ok) return;
    }
    try {
      await api("PUT", `/api/admin/users/${encodeURIComponent(u.user_id)}`, { active: !u.active });
      toast(u.active ? "Account disabled" : "Account enabled");
      refresh(true);
    } catch (e) { toast(e.message, "error"); }
  }

  async function importUsers() {
    const file = h("input", { type: "file", accept: ".csv,.xlsx", "aria-label": "File to import" });
    const body = h("div", { class: "dlg-body" },
      h("p", { class: "muted" }, "Choose an Excel (.xlsx) or CSV file. The first row must contain column titles:"),
      h("p", {}, h("code", {}, "user_id, name, department"), " and optionally ", h("code", {}, "password")),
      h("p", { class: "hint" }, "Employees without a password get a generated one. You can download the list of passwords right after the import."),
      file);
    let report = null;
    await modal({
      title: "Import employees", body, submitLabel: "Import",
      onSubmit: async () => {
        if (!file.files[0]) throw new Error("Choose a file first.");
        const fd = new FormData();
        fd.append("file", file.files[0]);
        report = await api("POST", "/api/admin/users/import", fd);
      },
    });
    if (!report) return;
    const csv = "﻿user_id,name,password\n" + report.credentials.map((c) => [c.user_id, c.name, c.password].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(",")).join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    await modal({
      title: `${report.created} account${report.created === 1 ? "" : "s"} created`, wide: true,
      body: h("div", { class: "dlg-body" },
        report.created ? h("p", {}, h("a", { class: "btn btn-primary", href: url, download: "new-employee-passwords.csv" }, icon("download"), "Download passwords (CSV)"),
          h("span", { class: "hint" }, "  Shown only now — keep the file safe and delete it after handing out passwords.")) : null,
        report.skipped.length ? h("div", {}, h("p", { class: "form-error" }, `${report.skipped.length} row(s) skipped:`),
          h("ul", { class: "hint" }, report.skipped.slice(0, 12).map((s) => h("li", {}, `${s.row ? "Row " + s.row : "ID " + s.user_id}: ${s.reason}`)))) : null),
    });
    URL.revokeObjectURL(url);
    refresh(true);
  }

  // -------------------------------------------------------------- branches
  async function loadBranches(force) {
    const d = await api("GET", "/api/branches");
    state.branches = d.branches;
    fillRegions();
    if (force || changed("branches", { n: d.branches.length, first: d.branches[0], last: d.branches[d.branches.length - 1] })) renderBranches();
    return new Date().toTimeString().slice(0, 8).padStart(19, " ");
  }

  function renderBranches() {
    const regions = new Set(state.branches.map((b) => b.region)).size;
    const search = h("input", { type: "search", class: "search", placeholder: "Search number, name or region", "aria-label": "Search branches" });
    const body = h("tbody");
    const note = h("p", { class: "hint" });
    const draw = () => {
      const q = CL.normAr(search.value).trim();
      const hits = state.branches.filter((b) => !q || CL.normAr(`${b.code} ${b.name} ${b.region}`).includes(q));
      body.replaceChildren(...hits.slice(0, 200).map((b) => h("tr", {},
        h("td", { class: "ip" }, b.code), h("td", {}, bidi(b.name)), h("td", {}, bidi(b.region)), h("td", {}, bidi(b.type)))));
      note.textContent = hits.length > 200 ? `Showing the first 200 of ${hits.length} — type to narrow the list.` : `${hits.length} shown`;
    };
    search.addEventListener("input", debounce(draw, 150));
    panel("branches").replaceChildren(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, `${state.branches.length} branches in ${regions} regions`), search, h("span", { class: "grow" }),
        h("button", { class: "btn btn-primary", type: "button", onclick: importBranches }, icon("upload"), "Import from Excel")),
      h("p", { class: "hint" }, "Employees choose from this list when the caller is a branch. Importing replaces the whole list; calls already logged keep the branch they were logged with."),
      h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, ["Number", "Branch", "Region", "Type"].map((c) => h("th", {}, c)))), body)), note));
    draw();
  }

  async function importBranches() {
    const file = h("input", { type: "file", accept: ".xlsx,.csv", "aria-label": "Branch list file" });
    let report = null;
    await modal({
      title: "Import the branch list", submitLabel: "Replace the list",
      body: h("div", { class: "dlg-body" },
        h("p", { class: "muted" }, "Choose an Excel (.xlsx) or CSV file. The first row must have the column titles:"),
        h("p", {}, h("code", { dir: "auto" }, "رقم الفرع"), ", ", h("code", { dir: "auto" }, "اسم الفرع"), " and optionally ",
          h("code", { dir: "auto" }, "اسم المنطقة التابع لها"), ", ", h("code", { dir: "auto" }, "نوع الفرع"), " (English titles work too)."),
        h("p", { class: "hint" }, `The current list of ${state.branches.length} branches will be replaced.`), file),
      onSubmit: async () => {
        if (!file.files[0]) throw new Error("Choose a file first.");
        const fd = new FormData();
        fd.append("file", file.files[0]);
        report = await api("POST", "/api/admin/branches/import", fd);
      },
    });
    if (!report) return;
    toast(`Branch list updated: ${report.count} branches` + (report.duplicates_merged ? ` (${report.duplicates_merged} duplicates merged)` : ""));
    refresh(true);
  }

  // ----------------------------------------------------------------- audit
  function auditDetails(e) {
    const d = e.details || {}, who = (o) => (o && o !== e.actor ? ` (employee ${o})` : "");
    const reason = (k) => reasonLabel(k);
    switch (e.action) {
      case "record_created": return `IP ${d.ip_phone} · ${reason(d.reason)}` + (d.branch ? ` · ${d.branch}` : "") + (d.note ? ` · “${d.note}”` : "");
      case "record_updated": return Object.keys(d.after || {}).map((k) => `${k.replace("_", " ")}: ${k === "reason" ? reason(d.before[k]) : d.before[k] || "—"} → ${k === "reason" ? reason(d.after[k]) : d.after[k] || "—"}`).join(" · ") + who(d.owner);
      case "record_deleted": return `${d.date} ${hhmm(d.time || "")} · IP ${d.ip_phone} · ${reason(d.reason)}` + (d.branch ? ` · ${d.branch}` : "") + who(d.owner);
      case "user_created": return `${d.name} (${d.role})`;
      case "user_updated": return Object.entries(d).map(([k, v]) => `${k}: ${v.from} → ${v.to}`).join(" · ");
      case "export": return `${e.target} · ${d.from} → ${d.to} · ${fmtNum(d.rows)} rows`;
      case "users_imported": return `${d.created} created, ${d.skipped} skipped`;
      case "branches_imported": return `${d.count} branches (was ${d.previous}), ${d.duplicates_merged} duplicates merged`;
      case "login_failed": return d.reason === "disabled" ? "account is disabled" : "";
      default: return "";
    }
  }

  async function loadAudit(force) {
    const a = state.audit;
    const d = await api("GET", `/api/admin/audit?${qs({ from: state.from, to: state.to, actor: a.actor, action: a.action, q: a.q, page: a.page })}`);
    state.audit.page = d.page;
    fillActions(d.actions);
    if (force || changed("audit", d)) renderAudit(d);
    return new Date().toTimeString().slice(0, 8).padStart(19, " ");
  }

  function renderAudit(d) {
    const exportQS = rangeQS();
    const table = d.items.length ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["Time", "User", "Action", "Details", "From"].map((c) => h("th", {}, c)))),
      h("tbody", {}, d.items.map((e) => h("tr", {},
        h("td", { class: "nowrap" }, e.ts), h("td", {}, bidi(e.actor_name || e.actor || "—"), e.actor_name ? h("span", { class: "hint" }, `  ${e.actor}`) : null),
        h("td", { class: "nowrap" }, AUDIT_LABELS[e.action] || e.action), h("td", { class: "note" }, bidi(auditDetails(e)), e.target && !e.action.startsWith("export") && !e.action.startsWith("record") ? h("span", { class: "hint" }, ` ${e.target}`) : null),
        h("td", { class: "nowrap hint" }, e.ip || "")))))) : h("p", { class: "empty" }, "No log entries match.");
    panel("audit").replaceChildren(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Audit log"), h("span", { class: "hint" }, "every sign-in, edit, delete, account change and export"), h("span", { class: "grow" }),
        dlLink("Excel", `/export/audit.xlsx?${exportQS}`), dlLink("CSV", `/export/audit.csv?${exportQS}`)),
      table, pager(d, (p) => { state.audit.page = p; refresh(true); })));
  }

  // ---------------------------------------------------------------- export
  function renderExport() {
    const r = rangeQS();
    const range = state.from === state.to ? fmtDay(state.from) : `${fmtDay(state.from)} → ${fmtDay(state.to)}`;
    const card = (title, text, ...links) => h("div", { class: "card" }, h("h2", {}, title), h("p", { class: "muted" }, text), h("div", { class: "row" }, links));
    panel("export").replaceChildren(
      h("p", { class: "muted" }, "Exports cover the date range chosen above: ", h("strong", {}, range), "."),
      h("div", { class: "export-grid" },
        card("Full report", "One Excel workbook: a summary per employee (totals and status split) plus every call in the period.", dlLink("Excel (.xlsx)", `/export/report.xlsx?${r}`)),
        card("All calls", "A flat list of every call with employee, department, IP phone, reason (Arabic and English), status and note. Opens in Excel with Arabic text intact.",
          dlLink("Excel (.xlsx)", `/export/records.xlsx?${r}`), dlLink("CSV", `/export/records.csv?${r}`)),
        card("Audit log", "Who signed in, logged, edited or deleted what and when — including the old and new values.",
          dlLink("Excel (.xlsx)", `/export/audit.xlsx?${r}`), dlLink("CSV", `/export/audit.csv?${r}`))));
    return null;
  }

  // ---------------------------------------------------------- filter inputs
  function fillUserFilter() {
    const sel = $("#fUser"), keep = state.rec.user_id || "";
    sel.replaceChildren(h("option", { value: "" }, "All employees"),
      ...state.users.filter((u) => u.role === "user").map((u) => h("option", { value: u.user_id }, `${u.name} (${u.user_id})`)));
    sel.value = keep;
  }
  function fillDepartments() {
    const sel = $("#fDept"), keep = state.rec.department || "";
    sel.replaceChildren(h("option", { value: "" }, "All departments"), ...(state.departments || []).map((d) => h("option", { value: d }, d)));
    sel.value = keep;
  }
  function fillRegions() {
    const sel = $("#fRegion"), keep = state.rec.region || "";
    const regions = [...new Set(state.branches.map((b) => b.region).filter(Boolean))];
    sel.replaceChildren(h("option", { value: "" }, "All regions"), ...regions.map((r) => h("option", { value: r }, r)));
    sel.value = keep;
  }
  function fillReasons() {
    const sel = $("#fReason");
    sel.replaceChildren(h("option", { value: "" }, "All reasons"), ...state.reasons.map((r) => h("option", { value: r.key }, `${r.label_ar} — ${r.label_en}`)));
  }
  function fillActions(actions) {
    const sel = $("#aAction"), keep = state.audit.action || "";
    const all = [...new Set([...Object.keys(AUDIT_LABELS), ...actions])];
    if (sel.options.length === all.length + 1) return;
    sel.replaceChildren(h("option", { value: "" }, "All actions"), ...all.map((a) => h("option", { value: a }, AUDIT_LABELS[a] || a)));
    sel.value = keep;
  }

  // ------------------------------------------------------------------ init
  async function init() {
    document.querySelectorAll("#tabs [role=tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
    document.querySelectorAll("#presets button").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.preset === "custom") { state.preset = "custom"; syncFilters(); $("#fFrom").focus(); return; }
      applyPreset(b.dataset.preset);
      state.rec.page = state.audit.page = 1;
      refresh(true);
    }));
    $("#fFrom").addEventListener("change", onRangeInput);
    $("#fTo").addEventListener("change", onRangeInput);
    const onRec = (key) => (ev) => { state.rec[key] = ev.target.value; state.rec.page = 1; refresh(true); };
    $("#fUser").addEventListener("change", onRec("user_id"));
    $("#fDept").addEventListener("change", onRec("department"));
    $("#fReason").addEventListener("change", onRec("reason"));
    $("#fRegion").addEventListener("change", onRec("region"));
    $("#fQuery").addEventListener("input", debounce(onRec("q"), 250));
    const onAudit = (key) => (ev) => { state.audit[key] = ev.target.value; state.audit.page = 1; refresh(true); };
    $("#aActor").addEventListener("input", debounce(onAudit("actor"), 250));
    $("#aAction").addEventListener("change", onAudit("action"));
    $("#aQuery").addEventListener("input", debounce(onAudit("q"), 250));
    window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

    try {
      const me = await api("GET", "/api/me");
      window.CL_ME = me.user;
      state.reasons = me.reasons;
      state.reasonByKey = Object.fromEntries(me.reasons.map((r) => [r.key, r]));
      state.today = me.today;
      state.users = (await api("GET", "/api/admin/users")).users;
      state.branches = (await api("GET", "/api/branches")).branches;
    } catch (e) {
      toast(e.message, "error");
      return;
    }
    fillReasons();
    fillRegions();
    fillUserFilter();
    applyPreset("today");
    showTab(location.hash.slice(1));
    setInterval(() => {
      if (document.hidden || document.querySelector("dialog[open]")) return;
      if (Date.now() - (state.lastRefresh || 0) >= pollDelay()) refresh(false);
    }, REFRESH_MS);
  }
  init();
})();
