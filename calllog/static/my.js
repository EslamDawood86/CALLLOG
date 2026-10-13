"use strict";
/* My work (/my): everything that belongs to the signed-in employee under their own user, plus the shared "Team status"
   strip (docs/HUB-DESIGN.md sections 3, 3.1, 3.2, brief docs/handoff/briefs/page-my.md).

   Reads GET /api/ops/my every 30 s through OPS.loader (paused while the tab is hidden). An administrator or manager can
   open somebody's page read-only with /my?as=<id> (the shell adds data-view-as and a banner); the page then sends ?as and
   offers no write. An employee only ever sees their own rows and their team(s)' e-mails, never an "Unclassified" one.
   Everything is built with CL.h / textContent, Arabic carries dir="auto", there is no innerHTML or inline style. */
(function () {
  const { h, $, api, toast, bidi, fmtNum } = CL;
  const SELF = document.currentScript ? document.currentScript.src : "";
  const page = $("#my-page");
  const VIEW_AS = page.dataset.viewAs || "";
  const READ_ONLY = !!VIEW_AS;
  const TICK_MS = 15000;

  const CALL_STATUSES = [["resolved", "Resolved"], ["transferred", "Transferred"], ["ticket_inquiry", "Ticket inquiry"]];

  const state = { data: null, serverTime: "" };
  const el = {};
  let strip = null;
  let skew = 0;

  // ------------------------------------------------------------------ helpers
  const serverNow = () => Date.now() + skew;
  function noteServerTime(iso) {
    const t = OPS.parseNaive(iso);
    if (!Number.isNaN(t)) { skew = t - Date.now(); state.serverTime = iso; }
  }
  function liveMinutes(iso, fallback) {
    const t = OPS.parseNaive(iso);
    if (Number.isNaN(t)) return fallback == null ? null : fallback;
    return Math.max(0, Math.floor((serverNow() - t) / 60000));
  }

  function ensureStyles() {
    if (document.querySelector('link[href*="ops-my.css"]')) return Promise.resolve();
    const href = SELF ? new URL("ops-my.css", SELF).href : "/static/ops-my.css";
    return new Promise((done) => {
      const link = h("link", { rel: "stylesheet", href });
      link.addEventListener("load", done);
      link.addEventListener("error", done);
      setTimeout(done, 2000);
      document.head.append(link);
    });
  }

  // ------------------------------------------------------------------ my calls
  function callsCard(c) {
    const today = c.today, yest = c.yesterday, week = c.last7;
    const shareTotal = state.data.shared && state.data.shared.calls ? state.data.shared.calls.total : 0;
    const hours = c.per_hour_today || [];
    const active = hours.filter((b) => b.total > 0);
    const lo = active.length ? Math.max(0, active[0].key - 1) : 8, hi = active.length ? active[active.length - 1].key : 9;
    const series = hours.filter((b) => b.key >= lo && b.key <= hi).map((b) => b.total);
    const dot = (key, label) => h("div", { class: "op-fact" }, h("dt", {}, h("span", { class: "status-chip" }, h("i", { class: `dot s-${key}` }), label)), h("dd", {}, fmtNum(today[key] || 0)));
    return OPS.card("My calls", { hint: shareTotal ? `${fmtNum(today.total)} of ${fmtNum(shareTotal)} on the team today` : "" },
      h("div", { class: "my-tiles" },
        OPS.kpi({ label: "Today", value: fmtNum(today.total), sub: "calls logged", icon: "phone", hero: true }),
        OPS.kpi({ label: "Yesterday", value: fmtNum(yest.total), sub: "whole day" }),
        OPS.kpi({ label: "Last 7 days", value: fmtNum(week.total), sub: "including today" })),
      series.length > 1 ? h("div", { class: "op-viz" }, OPS.sparkline(series, { width: 260, height: 44, label: "My calls per hour today" }),
        h("span", { class: "hint" }, `per hour, ${String(lo).padStart(2, "0")}:00 – now`)) : null,
      h("dl", { class: "op-facts" }, CALL_STATUSES.map(([key, label]) => dot(key, label))));
  }

  // ------------------------------------------------------------------ e-mail tables
  function mailRow(item, mine) {
    const when = item.first_reply_time || item.received_time;
    return h("tr", {},
      h("td", {}, OPS.statusChip(item.sla_status || item.status)),
      h("td", { dir: "auto" }, bidi(item.subject || "(no subject)")),
      mine ? h("td", { dir: "auto" }, bidi(item.customer_name || "—")) : null,
      h("td", { dir: "auto" }, bidi(item.team || "—")),
      h("td", { class: "nowrap" }, item.waiting || (item.response_minutes != null ? OPS.fmtDuration(item.response_minutes) : OPS.fmtWhen(when, state.serverTime))));
  }
  function mailTable(items, mine, caption) {
    return h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("caption", { class: "sr-only" }, caption),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "State"), h("th", { scope: "col" }, "Subject"),
        mine ? h("th", { scope: "col" }, "Customer") : null, h("th", { scope: "col" }, "Team"), h("th", { scope: "col" }, mine ? "Waiting / reply" : "Waiting"))),
      h("tbody", {}, items.map((i) => mailRow(i, mine)))));
  }

  function emailCard(e) {
    if (!e.configured) {
      return OPS.card("My e-mails", {}, OPS.emptyState({ title: "No e-mail address on your account", icon: "mail",
        text: "Ask your administrator to add your e-mail address to your account so your replies and assignments show here." }));
    }
    if (!e.available) return OPS.card("My e-mails", {}, OPS.emptyState({ title: "No e-mail scan yet", icon: "mail", text: "Your e-mails will appear after the first scan." }));
    const a = e.assigned, r = e.replied;
    const parts = [h("div", { class: "my-tiles" },
      OPS.kpi({ label: "Assigned to me", value: fmtNum(a.pending), sub: `${fmtNum(a.done)} done`, tone: a.pending ? "warn" : null, icon: "mail" }),
      OPS.kpi({ label: "Replied today", value: fmtNum(r.count), sub: r.average_first_reply_minutes != null ? `avg ${OPS.fmtDuration(r.average_first_reply_minutes)}` : "", icon: "check" }),
      OPS.kpi({ label: "On time / late", value: `${fmtNum(r.sla_met)} / ${fmtNum(r.sla_breached)}`, tone: r.sla_breached ? "danger" : "ok", icon: "shield" }))];
    if (a.items.length) parts.push(h("div", {}, h("h3", { class: "my-sub" }, "Assigned to me"), mailTable(a.items, true, "E-mails assigned to me")));
    if (r.items.length) parts.push(h("div", {}, h("h3", { class: "my-sub" }, "Replied today"), mailTable(r.items, true, "E-mails I replied to today")));
    if (!a.items.length && !r.items.length) parts.push(OPS.emptyState({ title: "Nothing assigned to you yet", icon: "check", text: "E-mails assigned to you, and the ones you reply to, show here." }));
    return OPS.card("My e-mails", {}, parts);
  }

  function teamCard(e) {
    if (e.team_emails_hint === "no_team") {
      return OPS.card("My team's e-mails", {}, OPS.emptyState({ title: "You are not in a team yet", icon: "info",
        text: "Ask your administrator to add you to a team to see your team's e-mails here." }));
    }
    const s = e.team_summary || {};
    const items = e.team_emails || [];
    const summary = h("div", { class: "my-tiles" },
      OPS.kpi({ label: "Pending", value: fmtNum(s.pending || 0), tone: s.pending ? "warn" : null, icon: "clock" }),
      OPS.kpi({ label: "Replied", value: fmtNum(s.replied || 0), icon: "check" }),
      OPS.kpi({ label: "Past SLA", value: fmtNum(s.sla_breached || 0), tone: s.sla_breached ? "danger" : null, icon: "alert" }),
      OPS.kpi({ label: "Reply rate", value: OPS.fmtPct(s.reply_rate), icon: "activity" }));
    const list = items.length ? mailTable(items, false, "My team's e-mails")
      : OPS.emptyState({ title: "Nothing pending for your team", icon: "check", text: "Your team's open e-mails show here." });
    return OPS.card("My team's e-mails", { hint: (s.teams || []).join(", ") }, summary, list,
      s.more ? h("p", { class: "hint" }, `and ${fmtNum(s.more)} more not shown`) : null);
  }

  // ------------------------------------------------------------------ my quality reviews
  async function appeal(review) {
    const note = h("textarea", { rows: "3", maxlength: "500", required: true, dir: "auto", "aria-label": "Why do you disagree?" });
    await OPS.modal({
      title: "Appeal this rating", submitLabel: "Send appeal",
      body: h("div", {}, h("p", { class: "muted", dir: "auto" }, bidi(review.subject || "")),
        h("label", { class: "my-field" }, h("span", {}, "Why do you disagree?"), note)),
      onSubmit: async () => {
        if (!note.value.trim()) throw new Error("Please explain why you disagree.");
        await api("POST", "/api/ops/email/quality/appeal", { message_id: review.message_id, appeal: note.value.trim() });
        toast("Appeal sent.");
        await loader.refresh();
      },
    });
  }

  function qualityCard(q) {
    if (!q.configured || !q.reviews.length) {
      return OPS.card("My quality reviews", {}, OPS.emptyState({ title: "No reviews yet", icon: "activity",
        text: "When a manager reviews one of your replies, it shows here with its rating." }));
    }
    const tone = (r) => (r === "Needs Improvement" ? "warn" : r === "Excellent" ? "ok" : "info");
    const rows = q.reviews.map((rev) => h("tr", {},
      h("td", {}, rev.rating ? OPS.chip(rev.rating, tone(rev.rating)) : h("span", { class: "hint" }, "—")),
      h("td", { dir: "auto" }, bidi(rev.subject || "(no subject)")),
      h("td", { dir: "auto" }, bidi(rev.reasons || "")),
      h("td", {}, rev.appeal_decision ? OPS.chip(`Appeal: ${rev.appeal_decision}`, "info")
        : rev.appeal_open ? OPS.chip("Appeal pending", "info")
          : (rev.can_appeal && !READ_ONLY ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => appeal(rev) }, "Appeal") : h("span", { class: "hint" }, "—")))));
    return OPS.card("My quality reviews", { hint: `${fmtNum(q.counts.reviewed || 0)} reviewed` },
      h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("caption", { class: "sr-only" }, "My quality reviews"),
        h("thead", {}, h("tr", {}, ["Rating", "Subject", "Reasons", ""].map((c) => h("th", { scope: "col" }, c)))),
        h("tbody", {}, rows))));
  }

  // ------------------------------------------------------------------ my security requests
  function slaCard(s) {
    if (!s.configured) {
      return OPS.card("My security requests", {}, OPS.emptyState({ title: "No security name on your account", icon: "shield",
        text: "Ask your administrator to set your security name (as it appears in the portal) so your requests show here." }));
    }
    if (!s.available) return OPS.card("My security requests", {}, OPS.emptyState({ title: "No security scan yet", icon: "shield", text: "Your requests will appear after the first scan." }));
    const t = s.totals;
    const tiles = h("div", { class: "my-tiles" },
      OPS.kpi({ label: "Open", value: fmtNum(t.open), icon: "shield", hero: true }),
      OPS.kpi({ label: "Reminder", value: fmtNum(t.reminder), tone: t.reminder ? "warn" : null, icon: "clock" }),
      OPS.kpi({ label: "Breached", value: fmtNum(t.breached), tone: t.breached ? "danger" : null, icon: "alert" }));
    if (!s.requests.length) return OPS.card("My security requests", {}, tiles, OPS.emptyState({ title: "Nothing waiting for you", icon: "check", text: "No security request is open under your name." }));
    const rows = s.requests.map((r) => {
      const fields = r.fields || {};
      return h("tr", { "data-sent": r.sent_at || "" },
        h("td", {}, OPS.statusChip(r.status)),
        h("td", { class: "nowrap my-age", "data-age": "" }, OPS.fmtDuration(liveMinutes(r.sent_at, r.age_minutes))),
        ...["اسم الموظف", "النظام", "نوع الطلب"].map((k) => h("td", { dir: "auto" }, bidi(fields[k] || "—"))));
    });
    return OPS.card("My security requests", {}, tiles,
      h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("caption", { class: "sr-only" }, "My security requests"),
        h("thead", {}, h("tr", {}, ["State", "Age", "Employee", "System", "Request type"].map((c) => h("th", { scope: "col" }, c)))),
        h("tbody", {}, rows))));
  }

  function tickAges() {
    for (const cell of page.querySelectorAll(".my-age[data-age]")) {
      const tr = cell.closest("tr");
      if (tr && tr.dataset.sent) cell.textContent = OPS.fmtDuration(liveMinutes(tr.dataset.sent, null));
    }
  }

  // ------------------------------------------------------------------ render / loop
  function render(d) {
    state.data = d;
    noteServerTime(d.server_time);
    if (strip) strip.update(d.shared);
    const name = (d.user && d.user.name) || "there";
    el.greeting.textContent = READ_ONLY ? `${name}'s work` : `Hello, ${name}`;
    el.date.textContent = d.date ? new Date(`${d.date}T12:00:00`).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" }) : "";
    const col = d.shared && d.shared.collector;
    el.freshness.textContent = col && col.last_update
      ? `${col.online ? "Live · data from" : "Data from"} ${OPS.fmtClock(col.last_update)}`
      : "";
    el.freshness.hidden = !(col && col.last_update);
    el.body.replaceChildren(callsCard(d.calls), emailCard(d.email), teamCard(d.email), qualityCard(d.quality), slaCard(d.sla));
  }
  function showError(e) {
    if (!state.data) toast(e.network ? "Cannot reach the server - retrying." : e.message, "error");
  }

  const loader = OPS.loader({ url: () => `/api/ops/my${VIEW_AS ? `?as=${encodeURIComponent(VIEW_AS)}` : ""}`,
    interval: 30000, since: false, onData: render, onError: showError });

  function build() {
    el.greeting = h("h1", {}, READ_ONLY ? "Work" : "My work");
    el.date = h("p", { class: "hint" });
    el.freshness = h("p", { class: "hint my-fresh", role: "status", hidden: true });
    el.stripHost = h("section", { id: "myStrip", class: "op-strip-host" });
    el.body = h("div", { class: "my-stack" }, h("p", { class: "empty" }, "Loading your work…"));
    const logCall = READ_ONLY ? null : h("a", { class: "btn btn-primary", href: "/dashboard" }, OPS.icon("phone"), "Log a call");
    page.replaceChildren(
      h("div", { class: "op-titlebar" }, h("div", {}, el.greeting, el.date, el.freshness), h("span", { class: "grow" }), logCall),
      el.stripHost, el.body);
    strip = OPS.sharedStrip(el.stripHost, { poll: false, links: false });
  }

  async function start() {
    await ensureStyles();
    build();
    loader.start();
    setInterval(() => { if (!document.hidden) tickAges(); }, TICK_MS);
  }
  start();
})();
