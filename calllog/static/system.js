"use strict";
/* System (/system, administrators only): the health of the application and the collectors, and every setting of the Hub.

   Tabs (the URL hash keeps the tab: /system#settings):
     Health      GET /api/ops/health    problems in plain words, data folder + disk, snapshot ages, collector heartbeats, accounts, server
     Collectors  GET /api/ops/jobs      one row per job (state, last OK, next run, result), "Run now" with a confirmation, the collector PC,
                                        what collector_config.json pins, the accounts (user name + "password set: yes/no" - never a password)
     Settings    GET/PUT /api/ops/settings   forms built from the descriptor list SECTIONS below: General, E-mail, Classification,
                                        E-mail reports, SLA, ITSM. Keys pinned by collector_config.json are read-only. Server validation
                                        errors are shown next to the field they name. Unsaved changes survive tab switches and are
                                        guarded on leaving the page.
     Logs        GET /api/ops/jobs/<job>/log     the tail of a job's log, refresh / auto refresh
     Reports     GET /api/ops/reports            reports the collector wrote, with download links

   Polling follows docs/HUB-DESIGN.md section 14 (OPS.loader): only the visible tab polls, random start offset, back-off on errors,
   nothing while the browser tab is hidden. Everything from the server goes in as text (CL.h); no innerHTML, no inline styles. */
(function () {
  const { h, $, api, toast, bidi, fmtNum } = CL;
  const TABS = ["health", "collectors", "settings", "logs", "reports"];
  const JOB_INFO = {
    email: { what: "Reads Outlook (read-only) and updates the E-mail page.", sends: false },
    email_report: { what: "The daily report: the Excel workbook and the e-mails to the teams and managers.", sends: true,
      warn: "Running it now SENDS those e-mails." },
    sla: { what: "Reads the security-requests portal and updates the SLA page.", sends: true, warn: "It may send reminder or breach e-mails." },
    itsm: { what: "Reads the ITSM scoreboard and updates the ITSM page.", sends: true, warn: "It may send the ITSM status e-mail." },
  };
  const views = {};
  const when = (iso, now) => (iso ? OPS.fmtWhen(iso, now) : "—");
  const defs = (pairs) => h("dl", { class: "op-defs" }, pairs.filter(Boolean).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  const yesNo = (ok, yes, no) => OPS.chip(ok ? yes : no, ok ? "ok" : "danger", ok ? "check" : "alert");
  const mono = (text) => h("span", { class: "op-mono", dir: "ltr" }, text);
  const clone = (v) => JSON.parse(JSON.stringify(v === undefined ? null : v));

  /** PUT the whole settings document. CL.api drops the per-field `errors` list of a 400, and the page needs it, so this reads the answer itself. */
  async function putSettings(doc) {
    let resp;
    try {
      resp = await fetch("/api/ops/settings", { method: "PUT", credentials: "same-origin", body: JSON.stringify({ settings: doc }),
        headers: { "X-CSRF-Token": CL.csrf(), "Content-Type": "application/json" } });
    } catch (e) {
      const err = new Error("Cannot reach the server. Check your network connection.");
      err.network = true;
      throw err;
    }
    if (resp.status === 401) { location.href = "/login"; throw new Error("Please sign in."); }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const err = new Error(data.error || `Request failed (${resp.status}).`);
      err.status = resp.status;
      err.errors = data.errors;
      throw err;
    }
    return data;
  }

  function setLive(ok, text) {
    $("#live").className = "live " + (ok ? "ok" : "bad");
    $("#liveText").textContent = text;
  }
  function loadingError(root, e, what) {
    root.replaceChildren(OPS.emptyState({ title: `${what} cannot be shown`, text: e.message, icon: "alert" }));
  }

  // ============================================================================================== Health
  views.health = (function () {
    const root = $("#tab-health");
    let loader = null, seen = "", shown = false, uptime = null;

    function problemBanner(d) {
      const items = d.problems || [];
      if (!items.length) {
        return h("div", { class: "op-banner op-t-ok", role: "status" }, OPS.icon("check"),
          h("span", { class: "op-banner-text" }, "Everything looks fine: the data folder is writable, the data is fresh and the collectors answer."));
      }
      return h("div", { class: "op-stack" },
        h("div", { class: `op-banner op-t-${d.status === "error" ? "danger" : "warn"}`, role: "status" }, OPS.icon(d.status === "error" ? "alert" : "clock"),
          h("span", { class: "op-banner-text" }, `${items.length} thing${items.length === 1 ? "" : "s"} need${items.length === 1 ? "s" : ""} attention`)),
        h("ul", { class: "op-problems" }, items.map((p) => h("li", { class: `op-t-${p.level === "danger" ? "danger" : p.level === "warn" ? "warn" : "info"}` },
          OPS.icon(OPS.LEVEL_ICON[p.level] || "info"), bidi(p.text)))));
    }

    function folderCard(d) {
      const f = d.data_folder, disk = f.disk || {}, o = d.ops_folder || {};
      const bar = disk.total_bytes
        ? OPS.stackbar([{ label: "used", value: disk.used_bytes, cls: "op-s0" }, { label: "free", value: disk.free_bytes, cls: "op-t-ok" }], { format: OPS.fmtBytes })
        : h("p", { class: "hint" }, disk.error || "Disk size unknown.");
      return OPS.card("Data folder", { hint: "the shared folder" },
        defs([["Location", mono(f.path)], ["Writable", yesNo(f.writable, "Yes", "No")], ["Write test", f.write_ms != null ? `${Math.round(f.write_ms)} ms` : (f.error || "—")],
          ["Hub files", `${fmtNum(o.files)}${o.truncated ? "+" : ""} files · ${OPS.fmtBytes(o.bytes)}`], ["Call Log days stored", fmtNum(d.records_days)]]),
        h("div", { class: "op-block" }, bar));
    }

    function snapshotCard(d) {
      const interval = (m) => ((d.heartbeats[m] || {}).interval_minutes) || 0;
      const rows = Object.entries(d.snapshots).map(([module, s]) => ({ module, ...s }));
      const table = OPS.table({
        search: false, copy: false, pageSize: 10, pageSizes: [10], caption: "Snapshots",
        columns: [
          { key: "module", label: "Module", render: (r) => h("strong", {}, { email: "E-mail", sla: "Security requests", itsm: "ITSM" }[r.module] || r.module) },
          { key: "generated_at", label: "Scanned", render: (r) => (r.present ? `${when(r.generated_at, d.generated_at)}` : "—") },
          { key: "age_minutes", label: "Age", num: true, render: (r) => (r.present ? OPS.fmtDuration(r.age_minutes) : "—") },
          { key: "host", label: "Collector", render: (r) => r.host || "—" },
          { key: "duration_seconds", label: "Took", num: true, render: (r) => (r.duration_seconds != null ? `${Math.round(r.duration_seconds)} s` : "—") },
          { key: "size_bytes", label: "Size", num: true, render: (r) => (r.size_bytes != null ? OPS.fmtBytes(r.size_bytes) : "—") },
          { key: "state", label: "State", render: (r) => {
            if (r.problem) return OPS.chip("Unreadable", "danger", "alert");
            if (!r.present) return OPS.chip("No data yet", "na", "dash");
            const old = r.age_minutes > 3 * interval(r.module) + 2;
            const chip = old ? OPS.chip("Older than 3 scans", "na", "clock") : OPS.chip("Up to date", "ok", "check");
            if (old) chip.title = "Outside working hours this is normal.";
            return chip;
          } },
        ],
        rows,
      });
      return OPS.card("Snapshots", { hint: "the last good scan of each module" }, table.el);
    }

    function heartbeatCard(d) {
      const rows = Object.values(d.heartbeats);
      const table = OPS.table({
        search: false, copy: false, pageSize: 10, pageSizes: [10], caption: "Collector heartbeats",
        columns: [
          { key: "job", label: "Job", render: (r) => h("strong", {}, OPS.JOB_LABELS[r.job] || r.job) },
          { key: "state", label: "State", render: (r) => OPS.jobChip(r) },
          { key: "age_minutes", label: "Last heartbeat", num: true, render: (r) => (r.present ? `${OPS.fmtDuration(r.age_minutes)} ago` : "—") },
          { key: "runs", label: "Runs", num: true, render: (r) => (r.present ? `${fmtNum(r.runs_ok)} ok · ${fmtNum(r.runs_failed)} failed` : "—") },
        ],
        rows,
      });
      return OPS.card("Collector heartbeats", { hint: "online = heard from in the last 3 scan intervals" }, table.el);
    }

    function accountsCard(d) {
      const u = d.users || {}, a = d.audit_today || {};
      const roles = [["admin", "Administrators"], ["manager", "Managers"], ["user", "Employees"]];
      return OPS.card("People", { hint: "accounts and sign-ins" },
        defs([...roles.map(([k, label]) => {
          const b = (u.by_role || {})[k] || { active: 0, disabled: 0 };
          return [label, `${fmtNum(b.active)} active${b.disabled ? ` · ${fmtNum(b.disabled)} disabled` : ""}`];
        }), ["Failed sign-ins today", h("span", { class: (a.failed_sign_ins || 0) >= 10 ? "op-note op-t-warn" : "" }, fmtNum(a.failed_sign_ins))],
        ["Blocked sign-ins today", fmtNum(a.blocked_sign_ins)], ["Audit entries today", fmtNum(a.entries)]]));
    }

    function serverCard(d) {
      const s = d.server || {}, r = d.pending_requests || { count: 0, items: [] };
      uptime = h("span", {}, OPS.fmtUptime(s.uptime_seconds));
      return OPS.card("Server", { hint: "the program that shows these pages" },
        defs([["Version", s.version], ["Started", when(s.started_at, d.generated_at)], ["Running for", uptime], ["Python", s.python],
          ["System", s.platform], ["Process", `${s.pid} · ${s.threads} threads`],
          ["“Run now” requests waiting", r.count ? h("span", { class: `op-note op-t-${r.count > 5 ? "warn" : "info"}` }, OPS.icon("clock"),
            r.items.map((i) => `${OPS.JOB_LABELS[i.job] || i.job} (${when(i.requested_at, d.generated_at)})`).join(", ")) : "none"]]));
    }

    function kpis(d) {
      const f = d.data_folder, disk = f.disk || {}, pct = disk.free_percent;
      const snaps = Object.values(d.snapshots), have = snaps.filter((s) => s.present);
      const ageOf = (s) => s.age_minutes || 0;
      const sessions = d.users || {};
      const diskTone = pct == null ? "na" : pct < 10 ? "danger" : pct < 20 ? "warn" : "ok";
      return h("div", { class: "op-kpis" },
        OPS.kpi({ label: "Data folder", icon: "database", key: "folder", value: f.writable ? "Writable" : "Not writable", tone: f.writable ? "ok" : "danger",
          sub: f.write_ms != null ? `write test ${Math.round(f.write_ms)} ms` : (f.error || "") }),
        OPS.kpi({ label: "Disk free", icon: "server", key: "disk", value: pct == null ? "—" : `${pct}%`, tone: diskTone,
          sub: disk.free_bytes != null ? `${OPS.fmtBytes(disk.free_bytes)} of ${OPS.fmtBytes(disk.total_bytes)}` : (disk.error || "") }),
        OPS.kpi({ label: "Snapshots", icon: "file", key: "snapshots", value: `${have.length} of ${snaps.length}`, tone: have.length === snaps.length ? "ok" : "warn",
          sub: have.length ? `oldest ${OPS.fmtDuration(Math.max(...have.map(ageOf)))}` : "no scan yet" }),
        OPS.kpi({ label: "Active accounts", icon: "user", key: "users", value: fmtNum(sessions.active), sub: sessions.disabled ? `${sessions.disabled} disabled` : "none disabled" }),
        OPS.kpi({ label: "Failed sign-ins today", icon: "lock", key: "signins", value: fmtNum((d.audit_today || {}).failed_sign_ins),
          tone: ((d.audit_today || {}).failed_sign_ins || 0) >= 10 ? "warn" : null, sub: `${(d.audit_today || {}).blocked_sign_ins || 0} blocked` }),
        OPS.kpi({ label: "Up for", icon: "activity", key: "uptime", value: OPS.fmtUptime((d.server || {}).uptime_seconds), sub: `since ${OPS.fmtClock((d.server || {}).started_at)}` }));
    }

    function render(d) {
      const key = JSON.stringify(d, (k, v) => (k === "generated_at" || k === "uptime_seconds" || k === "write_ms" ? undefined : v));
      if (uptime && shown) uptime.textContent = OPS.fmtUptime((d.server || {}).uptime_seconds);
      if (key === seen) return;
      seen = key;
      shown = true;
      root.replaceChildren(h("div", { class: "op-sys" }, problemBanner(d), kpis(d),
        h("div", { class: "op-sys-grid" }, folderCard(d), accountsCard(d), serverCard(d)), snapshotCard(d), heartbeatCard(d)));
    }
    return {
      show() {
        if (!loader) {
          loader = OPS.loader({ url: "/api/ops/health", interval: 30000, since: false,
            onData: (d) => { render(d); setLive(true, `Live · checked ${OPS.fmtClock(d.generated_at, true)}`); },
            onError: (e) => { setLive(false, e.network ? "Offline · retrying…" : e.message); if (!shown) loadingError(root, e, "The health report"); } });
        }
        loader.start();
      },
      hide() { if (loader) loader.stop(); },
    };
  })();

  // ========================================================================================== Collectors
  views.collectors = (function () {
    const root = $("#tab-collectors");
    let loader = null, seen = "", table = null, host = null, info = null, last = null;

    async function runNow(job) {
      const meta = JOB_INFO[job] || {}, label = OPS.JOB_LABELS[job] || job;
      const ok = await OPS.confirm({ title: `Run "${label}" now?`, message: `${meta.what || ""} It starts within a minute and ignores the working days and hours. ${meta.warn || ""}`.trim(),
        confirmLabel: "Run now", danger: !!meta.sends && job === "email_report" });
      if (!ok) return;
      try {
        const r = await api("POST", `/api/ops/jobs/${encodeURIComponent(job)}/run`);
        toast(r.already_pending ? "A request for this job is already waiting for the collector." : "Requested. The collector starts it within a minute.");
      } catch (e) {
        toast(e.message, "error");
      }
      loader.refresh();
    }

    function resultCell(r) {
      if (!r.present) return h("span", { class: "hint" }, "The collector has not reported yet.");
      if (r.paused) return h("span", { class: "op-note op-t-danger" }, OPS.icon("alert"), bidi(r.paused_reason || "Login failed - fix the password in collector_config.json. Retries are paused until it changes or you press Run now."));
      if (r.last_status === "error" && r.last_error) return h("span", { class: "op-note op-t-danger" }, OPS.icon("alert"), bidi(r.last_error));
      return h("span", {}, bidi(r.last_summary || "—"), r.last_duration_seconds != null ? h("span", { class: "hint" }, ` · took ${Math.round(r.last_duration_seconds)} s`) : null);
    }

    function jobsCard(d) {
      const waiting = new Set((d.pending || []).map((p) => p.job));
      const rows = Object.values(d.jobs);
      const columns = [
        { key: "job", label: "Job", render: (r) => h("div", {}, h("strong", {}, OPS.JOB_LABELS[r.job] || r.job), h("div", { class: "hint" }, (JOB_INFO[r.job] || {}).what || "")) },
        { key: "state", label: "State", nowrap: true, render: (r) => h("div", {}, OPS.jobChip(r), r.present && r.host ? h("div", { class: "hint" }, r.host) : null) },
        { key: "last_ok_at", label: "Last OK", nowrap: true, render: (r) => when(r.last_ok_at, d.server_time) },
        { key: "next_run_at", label: "Next run", nowrap: true, render: (r) => (r.enabled === false ? "—" : when(r.next_run_at, d.server_time)) },
        { key: "result", label: "Last result", render: resultCell },
        { key: "runs", label: "Runs", num: true, nowrap: true, render: (r) => (r.present ? `${fmtNum(r.runs_ok)} ok · ${fmtNum(r.runs_failed)} failed` : "—") },
        { key: "action", label: "", nowrap: true, render: (r) => (waiting.has(r.job)
          ? OPS.chip("Waiting for the collector", "info", "clock")
          : h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-label": `Run ${OPS.JOB_LABELS[r.job] || r.job} now`, onclick: () => runNow(r.job) }, OPS.icon("play"), "Run now")) },
      ];
      if (!table) {
        table = OPS.table({ columns, rows, search: false, copy: false, pageSize: 10, pageSizes: [10], caption: "Collector jobs", rowKey: (r) => r.job });
        host.replaceChildren(table.el);
      } else {
        table.setRows(rows);
      }
    }

    function machineCard(d) {
      const c = d.collector_info || {};
      if (!c.host) {
        return OPS.card("Collector PC", {}, OPS.emptyState({ title: "No collector has introduced itself yet", icon: "server",
          text: "Start it on the PC that has Outlook and Chrome:  python collector.py  (it must stay signed in to Windows)." }));
      }
      return OPS.card("Collector PC", { hint: c.updated_at ? `reported ${when(c.updated_at, d.server_time)}` : "" },
        defs([["Computer", c.host], ["Program", c.version], ["Python", c.python], ["Started", when(c.started_at, d.server_time)], ["Process", c.pid],
          ["Job isolation", c.isolated ? "each job in its own process" : "in the collector process"],
          ["Config file", mono(c.config_path || "—")], ["Config file found", yesNo(!!c.config_found, "Yes", "No")]]));
    }

    function pinnedCard(d) {
      const keys = ((d.collector_info || {}).pinned_keys) || [];
      return OPS.card("Set by collector_config.json", { hint: "wins over the Settings tab" },
        keys.length
          ? h("div", { class: "op-stack" }, h("p", { class: "muted" }, "These settings are read from the file on the collector PC. Change them there; the Settings tab shows them read-only."),
            h("ul", { class: "op-tl-items" }, keys.map((k) => h("li", {}, mono(k)))))
          : h("p", { class: "muted" }, "Nothing is pinned: every setting comes from the Settings tab."));
    }

    function accountsCard(d) {
      const accounts = ((d.collector_info || {}).accounts) || {};
      const names = { sla: "Security portal (SLA)", itsm: "ITSM" };
      const rows = Object.entries(accounts).map(([kind, a]) => ({ kind, ...a }));
      if (!rows.length) {
        return OPS.card("Accounts", {}, h("p", { class: "muted" }, "No accounts reported yet."));
      }
      return OPS.card("Accounts", { hint: "the portals the collector signs in to" },
        h("div", { class: "table-wrap" }, h("table", { class: "data op-table" },
          h("thead", {}, h("tr", {}, ["Account", "User name", "Password set", "From"].map((t) => h("th", { scope: "col" }, t)))),
          h("tbody", {}, rows.map((a) => h("tr", {}, h("td", {}, h("strong", {}, names[a.kind] || a.kind)), h("td", {}, a.username ? mono(a.username) : "—"),
            h("td", {}, yesNo(!!a.has_password, "Yes", "No")), h("td", { class: "op-wrap" }, a.password_source || "—")))))),
        rows.some((a) => !a.has_password)
          ? h("p", { class: "op-note op-t-warn op-block" }, OPS.icon("alert"), "A job without a password fails at every run. Put it in collector_config.json on the collector PC, or run  python collector.py --set-password sla  (or itsm).")
          : null);
    }

    function render(d) {
      last = d;
      if (!host) {
        host = h("div", {});
        info = h("div", { class: "op-sys-grid" });
        root.replaceChildren(h("div", { class: "op-sys" }, OPS.card("Collector jobs", { hint: "the jobs run on the collector PC; this page only shows their heartbeat" }, host), info));
      }
      jobsCard(d);
      const key = JSON.stringify([d.collector_info, d.server_time.slice(0, 16)]);
      if (key !== seen) {
        seen = key;
        info.replaceChildren(machineCard(d), accountsCard(d), pinnedCard(d));
      }
    }
    return {
      show() {
        if (!loader) {
          loader = OPS.loader({ url: "/api/ops/jobs", interval: 15000, since: false,
            onData: (d) => { render(d); setLive(true, `Live · updated ${OPS.fmtClock(d.server_time, true)}`); },
            onError: (e) => { setLive(false, e.network ? "Offline · retrying…" : e.message); if (!last) loadingError(root, e, "The collectors"); } });
        }
        loader.start();
      },
      hide() { if (loader) loader.stop(); },
    };
  })();

  // ============================================================================================ Settings
  /* The descriptor list. A field is {path, type, label, hint, ...}; types: switch number text select team days hours times lines kv json schedule.
     Paths are the keys of GET /api/ops/settings; what the server refuses comes back as {path, message} and is shown next to that field. */
  const SCHEDULE = (path, what) => ({ path, type: "schedule", label: `${what} schedule`, wide: true,
    hint: "By default the general working days and hours apply. Turn this off to give this module its own." });
  const MAIL = (base, extra) => [
    { path: `${base}.send`, type: "switch", label: "Send this e-mail" },
    { path: `${base}.to`, type: "lines", kind: "email", label: "To", hint: "One address per line." },
    { path: `${base}.cc`, type: "lines", kind: "email", label: "Cc", hint: "One address per line." }, ...(extra || [])];
  const SECTIONS = [
    { id: "general", title: "General", cards: [
      { title: "Working days and hours", text: "Scheduled scans run only inside this window. “Run now” on the Collectors tab ignores it. Each module can have its own window.", fields: [
        { path: "general.work_days", type: "days", label: "Working days", hint: "Leave every day off to run on all days." },
        { path: "general.active_hours", type: "hours", label: "Active hours", hint: "From-to, like 08:00-17:00. Empty = all day." },
        { path: "general.holidays", type: "lines", kind: "date", label: "Holidays", hint: "One date per line (2026-10-06). Nothing is scheduled on these days." }] }] },
    { id: "email", title: "E-mail", cards: [
      { title: "E-mail scan", text: "The read-only Outlook scan that fills the E-mail page.", fields: [
        { path: "email.enabled", type: "switch", label: "E-mail scan is on" },
        { path: "email.interval_minutes", type: "number", min: 1, max: 1440, label: "Scan every (minutes)" },
        { path: "email.sla_target_minutes", type: "number", min: 1, max: 10080, label: "Reply SLA (minutes)", hint: "A mail waiting longer than this without a team reply is past its SLA." },
        { path: "email.backlog_lookback_days", type: "number", min: 0, max: 30, label: "Earlier days to show", hint: "How many previous days of unanswered mail stay on the dashboard (0 = today only)." },
        { path: "email.history_keep_days", type: "number", min: 1, max: 365, label: "Keep history (days)" },
        { path: "email.target_archive_name", type: "text", max: 120, label: "Outlook archive to scan", hint: "Name of the Outlook archive or root folder. Empty = the default mailbox." },
        { path: "email.outlook_connect_retries", type: "number", min: 1, max: 20, label: "Outlook connection tries" },
        { path: "email.outlook_connect_retry_seconds", type: "number", min: 1, max: 300, label: "Seconds between tries" },
        SCHEDULE("email.schedule", "E-mail")] },
      { title: "Mailboxes and teams", text: "Who the business mailboxes are and who answers for each team.", fields: [
        { path: "email.business_emails", type: "lines", kind: "email", label: "Business mailboxes", hint: "A mail counts when one of these is in its TO line. One per line." },
        { path: "email.teams", type: "lines", kind: "text", label: "Teams", hint: "One per line. “Unclassified” is always kept." },
        { path: "email.manager_emails", type: "lines", kind: "email", label: "Managers", hint: "Their replies count but are not quality-reviewed." },
        { path: "email.ignored_sender_emails", type: "lines", kind: "email", label: "Ignored senders", hint: "Mail from these addresses is never counted." },
        { path: "email.team_members", type: "lines", kind: "email", label: "Other team members", hint: "People whose reply counts as the team's reply but who are not listed per team below." },
        { path: "email.team_members_by_team", type: "kv", label: "Members of each team", wide: true,
          kv: { key: "team", value: "lines", add: "Add a team", empty: "No team members listed yet.", valueHint: "e-mail addresses, one per line" } },
        { path: "email.team_link_groups", type: "kv", label: "Linked teams", wide: true, hint: "A reply from a member of a linked team also answers for the team on the left.",
          kv: { key: "team", value: "lines", add: "Link teams", empty: "No linked teams.", valueHint: "team names, one per line", valueKind: "text" } }] },
      { title: "Advanced", details: true, text: "Everything else the e-mail engine may read (extra keywords, system map overrides). Plain JSON; leave {} unless you were told otherwise.", fields: [
        { path: "email.advanced", type: "json", label: "Advanced settings (JSON)", wide: true }] }] },
    { id: "classification", title: "Classification", cards: [
      { title: "Unclassified e-mails", classification: true, fields: [
        { path: "email.classification.system_team_map", type: "kv", label: "Keyword rules", wide: true,
          hint: "A word or application name found in the subject or body sends the mail to a team. These rules win over the built-in list.",
          kv: { key: "text", value: "team", keyPlaceholder: "keyword, e.g. swift", add: "Add a keyword rule", empty: "No keyword rules yet." } },
        { path: "email.classification.address_team_map", type: "kv", label: "Mailbox rules", wide: true,
          hint: "Mail sent TO one of your business mailboxes goes to that mailbox's team.",
          kv: { key: "email", value: "team", keyPlaceholder: "mailbox@example.test", add: "Add a mailbox rule", empty: "No mailbox rules yet." } },
        { path: "email.classification.unclassified_team", type: "team", label: "Where mails with no team go", empty: "Keep them in Unclassified",
          hint: "Instead of the Unclassified bucket. Leave on “Keep” to review them by hand." },
        { path: "email.classification.extra_no_action_keywords", type: "lines", kind: "text", label: "Extra “no action needed” phrases",
          hint: "Arabic or English. A mail containing one of these needs no reply. One per line." }] }] },
    { id: "reports", title: "E-mail reports", cards: [
      { title: "Daily report", text: "The old daily run: the Excel workbook and the e-mails to the managers and teams.", fields: [
        { path: "email.report.send", type: "switch", label: "Send the daily report" },
        { path: "email.report.send_times", type: "times", label: "Send at", hint: "24-hour times, comma separated, like 16:30." },
        { path: "email.report.subject_prefix", type: "text", max: 120, label: "Subject starts with" },
        { path: "email.report.attach_excel", type: "switch", label: "Attach the Excel workbook" },
        { path: "email.report.attach_pending_msg", type: "switch", label: "Attach the pending mails (.msg)" },
        { path: "email.report.max_pending_attachments", type: "number", min: 1, max: 500, label: "Most pending mails to attach" },
        { path: "email.report.to", type: "lines", kind: "email", label: "To", hint: "One address per line." },
        { path: "email.report.cc", type: "lines", kind: "email", label: "Cc", hint: "One address per line." },
        { path: "email.report.team_recipients", type: "kv", label: "Team pending lists", wide: true, hint: "Each team gets its own pending list at these addresses.",
          kv: { key: "team", value: "recipients", add: "Add a team", empty: "No team lists." } }] },
      { title: "Assignment, quality and end of day", fields: [
        { path: "email.assignment_notifications.send", type: "switch", label: "Tell a person when a mail is assigned to them" },
        { path: "email.assignment_notifications.cc", type: "lines", kind: "email", label: "Assignment e-mails Cc" },
        { path: "email.quality_feedback.send", type: "switch", label: "Send quality feedback to the replier" },
        { path: "email.quality_feedback.sample_size", type: "number", min: 1, max: 100, label: "Quality sample size" },
        { path: "email.quality_feedback.cc", type: "lines", kind: "email", label: "Quality feedback Cc" },
        { path: "email.end_of_day.send", type: "switch", label: "Send the end-of-day e-mail" },
        { path: "email.end_of_day.to", type: "lines", kind: "email", label: "End of day To" },
        { path: "email.end_of_day.cc", type: "lines", kind: "email", label: "End of day Cc" }] }] },
    { id: "sla", title: "Security requests", cards: [
      { title: "SLA monitor", text: "The security portal: requests waiting too long at the security user.", fields: [
        { path: "sla.enabled", type: "switch", label: "SLA monitor is on" },
        { path: "sla.interval_minutes", type: "number", min: 1, max: 1440, label: "Scan every (minutes)" },
        { path: "sla.warning_minutes", type: "number", min: 1, max: 10080, label: "Reminder after (minutes)" },
        { path: "sla.breach_minutes", type: "number", min: 1, max: 10080, label: "Breach after (minutes)", hint: "Must be larger than the reminder time." },
        { path: "sla.repeat_minutes", type: "number", min: 0, max: 10080, label: "Repeat e-mail every (minutes)", hint: "While requests stay overdue. 0 = only when something is newly escalated." },
        { path: "sla.username", type: "text", max: 60, label: "Portal user name", hint: "The password is never stored here: see collector_config.json." },
        { path: "sla.base_url", type: "text", max: 300, label: "Portal address", dir: "ltr" },
        SCHEDULE("sla.schedule", "Security requests")] },
      { title: "Reminder and breach e-mails", fields: [...MAIL("sla.mail", [
        { path: "sla.mail.subject", type: "text", max: 150, label: "Subject" },
        { path: "sla.mail.display_only", type: "switch", label: "Show instead of sending", hint: "Opens the e-mail on the collector PC without sending it." },
        { path: "sla.mail.cc_security_users", type: "switch", label: "Copy the security users" }]),
        { path: "sla.owner_emails", type: "kv", label: "Security users' e-mail addresses", wide: true, hint: "The name exactly as the portal shows it, and the address to copy.",
          kv: { key: "text", value: "email", keyPlaceholder: "name on the portal", valuePlaceholder: "name@example.test", add: "Add a person", empty: "No addresses yet." } }] },
      { title: "Portal page details", details: true, text: "Change only when the portal changed: the words and addresses the monitor looks for.", fields: [
        { path: "sla.login_path", type: "text", max: 300, label: "Sign-in page", dir: "ltr" },
        { path: "sla.reassign_path", type: "text", max: 300, label: "Requests page", dir: "ltr" },
        { path: "sla.app_url_fragment", type: "text", max: 100, label: "Application address contains", dir: "ltr" },
        { path: "sla.package_text", type: "text", max: 200, label: "Package column text" },
        { path: "sla.package_link_text", type: "text", max: 100, label: "Package link text" },
        { path: "sla.login_button_text", type: "text", max: 100, label: "Sign-in button text" },
        { path: "sla.search_button_text", type: "text", max: 100, label: "Search button text" },
        { path: "sla.sent_date_header", type: "text", max: 100, label: "Sent-date column" },
        { path: "sla.owner_header", type: "text", max: 100, label: "Security-user column" },
        { path: "sla.detail_headers", type: "lines", kind: "text", label: "Detail columns to keep", hint: "One column title per line." },
        { path: "sla.date_order", type: "select", label: "Date order on the portal", options: () => [["dmy", "day / month / year"], ["mdy", "month / day / year"]] },
        { path: "sla.selectors", type: "kv", label: "Page element overrides", wide: true, hint: "Only if the sign-in page changed: element name -> CSS selector (username, password, login_button).",
          kv: { key: "text", value: "text", keyPlaceholder: "username", valuePlaceholder: "input#user", add: "Add an override", empty: "None - the defaults work." } },
        { path: "sla.browser_channel", type: "text", max: 40, label: "Browser", hint: "chrome or msedge." },
        { path: "sla.browser_executable", type: "text", max: 400, label: "Browser program (optional)", dir: "ltr" },
        { path: "sla.timeout_seconds", type: "number", min: 5, max: 600, label: "Page timeout (seconds)" },
        { path: "sla.ignore_https_errors", type: "switch", label: "Ignore certificate errors" }] }] },
    { id: "itsm", title: "ITSM", cards: [
      { title: "ITSM status", text: "Implementation and rollback requests per role, from the ITSM scoreboard.", fields: [
        { path: "itsm.enabled", type: "switch", label: "ITSM scan is on" },
        { path: "itsm.interval_minutes", type: "number", min: 1, max: 1440, label: "Scan every (minutes)" },
        { path: "itsm.url", type: "text", max: 300, label: "ITSM address", dir: "ltr" },
        { path: "itsm.username", type: "text", max: 60, label: "User name", hint: "The password is never stored here: see collector_config.json." },
        { path: "itsm.roles", type: "lines", kind: "text", label: "Roles to read", hint: "One role per line, as named in ITSM." },
        { path: "itsm.categories", type: "kv", label: "Request categories", wide: true, hint: "The label shown on the page, and the pattern that matches its scoreboard line.",
          kv: { key: "text", value: "text", keyPlaceholder: "label", valuePlaceholder: "pattern", add: "Add a category", empty: "No categories.", mono: true } },
        SCHEDULE("itsm.schedule", "ITSM")] },
      { title: "Status e-mail", fields: [...MAIL("itsm.mail", [
        { path: "itsm.mail.send_times", type: "times", label: "Send at", hint: "24-hour times, comma separated, like 08:30." },
        { path: "itsm.mail.display_only", type: "switch", label: "Show instead of sending" }])] },
      { title: "Browser", details: true, fields: [
        { path: "itsm.headless", type: "switch", label: "Run Chrome without a window" },
        { path: "itsm.cdp_port", type: "number", min: 1024, max: 65535, label: "Chrome debugging port" },
        { path: "itsm.browser_executable", type: "text", max: 400, label: "Chrome program (optional)", dir: "ltr" }] }] },
  ];
  const ALL_FIELDS = SECTIONS.flatMap((s) => s.cards.flatMap((c) => c.fields.map((f) => ({ ...f, section: s.id }))));
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const teamsOf = (doc) => ((doc.email || {}).teams || []).filter(Boolean);

  const getPath = (obj, path) => path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
  function setPath(obj, path, value) {
    const keys = path.split(".");
    let o = obj;
    for (const k of keys.slice(0, -1)) { if (o[k] == null || typeof o[k] !== "object") o[k] = {}; o = o[k]; }
    o[keys[keys.length - 1]] = value;
  }
  const same = (a, b) => JSON.stringify(a === undefined ? null : a) === JSON.stringify(b === undefined ? null : b);
  const underPath = (errPath, path) => errPath === path || errPath.startsWith(`${path}.`) || errPath.startsWith(`${path}[`);

  views.settings = (function () {
    const root = $("#tab-settings");
    const st = { doc: null, base: null, pinned: [], fields: [], errors: [], section: "general", saving: false, uid: 0, teamSelects: new Set() };
    let barText = null, saveBtn = null, discardBtn = null, bar = null, errorBox = null, tabButtons = {}, panels = {}, tabBadge = null;

    const isPinned = (path) => st.pinned.some((k) => k === path || k.startsWith(`${path}.`) || path.startsWith(`${k}.`));
    const nextId = () => `set-${++st.uid}`;
    const pinBadge = () => h("span", { class: "op-pinned", title: "Read from collector_config.json on the collector PC. The value here is ignored." }, OPS.icon("lock"), "set in collector_config.json");

    // ------------------------------------------------------------------ change tracking
    const changed = () => ALL_FIELDS.filter((f) => !same(getPath(st.doc, f.path), getPath(st.base, f.path)));
    const isDirty = () => !!st.doc && changed().length > 0;
    function updateBar() {
      const list = st.doc ? changed() : [], n = list.length, bad = st.fields.filter((f) => f.local).length;
      barText.textContent = n ? `${n} unsaved change${n === 1 ? "" : "s"}` : "No unsaved changes";
      bar.classList.toggle("is-clean", n === 0);
      saveBtn.disabled = st.saving || n === 0 || bad > 0;
      discardBtn.disabled = st.saving || n === 0;
      tabBadge.hidden = n === 0;
      // sub-tab badges: how many fields of each section changed or are wrong
      const dirty = {}, wrong = {};
      list.forEach((f) => { dirty[f.section] = (dirty[f.section] || 0) + 1; });
      st.fields.forEach((f) => { if (f.errorText) wrong[f.section] = (wrong[f.section] || 0) + 1; });
      for (const s of SECTIONS) {
        const b = tabButtons[s.id].querySelector(".op-count");
        const text = wrong[s.id] ? `${wrong[s.id]} to fix` : dirty[s.id] ? `${dirty[s.id]} changed` : "";
        b.textContent = text;
        b.hidden = !text;
        b.className = "op-count" + (wrong[s.id] ? " op-t-danger" : dirty[s.id] ? " op-t-warn" : "");
      }
    }
    function touch(field) {
      field.errorText = "";
      field.err.hidden = true;
      field.el.classList.remove("has-error");
      st.errors = st.errors.filter((e) => !underPath(e.path, field.def.path));
      renderErrorList();
      updateBar();
    }
    function setLocal(field, text) {
      field.local = text;
      field.errorText = text;
      field.err.replaceChildren(...(text ? [OPS.icon("alert"), text] : []));
      field.err.hidden = !text;
      field.el.classList.toggle("has-error", !!text);
    }
    function commit(field, value) {
      setPath(st.doc, field.def.path, value);
      touch(field);
    }

    // ------------------------------------------------------------------ small controls
    function teamSelect(value, opts) {
      const o = opts || {};
      const sel = h("select", { "aria-label": o.label || "Team", disabled: o.disabled });
      sel._opts = o;
      st.teamSelects.add(sel);
      fillTeams(sel, value);
      return sel;
    }
    function fillTeams(sel, value) {
      const o = sel._opts, keep = value !== undefined ? value : sel.value;
      let teams = teamsOf(st.doc).filter((t) => !(o.noUnclassified && t.toLowerCase() === "unclassified"));
      if (keep && !teams.includes(keep)) teams = [...teams, keep];
      sel.replaceChildren(...(o.empty ? [h("option", { value: "" }, o.empty)] : []), ...teams.map((t) => h("option", { value: t, selected: t === keep }, t)));
      sel.value = keep || (o.empty ? "" : teams[0] || "");                                   // a new row starts on the first team
    }
    function refreshTeamSelects() {
      for (const sel of [...st.teamSelects]) {
        if (!sel.isConnected) st.teamSelects.delete(sel);        // its row was removed or the form was rebuilt
        else fillTeams(sel);
      }
    }
    const addButton = (text, onclick) => h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick }, OPS.icon("plus"), text);
    const removeButton = (what, onclick) => h("button", { class: "btn btn-ghost btn-sm btn-danger-text", type: "button", "aria-label": `Remove ${what}`, title: "Remove", onclick }, OPS.icon("trash"));
    const splitLines = (text, kind) => String(text).split(kind === "text" ? /\r?\n/ : /[\s,;]+/).map((x) => x.trim()).filter(Boolean);

    // ------------------------------------------------------------------ field builders: each returns {control, bare?}
    const BUILD = {
      text(f, field, pinned) {
        const input = h("input", { type: "text", id: field.id, value: getPath(st.doc, f.path) ?? "", maxlength: f.max, autocomplete: "off", spellcheck: "false",
          dir: f.dir || "auto", placeholder: pinned ? "set on the collector PC" : f.placeholder, disabled: pinned });
        input.addEventListener("input", () => commit(field, input.value));
        return { control: input };
      },
      number(f, field, pinned) {
        const input = h("input", { type: "number", id: field.id, value: getPath(st.doc, f.path) ?? "", min: f.min, max: f.max, step: 1, inputmode: "numeric", disabled: pinned });
        input.addEventListener("input", () => commit(field, input.value === "" ? "" : Number(input.value)));
        return { control: input };
      },
      hours(f, field, pinned) {
        const input = h("input", { type: "text", id: field.id, value: getPath(st.doc, f.path) ?? "", maxlength: 11, placeholder: "08:00-17:00", dir: "ltr", autocomplete: "off", disabled: pinned });
        input.addEventListener("input", () => commit(field, input.value));
        return { control: input };
      },
      times(f, field, pinned) {
        const input = h("input", { type: "text", id: field.id, value: (getPath(st.doc, f.path) || []).join(", "), placeholder: "16:30", dir: "ltr", autocomplete: "off", disabled: pinned });
        input.addEventListener("input", () => commit(field, splitLines(input.value, "time")));
        return { control: input };
      },
      select(f, field, pinned) {
        const value = getPath(st.doc, f.path) ?? "";
        const options = f.options(st.doc);
        const sel = h("select", { id: field.id, disabled: pinned }, options.map(([v, label]) => h("option", { value: v, selected: v === value }, label)));
        sel.value = value;
        sel.addEventListener("change", () => commit(field, sel.value));
        return { control: sel };
      },
      team(f, field, pinned) {
        const sel = teamSelect(getPath(st.doc, f.path) || "", { label: f.label, empty: f.empty, noUnclassified: true, disabled: pinned });
        sel.id = field.id;
        sel.addEventListener("change", () => commit(field, sel.value));
        return { control: sel };
      },
      switch(f, field, pinned) {
        const input = h("input", { type: "checkbox", role: "switch", id: field.id, checked: !!getPath(st.doc, f.path), disabled: pinned });
        input.addEventListener("change", () => commit(field, input.checked));
        return { bare: true, control: h("label", { class: "op-switch", for: field.id }, input, h("span", { class: "op-switch-track" }), h("span", {}, f.label, pinned ? pinBadge() : null)) };
      },
      days(f, field, pinned) {
        const current = new Set(getPath(st.doc, f.path) || []);
        const boxes = WEEKDAYS.map((d) => h("input", { type: "checkbox", value: d, checked: current.has(d), disabled: pinned }));
        boxes.forEach((b) => b.addEventListener("change", () => commit(field, boxes.filter((x) => x.checked).map((x) => x.value))));
        return { group: true, control: h("div", { class: "op-days", role: "group", "aria-label": f.label }, boxes.map((b) => h("label", {}, b, h("span", {}, b.value)))) };
      },
      lines(f, field, pinned) {
        const area = h("textarea", { id: field.id, rows: 4, value: (getPath(st.doc, f.path) || []).join("\n"), spellcheck: "false", dir: f.kind === "text" ? "auto" : "ltr", disabled: pinned });
        area.addEventListener("input", () => {
          commit(field, splitLines(area.value, f.kind));
          if (f.path === "email.teams") refreshTeamSelects();
        });
        return { control: area };
      },
      json(f, field, pinned) {
        const area = h("textarea", { id: field.id, rows: 6, value: JSON.stringify(getPath(st.doc, f.path) || {}, null, 2), spellcheck: "false", dir: "ltr", disabled: pinned });
        area.addEventListener("input", () => {
          try {
            const v = JSON.parse(area.value || "{}");
            if (v === null || typeof v !== "object" || Array.isArray(v)) throw new Error("it must be an object like { }");
            commit(field, v);
            setLocal(field, "");
          } catch (e) {
            touch(field);
            setLocal(field, `Not valid JSON: ${e.message}`);
            updateBar();
          }
        });
        return { control: area };
      },
      schedule(f, field, pinned) {
        const own = () => getPath(st.doc, f.path) || {};
        const host = h("div", { class: "op-stack" });
        const useGeneral = h("input", { type: "checkbox", role: "switch", id: field.id, checked: own().work_days == null && own().active_hours == null, disabled: pinned });
        const draw = () => {
          const mine = own(), shared = useGeneral.checked;
          host.replaceChildren();
          if (shared) return;
          const boxes = WEEKDAYS.map((d) => h("input", { type: "checkbox", value: d, checked: (mine.work_days || []).includes(d), disabled: pinned }));
          boxes.forEach((b) => b.addEventListener("change", () => commit(field, { ...own(), work_days: boxes.filter((x) => x.checked).map((x) => x.value) })));
          const hours = h("input", { type: "text", value: mine.active_hours ?? "", maxlength: 11, placeholder: "08:00-17:00", dir: "ltr", "aria-label": `${f.label}: active hours`, disabled: pinned });
          hours.addEventListener("input", () => commit(field, { ...own(), active_hours: hours.value }));
          host.append(h("div", { class: "op-days", role: "group", "aria-label": `${f.label}: working days` }, boxes.map((b) => h("label", {}, b, h("span", {}, b.value)))),
            h("label", { class: "op-field" }, h("span", { class: "hint" }, "Active hours (empty = all day)"), hours));
        };
        useGeneral.addEventListener("change", () => {
          if (useGeneral.checked) commit(field, { work_days: null, active_hours: null });
          else commit(field, { work_days: [...(getPath(st.doc, "general.work_days") || [])], active_hours: getPath(st.doc, "general.active_hours") || "" });
          draw();
        });
        draw();
        return { bare: true, control: h("div", { class: "op-stack" },
          h("label", { class: "op-switch", for: field.id }, useGeneral, h("span", { class: "op-switch-track" }), h("span", {}, "Use the general working days and hours", pinned ? pinBadge() : null)), host) };
      },
      kv(f, field, pinned) {
        const spec = f.kv;
        const host = h("div", { class: "op-rows" });
        let rows = Object.entries(getPath(st.doc, f.path) || {}).map(([k, v]) => ({ k, v: clone(v) }));
        const defaultValue = () => (spec.value === "lines" ? [] : spec.value === "recipients" ? { to: [], cc: [] } : "");
        function save() {
          const out = {}, seen = new Set();
          let dup = "";
          for (const r of rows) {
            const key = String(r.k).trim();
            if (!key) continue;
            if (seen.has(key.toLowerCase())) dup = key;
            seen.add(key.toLowerCase());
            out[key] = r.v;
          }
          commit(field, out);
          setLocal(field, dup ? `“${dup}” appears twice. Each name can be used once.` : "");
          updateBar();
        }
        const keyControl = (r) => {
          if (spec.key === "team") {
            const sel = teamSelect(r.k, { label: "Team", disabled: pinned });
            sel.addEventListener("change", () => { r.k = sel.value; save(); });
            if (!r.k) { r.k = sel.value; }
            return sel;
          }
          const input = h("input", { type: "text", value: r.k, placeholder: spec.keyPlaceholder, "aria-label": spec.keyPlaceholder || "Name", autocomplete: "off", spellcheck: "false",
            dir: spec.key === "email" ? "ltr" : "auto", disabled: pinned });
          input.addEventListener("input", () => { r.k = input.value; save(); });
          return input;
        };
        const valueControl = (r) => {
          const area = (get, set, label) => {
            const a = h("textarea", { rows: 2, value: get().join("\n"), "aria-label": label, spellcheck: "false", placeholder: spec.valueHint, dir: spec.valueKind === "text" ? "auto" : "ltr", disabled: pinned });
            a.addEventListener("input", () => { set(splitLines(a.value, spec.valueKind === "text" ? "text" : "email")); save(); });
            return a;
          };
          if (spec.value === "team") {
            const sel = teamSelect(r.v, { label: "Team", noUnclassified: true, disabled: pinned });
            sel.addEventListener("change", () => { r.v = sel.value; save(); });
            if (!r.v) r.v = sel.value;
            return [sel];
          }
          if (spec.value === "lines") return [area(() => r.v || [], (v) => { r.v = v; }, `Values for ${r.k || "this row"}`)];
          if (spec.value === "recipients") {
            r.v = r.v && typeof r.v === "object" ? r.v : { to: [], cc: [] };
            return [area(() => r.v.to || [], (v) => { r.v = { ...r.v, to: v }; }, "To"), area(() => r.v.cc || [], (v) => { r.v = { ...r.v, cc: v }; }, "Cc")];
          }
          const input = h("input", { type: "text", value: r.v ?? "", placeholder: spec.valuePlaceholder, "aria-label": spec.valuePlaceholder || "Value", autocomplete: "off", spellcheck: "false",
            dir: spec.value === "email" || spec.mono ? "ltr" : "auto", disabled: pinned });
          input.addEventListener("input", () => { r.v = input.value; save(); });
          return [input];
        };
        function draw(focusLast) {
          host.replaceChildren(...[...rows.map((r, i) => {
            const controls = valueControl(r);
            return h("div", { class: "op-row" + (controls.length > 1 ? " op-row-3" : "") }, keyControl(r), controls,
              pinned ? h("span") : removeButton(r.k || "this row", () => { rows.splice(i, 1); draw(); save(); }));
          }), rows.length ? null : h("p", { class: "op-row-empty" }, spec.empty || "Nothing added yet."),
          pinned ? null : h("div", {}, addButton(spec.add || "Add", () => { rows.push({ k: "", v: defaultValue() }); draw(true); save(); }))].filter(Boolean));
          if (focusLast) { const last = host.querySelectorAll(".op-row"); const focus = last[last.length - 1]; if (focus) focus.querySelector("input, select, textarea").focus(); }
        }
        draw();
        return { group: true, control: host };
      },
    };

    function buildField(def, section) {
      const field = { def, section, id: nextId(), local: "", errorText: "" };
      const pinned = isPinned(def.path);
      const built = BUILD[def.type](def, field, pinned);
      field.err = h("div", { class: "op-field-error", role: "alert", hidden: true });
      const head = built.bare ? null : h(built.group ? "div" : "label", Object.assign({ class: "op-label" }, built.group ? {} : { for: field.id }), def.label, pinned ? pinBadge() : null);
      const hint = def.hint ? h("span", { class: "hint" }, def.hint) : null;
      field.el = h("div", { class: "op-field" + (def.wide ? " wide" : "") + (pinned ? " is-pinned" : ""), "data-path": def.path },
        head, ...(built.group ? [hint, built.control] : [built.control, hint]), field.err);
      st.fields.push(field);
      return field.el;
    }

    // ------------------------------------------------------------------ the page
    function card(c, section) {
      const body = [c.text ? h("p", { class: "muted" }, c.text) : null];
      if (c.classification) {
        body.push(h("p", { class: "op-note op-t-info op-block" }, OPS.icon("info"),
          h("span", {}, "“Unclassified” is where an e-mail goes when the scan finds no team for it. Rules added here are used from the next scan. Mails that are still unclassified can be assigned one by one, or turned into a rule, on the ",
            h("a", { class: "link", href: "/email#unclassified" }, "Unclassified panel of the E-mail page"), ".")));
      }
      const fields = h("div", { class: "op-fields" }, c.fields.map((f) => buildField(f, section.id)));
      if (c.details) {
        return h("details", { class: "card op-fieldset" }, h("summary", { class: "op-fieldset-title" }, c.title), h("div", { class: "op-stack" }, ...body, fields));
      }
      return h("section", { class: "card op-fieldset" }, h("h2", { class: "op-fieldset-title" }, c.title), ...body, fields);
    }

    function renderErrorList() {
      const label = (path) => { const f = st.fields.find((x) => underPath(path, x.def.path)); return f ? f.def.label : path; };
      errorBox.hidden = !st.errors.length;
      errorBox.replaceChildren(...(st.errors.length ? [h("strong", {}, "These settings were not saved:"),
        h("ul", { class: "op-errors" }, st.errors.slice(0, 12).map((e) => h("li", {}, OPS.icon("alert"), h("span", {}, `${label(e.path)}: ${e.message}`))))] : []));
    }

    function showServerErrors(errors) {
      st.errors = errors;
      let first = null;
      for (const f of st.fields) {
        const mine = errors.filter((e) => underPath(e.path, f.def.path));
        if (!mine.length) continue;
        f.errorText = mine.map((e) => e.message).join("; ");
        f.err.replaceChildren(OPS.icon("alert"), f.errorText);
        f.err.hidden = false;
        f.el.classList.add("has-error");
        first = first || f;
      }
      renderErrorList();
      updateBar();
      if (first) { showSection(first.section); first.el.scrollIntoView({ block: "center" }); }
    }

    function showSection(id) {
      st.section = id;
      for (const s of SECTIONS) {
        tabButtons[s.id].setAttribute("aria-selected", String(s.id === id));
        panels[s.id].hidden = s.id !== id;
      }
    }

    function render() {
      st.fields = [];
      st.teamSelects.clear();
      tabButtons = {};
      panels = {};
      const nav = h("div", { class: "op-subtabs", role: "tablist", "aria-label": "Settings sections" }, SECTIONS.map((s) => {
        const b = h("button", { type: "button", role: "tab", "aria-selected": "false", onclick: () => showSection(s.id) }, s.title, h("span", { class: "op-count", hidden: true }));
        tabButtons[s.id] = b;
        return b;
      }));
      SECTIONS.forEach((s) => { panels[s.id] = h("div", { class: "op-stack", role: "tabpanel", hidden: true }, s.cards.map((c) => card(c, s))); });
      errorBox = h("div", { class: "op-banner op-t-danger", role: "alert", hidden: true });
      barText = h("span", { class: "op-dirty" });
      saveBtn = h("button", { class: "btn btn-primary", type: "button", onclick: save }, OPS.icon("check"), "Save settings");
      discardBtn = h("button", { class: "btn btn-ghost", type: "button", onclick: discard }, "Discard changes");
      bar = h("div", { class: "op-savebar is-clean" }, barText, h("span", { class: "grow" }), discardBtn, saveBtn);
      root.replaceChildren(h("div", { class: "op-form" }, nav, errorBox, ...SECTIONS.map((s) => panels[s.id]), bar));
      showSection(st.section);
      updateBar();
    }

    async function save() {
      if (st.saving || !isDirty()) return;
      const bad = st.fields.find((f) => f.local);
      if (bad) { showSection(bad.section); bad.el.scrollIntoView({ block: "center" }); return; }
      st.saving = true;
      saveBtn.disabled = true;
      saveBtn.replaceChildren(OPS.icon("refresh"), "Saving…");
      try {
        const out = await putSettings(st.doc);
        st.doc = clone(out.settings);
        st.base = clone(out.settings);
        st.errors = [];
        st.saving = false;
        render();
        toast(`Settings saved${out.changed && out.changed.length ? ` (${out.changed.length} changed)` : ""}. The collector uses them at its next run.`);
      } catch (e) {
        st.saving = false;
        saveBtn.replaceChildren(OPS.icon("check"), "Save settings");
        if (e.status === 400 && e.errors) showServerErrors(e.errors);
        else toast(e.message, "error");
        updateBar();
      }
    }
    async function discard() {
      const ok = await OPS.confirm({ title: "Discard your changes?", message: `${changed().length} unsaved change(s) will be lost and the saved settings shown again.`, confirmLabel: "Discard" });
      if (!ok) return;
      st.doc = clone(st.base);
      st.errors = [];
      render();
    }

    async function load() {
      try {
        const d = await api("GET", "/api/ops/settings");
        st.doc = clone(d.settings);
        st.base = clone(d.settings);
        st.pinned = d.pinned_keys || [];
        render();
        setLive(true, "Settings loaded");
      } catch (e) {
        setLive(false, e.network ? "Offline" : e.message);
        root.replaceChildren(OPS.emptyState({ title: "Settings cannot be shown", text: e.message, icon: "alert",
          action: h("button", { class: "btn btn-ghost", type: "button", onclick: load }, OPS.icon("refresh"), "Try again") }));
      }
    }

    // changes survive tab switches; leaving the page asks first
    tabBadge = h("span", { class: "op-count op-t-warn", hidden: true }, "unsaved");
    const tabButton = document.querySelector('#sysTabs [data-tab="settings"]');
    if (tabButton) tabButton.append(" ", tabBadge);
    window.addEventListener("beforeunload", (ev) => { if (isDirty()) { ev.preventDefault(); ev.returnValue = ""; } });

    return {
      show() { if (!st.doc) { root.replaceChildren(h("p", { class: "empty" }, "Loading the settings…")); load(); } else setLive(true, "Settings"); },
      hide() {},
    };
  })();

  // ================================================================================================== Logs
  views.logs = (function () {
    const root = $("#tab-logs");
    const st = { job: "email", lines: 200, auto: false, loaded: false };
    let box = null, meta = null, timer = null, jobButtons = {}, busy = false;

    async function load(quiet) {
      if (busy) return;
      busy = true;
      try {
        const d = await api("GET", `/api/ops/jobs/${encodeURIComponent(st.job)}/log?lines=${st.lines}`);
        const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
        const text = d.lines.join("\n");
        if (box.textContent !== text) {
          box.textContent = text || "";
          if (atEnd || !quiet) box.scrollTop = box.scrollHeight;
        }
        meta.textContent = d.lines.length ? `Last ${d.lines.length} line${d.lines.length === 1 ? "" : "s"} of the ${OPS.JOB_LABELS[st.job]} log · loaded ${OPS.fmtClock(OPS.localNowIso(), true)}`
          : `The ${OPS.JOB_LABELS[st.job]} log is empty - the collector has not run this job yet.`;
        setLive(true, `Live · loaded ${OPS.fmtClock(OPS.localNowIso(), true)}`);
      } catch (e) {
        setLive(false, e.network ? "Offline · retrying…" : e.message);
        if (!quiet) toast(e.message, "error");
      }
      busy = false;
    }
    function pick(job) {
      st.job = job;
      Object.entries(jobButtons).forEach(([k, b]) => b.setAttribute("aria-pressed", String(k === job)));
      load(false);
    }
    function schedule() {
      clearTimeout(timer);
      if (!st.auto || document.hidden || views._active !== "logs") return;
      timer = setTimeout(async () => { await load(true); schedule(); }, 15000 * (1 + Math.random() * 0.2));
    }
    function build() {
      jobButtons = {};
      const jobs = h("div", { class: "segmented", role: "group", "aria-label": "Log of" }, Object.entries(OPS.JOB_LABELS).map(([k, label]) => {
        const b = h("button", { type: "button", "aria-pressed": String(k === st.job), onclick: () => pick(k) }, label);
        jobButtons[k] = b;
        return b;
      }));
      const size = h("select", { "aria-label": "Lines to show", onchange: (ev) => { st.lines = +ev.target.value; load(false); } },
        [100, 200, 500, 1000].map((n) => h("option", { value: n, selected: n === st.lines }, `${n} lines`)));
      const auto = h("input", { type: "checkbox", role: "switch", id: "logAuto" });
      auto.addEventListener("change", () => { st.auto = auto.checked; schedule(); });
      box = h("pre", { class: "op-log", tabindex: "0", "aria-label": "Log text" });
      meta = h("p", { class: "hint" });
      root.replaceChildren(h("div", { class: "op-sys" }, OPS.card("Job logs", { hint: "what each collector job wrote, newest at the bottom" },
        h("div", { class: "op-toolbar" }, jobs, size,
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => load(false) }, OPS.icon("refresh"), "Refresh"),
          h("label", { class: "op-switch", for: "logAuto" }, auto, h("span", { class: "op-switch-track" }), h("span", {}, "Refresh every 15 s")),
          h("span", { class: "grow" }),
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: async () => { const ok = await OPS.copyText(box.textContent); toast(ok ? "Log copied" : "Copy is blocked by this browser", ok ? undefined : "error"); } }, OPS.icon("copy"), "Copy")),
        h("div", { class: "op-block" }, box), meta)));
    }
    document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(); });
    return {
      show() { if (!st.loaded) { st.loaded = true; build(); } load(false); schedule(); },
      hide() { clearTimeout(timer); },
    };
  })();

  // ============================================================================================== Reports
  views.reports = (function () {
    const root = $("#tab-reports");
    let table = null, built = false, filter = "";
    const MODULES = { email: "E-mail", sla: "Security requests", itsm: "ITSM" };

    async function load() {
      try {
        const d = await api("GET", "/api/ops/reports");
        if (!built) build();
        table.setRows(d.reports);
        setLive(true, `${d.reports.length} report${d.reports.length === 1 ? "" : "s"}`);
      } catch (e) {
        setLive(false, e.network ? "Offline" : e.message);
        loadingError(root, e, "The reports list");
        built = false;
      }
    }
    function build() {
      built = true;
      table = OPS.table({
        pageSize: 25, caption: "Reports", searchPlaceholder: "Search reports", copy: false, rowKey: (r) => `${r.module}/${r.name}`, sort: { key: "modified", desc: true },
        empty: () => OPS.emptyState({ title: "No reports yet", icon: "file", text: "The collector writes them after its daily run (and when you press Run now on the Collectors tab)." }),
        columns: [
          { key: "name", label: "Report", sort: true, render: (r) => h("a", { class: "link op-inline-link", href: r.url, download: r.name }, OPS.icon("download"), r.name) },
          { key: "module", label: "Module", sort: true, render: (r) => MODULES[r.module] || r.module, text: (r) => MODULES[r.module] || r.module },
          { key: "modified", label: "Created", sort: true, nowrap: true, render: (r) => OPS.fmtWhen(r.modified), text: (r) => r.modified },
          { key: "size", label: "Size", num: true, sort: true, render: (r) => OPS.fmtBytes(r.size), text: (r) => OPS.fmtBytes(r.size) },
        ],
        rows: [],
        toolbar: h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: load }, OPS.icon("refresh"), "Refresh"),
      });
      root.replaceChildren(h("div", { class: "op-sys" }, OPS.card("Reports", { hint: "Excel workbooks and HTML reports written by the collector" }, table.el)));
    }
    return { show() { load(); }, hide() {} };
  })();

  // ============================================================================================== tabs
  function showTab(name) {
    if (!TABS.includes(name)) name = "health";
    const previous = views._active;
    if (previous && previous !== name) views[previous].hide();
    views._active = name;
    document.querySelectorAll("#sysTabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    TABS.forEach((t) => { $(`#tab-${t}`).hidden = t !== name; });
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    views[name].show();
  }
  document.querySelectorAll("#sysTabs [role=tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  showTab(location.hash.slice(1));
})();
